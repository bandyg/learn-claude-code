from __future__ import annotations

import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agents.s02.agent_service import SessionStore, create_app


def fake_chat_handler(message: str, history: list[dict]) -> tuple[str, dict]:
    return f"echo:{message}|history={len(history)}", {"llm_calls": 1, "tool_calls": 0}


def build_client() -> TestClient:
    store = SessionStore(ttl_sec=1800, max_items=100, cleanup_interval_sec=9999)
    app = create_app(chat_handler=fake_chat_handler, session_store=store)
    return TestClient(app)


def _history_count(reply: str) -> int:
    match = re.search(r"history=(\d+)", reply)
    if not match:
        raise AssertionError(f"Cannot parse history count from: {reply}")
    return int(match.group(1))


def test_chat_happy_path() -> None:
    with build_client() as client:
        payload = {"user_id": "u1", "session_id": "s1", "message": "hello"}
        resp1 = client.post("/chat", json=payload)
        assert resp1.status_code == 200
        body1 = resp1.json()
        assert body1["status"] == "ok"
        assert body1["session_status"] == "ACTIVE"
        assert _history_count(body1["reply"]) == 0

        resp2 = client.post("/chat", json={"user_id": "u1", "session_id": "s1", "message": "again"})
        assert resp2.status_code == 200
        assert _history_count(resp2.json()["reply"]) == 2


def test_chat_invalid_payload() -> None:
    with build_client() as client:
        bad_json_resp = client.post("/chat", data="{", headers={"Content-Type": "application/json"})
        assert bad_json_resp.status_code == 400
        bad_body = bad_json_resp.json()
        assert bad_body["error_code"] == "INVALID_JSON"

        non_obj_resp = client.post("/chat", json=["not-object"])
        assert non_obj_resp.status_code == 400
        assert non_obj_resp.json()["error_code"] == "INVALID_REQUEST"

        missing_field_resp = client.post("/chat", json={"user_id": "u1", "session_id": "s1"})
        assert missing_field_resp.status_code == 400
        missing_body = missing_field_resp.json()
        assert missing_body["error_code"] == "INVALID_ARGUMENT"
        assert "message" in missing_body["message"]


def test_chat_concurrent_calls_same_session() -> None:
    with build_client() as client:
        def _call(idx: int) -> int:
            resp = client.post(
                "/chat",
                json={"user_id": "u2", "session_id": "s2", "message": f"m{idx}"},
            )
            assert resp.status_code == 200
            return _history_count(resp.json()["reply"])

        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(_call, range(10)))

        assert len(results) == 10
        final = client.post("/chat", json={"user_id": "u2", "session_id": "s2", "message": "tail"})
        assert final.status_code == 200
        assert _history_count(final.json()["reply"]) == 20


def test_new_resets_context_and_generates_unique_session_id() -> None:
    with build_client() as client:
        # Seed old session history.
        _ = client.post("/chat", json={"user_id": "u3", "session_id": "old", "message": "a"})
        second = client.post("/chat", json={"user_id": "u3", "session_id": "old", "message": "b"})
        assert _history_count(second.json()["reply"]) == 2

        reset1 = client.post("/new", json={"user_id": "u3", "session_id": "old"})
        assert reset1.status_code == 200
        body1 = reset1.json()
        assert body1["status"] == "reset_ok"
        sid1 = body1["session_id"]
        assert sid1 and sid1 != "old"

        after_reset = client.post("/chat", json={"user_id": "u3", "session_id": sid1, "message": "fresh"})
        assert after_reset.status_code == 200
        assert _history_count(after_reset.json()["reply"]) == 0

        reset2 = client.post("/new", json={"user_id": "u3"})
        assert reset2.status_code == 200
        sid2 = reset2.json()["session_id"]
        assert sid2 != sid1

        # Unknown user should be silently created/reset.
        reset_unknown = client.post("/new", json={"user_id": "nouser"})
        assert reset_unknown.status_code == 200
        assert reset_unknown.json()["status"] == "reset_ok"


def test_chat_internal_error_returns_500() -> None:
    def boom_handler(message: str, history: list[dict]) -> tuple[str, dict]:
        raise RuntimeError("boom")

    store = SessionStore(ttl_sec=1800, max_items=10, cleanup_interval_sec=9999)
    app = create_app(chat_handler=boom_handler, session_store=store)
    with TestClient(app) as client:
        resp = client.post("/chat", json={"user_id": "u4", "session_id": "s4", "message": "x"})
        assert resp.status_code == 500
        assert resp.json()["error_code"] == "INTERNAL_ERROR"


def test_session_store_expiry_and_lru_paths() -> None:
    store = SessionStore(ttl_sec=100, max_items=1, cleanup_interval_sec=9999)
    try:
        first = store.get_or_create("u5", "s1")
        first.last_access_at = time.time() - 1000
        store.get_or_create("u5", "s2")
        # max_items=1 should evict older entry
        assert "u5:s1" not in store._entries
        assert store._user_current_session.get("u5") == "s2"

        # force current session to expired then cleanup on read path
        current = store.get_or_create("u5", "s2")
        current.last_access_at = time.time() - 1000
        store.get_or_create("u6", "s3")
        assert "u5" not in store._user_current_session or store._user_current_session.get("u5") != "s2"
    finally:
        store.close()


def test_metrics_dataclass_serialization() -> None:
    @dataclass
    class M:
        llm_calls: int
        tool_calls: int

    def dataclass_handler(message: str, history: list[dict]) -> tuple[str, M]:
        return "ok", M(llm_calls=2, tool_calls=3)

    app = create_app(chat_handler=dataclass_handler, session_store=SessionStore(cleanup_interval_sec=9999))
    with TestClient(app) as client:
        resp = client.post("/chat", json={"user_id": "u6", "session_id": "s6", "message": "hi"})
        assert resp.status_code == 200
        assert resp.json()["metrics"] == {"llm_calls": 2, "tool_calls": 3}
