#!/usr/bin/env python3
"""
HTTP service wrapper for s02_handwrite agent loop.

Endpoints:
- POST /chat: continue a session and return assistant reply
- POST /new: reset user's current session context and issue a new session_id
"""

from __future__ import annotations

import logging
import os
import sys
import threading
import time
import traceback
import uuid
from json import JSONDecodeError
from collections import OrderedDict
from dataclasses import asdict, dataclass, field, is_dataclass
from pathlib import Path
from typing import Any, Callable

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
import uvicorn

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agents.s02.s02_handwrite import chat_with_tools


SESSION_TTL_SEC = int(os.getenv("SESSION_TTL_SEC", "1800"))
SESSION_MAX_ITEMS = int(os.getenv("SESSION_MAX_ITEMS", "1024"))
SESSION_CLEANUP_INTERVAL_SEC = int(os.getenv("SESSION_CLEANUP_INTERVAL_SEC", "30"))
SERVICE_HOST = os.getenv("SERVICE_HOST", "0.0.0.0")
SERVICE_PORT = int(os.getenv("SERVICE_PORT", "5015"))


logger = logging.getLogger("s02_agent_service")
if not logger.handlers:
    logging.basicConfig(level=logging.INFO, format="%(message)s")


def log_event(
    level: int,
    endpoint: str,
    user_id: str,
    session_id: str,
    duration_ms: float,
    message: str,
    error_code: str = "",
    stack: str = "",
) -> None:
    payload = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "endpoint": endpoint,
        "user_id": user_id,
        "session_id": session_id,
        "duration_ms": round(duration_ms, 2),
        "message": message,
    }
    if error_code:
        payload["error_code"] = error_code
    if stack:
        payload["stack"] = stack
    logger.log(level, str(payload))


class APIError(Exception):
    def __init__(self, status_code: int, error_code: str, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.error_code = error_code
        self.message = message


@dataclass
class SessionContext:
    history: list[dict[str, Any]] = field(default_factory=list)
    entities: dict[str, Any] = field(default_factory=dict)
    temp_state: dict[str, Any] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)
    last_access_at: float = field(default_factory=time.time)
    lock: threading.RLock = field(default_factory=threading.RLock)


class SessionStore:
    def __init__(
        self,
        ttl_sec: int = SESSION_TTL_SEC,
        max_items: int = SESSION_MAX_ITEMS,
        cleanup_interval_sec: int = SESSION_CLEANUP_INTERVAL_SEC,
    ):
        self.ttl_sec = ttl_sec
        self.max_items = max_items
        self.cleanup_interval_sec = cleanup_interval_sec
        self._entries: OrderedDict[str, SessionContext] = OrderedDict()
        self._user_current_session: dict[str, str] = {}
        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        self._cleaner_thread = threading.Thread(target=self._cleaner_loop, daemon=True)
        self._cleaner_thread.start()

    def close(self) -> None:
        self._stop_event.set()
        self._cleaner_thread.join(timeout=0.5)

    def _cleaner_loop(self) -> None:
        while not self._stop_event.wait(self.cleanup_interval_sec):
            with self._lock:
                self._cleanup_expired_locked()

    @staticmethod
    def _build_key(user_id: str, session_id: str) -> str:
        return f"{user_id}:{session_id}"

    def _cleanup_expired_locked(self) -> None:
        now = time.time()
        expired_keys = [
            key for key, ctx in self._entries.items()
            if now - ctx.last_access_at > self.ttl_sec
        ]
        for key in expired_keys:
            self._entries.pop(key, None)
            user_id, session_id = key.split(":", 1)
            if self._user_current_session.get(user_id) == session_id:
                self._user_current_session.pop(user_id, None)

    def _evict_lru_locked(self) -> None:
        while len(self._entries) > self.max_items:
            key, _ = self._entries.popitem(last=False)
            user_id, session_id = key.split(":", 1)
            if self._user_current_session.get(user_id) == session_id:
                self._user_current_session.pop(user_id, None)

    def get_or_create(self, user_id: str, session_id: str) -> SessionContext:
        with self._lock:
            self._cleanup_expired_locked()
            key = self._build_key(user_id, session_id)
            ctx = self._entries.get(key)
            if ctx is None:
                ctx = SessionContext()
                self._entries[key] = ctx
            ctx.last_access_at = time.time()
            self._entries.move_to_end(key, last=True)
            self._user_current_session[user_id] = session_id
            self._evict_lru_locked()
            return ctx

    def reset_user_session(self, user_id: str, session_id: str | None = None) -> str:
        with self._lock:
            self._cleanup_expired_locked()
            old_session_id = session_id or self._user_current_session.get(user_id)
            if old_session_id:
                old_key = self._build_key(user_id, old_session_id)
                self._entries.pop(old_key, None)

            new_session_id = str(uuid.uuid4())
            new_key = self._build_key(user_id, new_session_id)
            self._entries[new_key] = SessionContext()
            self._entries.move_to_end(new_key, last=True)
            self._user_current_session[user_id] = new_session_id
            self._evict_lru_locked()
            return new_session_id


def _metrics_to_dict(metrics: Any) -> dict[str, Any]:
    if is_dataclass(metrics):
        return asdict(metrics)
    if isinstance(metrics, dict):
        return metrics
    return {}


def _validate_required_text(name: str, value: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise APIError(
            status_code=400,
            error_code="INVALID_ARGUMENT",
            message=f"Field `{name}` is required and must be non-empty.",
        )
    return text


async def _parse_json_payload(request: Request) -> dict[str, Any]:
    try:
        payload = await request.json()
    except JSONDecodeError:
        raise APIError(
            status_code=400,
            error_code="INVALID_JSON",
            message="Malformed JSON body.",
        )
    if not isinstance(payload, dict):
        raise APIError(
            status_code=400,
            error_code="INVALID_REQUEST",
            message="JSON body must be an object.",
        )
    return payload


def default_chat_handler(message: str, history: list[dict[str, Any]]) -> tuple[str, Any]:
    return chat_with_tools(message, history)


def create_app(
    chat_handler: Callable[[str, list[dict[str, Any]]], tuple[str, Any]] | None = None,
    session_store: SessionStore | None = None,
) -> FastAPI:
    app = FastAPI(title="s02 agent service")
    store = session_store or SessionStore()
    handler = chat_handler or default_chat_handler
    app.state.session_store = store

    @app.exception_handler(APIError)
    async def api_error_handler(request: Request, exc: APIError) -> JSONResponse:
        duration_ms = (time.perf_counter() - getattr(request.state, "start_perf", time.perf_counter())) * 1000
        user_id = getattr(request.state, "user_id", "")
        session_id = getattr(request.state, "session_id", "")
        log_event(
            logging.WARNING,
            request.url.path,
            user_id,
            session_id,
            duration_ms,
            exc.message,
            error_code=exc.error_code,
        )
        return JSONResponse(
            status_code=exc.status_code,
            content={"error_code": exc.error_code, "message": exc.message},
        )

    @app.middleware("http")
    async def attach_request_timer(request: Request, call_next):
        request.state.start_perf = time.perf_counter()
        try:
            response = await call_next(request)
            duration_ms = (time.perf_counter() - request.state.start_perf) * 1000
            user_id = getattr(request.state, "user_id", "")
            session_id = getattr(request.state, "session_id", "")
            log_event(
                logging.INFO,
                request.url.path,
                user_id,
                session_id,
                duration_ms,
                f"status={response.status_code}",
            )
            return response
        except Exception as exc:
            duration_ms = (time.perf_counter() - request.state.start_perf) * 1000
            user_id = getattr(request.state, "user_id", "")
            session_id = getattr(request.state, "session_id", "")
            stack = traceback.format_exc()
            log_event(
                logging.ERROR,
                request.url.path,
                user_id,
                session_id,
                duration_ms,
                str(exc),
                error_code="INTERNAL_ERROR",
                stack=stack,
            )
            return JSONResponse(
                status_code=500,
                content={"error_code": "INTERNAL_ERROR", "message": "Internal server error."},
            )

    @app.post("/chat")
    async def chat(request: Request) -> dict[str, Any]:
        payload = await _parse_json_payload(request)
        user_id = _validate_required_text("user_id", payload.get("user_id", ""))
        session_id = _validate_required_text("session_id", payload.get("session_id", ""))
        message = _validate_required_text("message", payload.get("message", ""))

        request.state.user_id = user_id
        request.state.session_id = session_id
        ctx = store.get_or_create(user_id=user_id, session_id=session_id)
        with ctx.lock:
            reply_text, metrics = handler(message, list(ctx.history))
            ctx.history.append({"role": "user", "content": message})
            ctx.history.append({"role": "assistant", "content": reply_text})
            ctx.last_access_at = time.time()

        return {
            "status": "ok",
            "session_status": "ACTIVE",
            "user_id": user_id,
            "session_id": session_id,
            "reply": reply_text,
            "metrics": _metrics_to_dict(metrics),
        }

    @app.post("/new")
    async def new_session(request: Request) -> dict[str, str]:
        payload = await _parse_json_payload(request)
        user_id = _validate_required_text("user_id", payload.get("user_id", ""))
        session_id = str(payload.get("session_id", "") or "").strip() or None
        request.state.user_id = user_id
        request.state.session_id = session_id or ""
        new_session_id = store.reset_user_session(user_id=user_id, session_id=session_id)
        return {"status": "reset_ok", "session_id": new_session_id}

    @app.on_event("shutdown")
    def on_shutdown() -> None:
        store.close()

    return app


app = create_app()


if __name__ == "__main__":
    uvicorn.run(app, host=SERVICE_HOST, port=SERVICE_PORT, log_level="info")
