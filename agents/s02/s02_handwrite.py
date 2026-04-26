#!/usr/bin/env python3
# s01_anthropic_force_tools.py - 强制工具调用版
"""
强制模型使用工具，完美解决 qwen3 不调用工具问题。
"""

import os
import json
import subprocess
import time
import uuid
import platform
import re
from pathlib import Path
from dataclasses import dataclass
from typing import Any
import anthropic
from anthropic.types import Message

from dotenv import load_dotenv
load_dotenv(override=True)

# === 配置 ===
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "ollama")
ANTHROPIC_BASE_URL = os.getenv("ANTHROPIC_BASE_URL", "http://192.168.1.99:11434")
MODEL = os.getenv("MODEL", os.getenv("MODEL_ID", "qwen3:14b"))
MAX_INLINE_OUTPUT = int(os.getenv("MAX_INLINE_OUTPUT", "4000"))
TOOL_TIMEOUT_SEC = int(os.getenv("TOOL_TIMEOUT_SEC", "30"))
MAX_TOOL_TURNS = int(os.getenv("MAX_TOOL_TURNS", "8"))
MAX_API_RETRIES = int(os.getenv("MAX_API_RETRIES", "2"))
RETRY_BASE_DELAY_SEC = float(os.getenv("RETRY_BASE_DELAY_SEC", "0.6"))
MAX_HISTORY_MESSAGES = int(os.getenv("MAX_HISTORY_MESSAGES", "24"))
CACHE_TTL_SEC = int(os.getenv("CACHE_TTL_SEC", "10"))
MAX_CACHE_ITEMS = int(os.getenv("MAX_CACHE_ITEMS", "200"))
MAX_SAME_TOOL_REPEAT = int(os.getenv("MAX_SAME_TOOL_REPEAT", "3"))
MAX_WRITE_CHARS = int(os.getenv("MAX_WRITE_CHARS", "300000"))
MAX_FINAL_CONTINUATIONS = int(os.getenv("MAX_FINAL_CONTINUATIONS", "2"))
BASH_POLICY_MODE = os.getenv("BASH_POLICY_MODE", "restricted").strip().lower()

# 检测操作系统
IS_WINDOWS = platform.system() == "Windows"
WORKDIR = Path(os.getcwd()).resolve()
OUTPUT_DIR = WORKDIR / ".tool_outputs"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
TOOL_OUTPUT_FILE_RE = re.compile(r"tool_output_\d+_[0-9a-f]{8}\.txt", re.IGNORECASE)


@dataclass
class CommandCacheEntry:
    output_json: str
    created_at: float


@dataclass
class QueryMetrics:
    llm_calls: int = 0
    llm_ms: float = 0.0
    tool_calls: int = 0
    tool_ms: float = 0.0
    loop_break_reason: str = ""


COMMAND_CACHE: dict[str, CommandCacheEntry] = {}

# === 强制工具使用的 SYSTEM 提示 ===
SYSTEM = f"""You are a senior software engineer.

Environment:
- OS: {'Windows PowerShell' if IS_WINDOWS else 'Linux/macOS shell'}
- Working directory: {WORKDIR}

Command policy ({'Windows' if IS_WINDOWS else 'Unix'}):
- {'Use dir/type/findstr/cd. Avoid sed/tail/head/wc/grep/cat.' if IS_WINDOWS else 'Use ls/cat/grep/find.'}
- If output is truncated, use read_file tool or narrower commands to fetch exact ranges.
- Never claim you cannot execute commands in this environment. Use tools and report real tool errors.
"""

# === 客户端 ===
client = anthropic.Anthropic(
    api_key=ANTHROPIC_API_KEY,
    base_url=ANTHROPIC_BASE_URL,
)

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "bash",
            "description": f"Execute shell command. Windows: use 'dir', 'type file'. Linux/Mac: 'ls', 'cat file'.",
            "parameters": {
                "type": "object",
                "properties": {"command": {"type": "string", "description": "Shell command"}},
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a local text file with optional line window.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Path relative to workspace or absolute path in workspace"},
                    "offset": {"type": "integer", "description": "Start line (1-based), default 1"},
                    "limit": {"type": "integer", "description": "Max lines to read, default 200"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "open_file",
            "description": "Open/read a local file with line range support (line_start/line_end).",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Path relative to workspace or absolute path in workspace"},
                    "line_start": {"type": "integer", "description": "Start line (1-based), default 1"},
                    "line_end": {"type": "integer", "description": "End line (1-based), default start+199"},
                    "offset": {"type": "integer", "description": "Alias for line_start"},
                    "limit": {"type": "integer", "description": "Alias for number of lines to read"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Write text content to a local file. Supports append mode for chunked writes.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Target file path relative to workspace or absolute path in workspace"},
                    "content": {"type": "string", "description": "Text content to write"},
                    "append": {"type": "boolean", "description": "Append to file if true; overwrite if false/default"},
                },
                "required": ["path", "content"],
            },
        },
    },
]


def safe_path(path_str: str) -> Path:
    p = Path(path_str)
    if not p.is_absolute():
        p = (WORKDIR / p).resolve()
    else:
        p = p.resolve()
    if not p.is_relative_to(WORKDIR):
        raise ValueError(f"Path escapes workspace: {path_str}")
    return p


def compact_history(history: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if len(history) <= MAX_HISTORY_MESSAGES:
        return history
    trimmed = history[-MAX_HISTORY_MESSAGES:]
    dropped = len(history) - len(trimmed)
    note = {
        "role": "assistant",
        "content": f"<context-note>{dropped} earlier messages omitted to reduce token usage.</context-note>",
    }
    return [note] + trimmed


def is_dangerous_command(command: str) -> bool:
    lowered = command.lower()
    dangerous_tokens = [
        "rm -rf",
        "del /f /s /q",
        "rmdir /s /q",
        "rd /s /q",
        "remove-item",
        "shutdown",
        "reboot",
        "format ",
        "mkfs",
        "diskpart",
        "powershell -enc",
        "invoke-expression",
        "iwr ",
        "curl ",
        "wget ",
        ":(){:|:&};:",
    ]
    return any(token in lowered for token in dangerous_tokens)


def has_forbidden_shell_operators(command: str) -> bool:
    # Disallow shell chaining/redirection in restricted mode.
    forbidden = ("&&", "||", ";", "|", ">", "<", "`", "$(")
    return any(token in command for token in forbidden)


def is_allowed_bash_command(command: str) -> bool:
    if BASH_POLICY_MODE == "permissive":
        return True
    lowered = command.strip().lower()
    if not lowered:
        return False
    allowed_prefixes = (
        "dir",
        "type ",
        "cd",
        "findstr ",
        "python -c ",
        "git status",
        "git log",
        "git show",
        "git diff",
        "git ls-tree",
        "git rev-parse",
    )
    return lowered.startswith(allowed_prefixes)


def is_read_only_command(command: str) -> bool:
    lowered = command.strip().lower()
    read_only_prefixes = (
        "dir",
        "type ",
        "cd",
        "git show",
        "git log",
        "git ls-tree",
        "git rev-parse",
        "git status",
        "findstr ",
        "python -c ",
    )
    return lowered.startswith(read_only_prefixes)


def translate_windows_command(command: str) -> tuple[str, str]:
    stripped = command.strip()
    if not IS_WINDOWS or not stripped:
        return stripped, ""

    parts = stripped.split()
    if not parts:
        return stripped, ""

    cmd = parts[0].lower()
    args = " ".join(parts[1:])

    if cmd == "ls":
        return (f"dir {args}".strip(), "Auto-translated 'ls' to 'dir' for Windows.")
    if cmd == "cat":
        return (f"type {args}".strip(), "Auto-translated 'cat' to 'type' for Windows.")
    if cmd == "pwd":
        return "cd", "Auto-translated 'pwd' to 'cd' for Windows."
    if cmd == "grep":
        return (f"findstr {args}".strip(), "Auto-translated 'grep' to 'findstr' for Windows (basic mode).")
    if cmd in {"sed", "tail", "head", "wc"}:
        return stripped, f"'{cmd}' is not available in cmd.exe by default. Use PowerShell/Get-Content/findstr or targeted git commands."
    return stripped, ""


def normalize_command(raw_command: str) -> tuple[str, str]:
    command = (raw_command or "").strip()
    hint = ""
    if command.lower().startswith("bash "):
        remainder = command[5:].strip()
        for prefix in ("-lc", "-c"):
            if remainder.lower().startswith(prefix):
                command = remainder[len(prefix):].strip()
                break
        else:
            command = remainder
    command, translate_hint = translate_windows_command(command)
    if translate_hint:
        hint = translate_hint
    return command, hint


def extract_read_target_from_command(command: str) -> str:
    stripped = (command or "").strip()
    lowered = stripped.lower()
    if not (lowered.startswith("type ") or lowered.startswith("cat ")):
        return ""
    parts = stripped.split(maxsplit=1)
    if len(parts) < 2:
        return ""
    target = parts[1].strip()
    if (target.startswith('"') and target.endswith('"')) or (target.startswith("'") and target.endswith("'")):
        target = target[1:-1].strip()
    return target


def canonicalize_tool_args_for_loop(name: str, args: dict[str, Any]) -> dict[str, Any]:
    if name != "bash":
        return args

    command = ""
    if isinstance(args, dict):
        if "command" in args:
            command = str(args.get("command") or "")
        elif "cmd" in args:
            command = str(args.get("cmd") or "")
    if not command:
        return args

    normalized = TOOL_OUTPUT_FILE_RE.sub("tool_output_<dynamic>.txt", command)
    if normalized == command:
        return args

    copied = dict(args)
    copied["command"] = normalized
    if "cmd" in copied:
        copied.pop("cmd", None)
    return copied


def maybe_store_large_output(text: str) -> tuple[str, str, bool]:
    clean_text = text.strip() or "(no output)"
    if len(clean_text) <= MAX_INLINE_OUTPUT:
        return clean_text, "", False

    file_name = f"tool_output_{int(time.time())}_{uuid.uuid4().hex[:8]}.txt"
    output_path = OUTPUT_DIR / file_name
    try:
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        output_path.write_text(clean_text, encoding="utf-8")
    except Exception as e:
        # Fallback: still return truncated preview instead of failing the whole tool call.
        preview = clean_text[:MAX_INLINE_OUTPUT]
        hidden_chars = len(clean_text) - len(preview)
        marker = (
            f"\n\n[TRUNCATED] Hidden {hidden_chars} chars. "
            f"Could not persist full output to file: {e}"
        )
        return preview + marker, "", True

    preview = clean_text[:MAX_INLINE_OUTPUT]
    hidden_chars = len(clean_text) - len(preview)
    marker = (
        f"\n\n[TRUNCATED] Hidden {hidden_chars} chars. "
        f"Use read_file with path='{output_path}' to inspect full output in chunks."
    )
    return preview + marker, str(output_path), True


def prune_cache() -> None:
    now = time.time()
    expired_keys = [k for k, v in COMMAND_CACHE.items() if now - v.created_at > CACHE_TTL_SEC]
    for key in expired_keys:
        COMMAND_CACHE.pop(key, None)
    if len(COMMAND_CACHE) <= MAX_CACHE_ITEMS:
        return
    sorted_items = sorted(COMMAND_CACHE.items(), key=lambda item: item[1].created_at)
    to_remove = len(COMMAND_CACHE) - MAX_CACHE_ITEMS
    for key, _ in sorted_items[:to_remove]:
        COMMAND_CACHE.pop(key, None)


def run_bash(command: str) -> dict[str, Any]:
    normalized_command, normalization_hint = normalize_command(command)
    if not normalized_command:
        hint_text = (
            f" Hint: {normalization_hint}" if normalization_hint else " Hint: pass {'command': 'dir /s'} on Windows."
        )
        return {
            "ok": False,
            "tool": "bash",
            "error": "Empty command",
            "original_command": command,
            "command": normalized_command,
            "exit_code": -1,
            "duration_ms": 0.0,
            "content": f"Empty command argument from model.{hint_text}",
            "truncated": False,
            "output_path": "",
            "hint": normalization_hint,
            "from_cache": False,
        }

    if is_dangerous_command(normalized_command):
        return {
            "ok": False,
            "tool": "bash",
            "error": "Dangerous command blocked",
            "original_command": command,
            "command": normalized_command,
            "exit_code": -1,
            "duration_ms": 0.0,
            "content": "Dangerous command blocked",
            "truncated": False,
            "output_path": "",
            "hint": normalization_hint,
            "from_cache": False,
        }
    if has_forbidden_shell_operators(normalized_command) and BASH_POLICY_MODE != "permissive":
        return {
            "ok": False,
            "tool": "bash",
            "error": "Command rejected by shell-operator policy",
            "original_command": command,
            "command": normalized_command,
            "exit_code": -1,
            "duration_ms": 0.0,
            "content": (
                "Command rejected: shell chaining/redirection operators are not allowed "
                "in restricted mode. Use a single safe command."
            ),
            "truncated": False,
            "output_path": "",
            "hint": "Allowed examples: `dir`, `type file`, `git log -n 20`, `findstr pattern file`",
            "from_cache": False,
        }
    if not is_allowed_bash_command(normalized_command):
        return {
            "ok": False,
            "tool": "bash",
            "error": "Command not allowed by policy",
            "original_command": command,
            "command": normalized_command,
            "exit_code": -1,
            "duration_ms": 0.0,
            "content": (
                "Command rejected by restricted policy. "
                "Use read-oriented commands or dedicated tools (open_file/write_file)."
            ),
            "truncated": False,
            "output_path": "",
            "hint": (
                "Allowed prefixes: dir, type, cd, findstr, python -c, "
                "git status/log/show/diff/ls-tree/rev-parse. "
                "Set BASH_POLICY_MODE=permissive to relax."
            ),
            "from_cache": False,
        }

    # Prevent recursive ".tool_outputs -> truncation -> new .tool_outputs" loops.
    read_target = extract_read_target_from_command(normalized_command)
    if read_target:
        try:
            safe_target = safe_path(read_target)
            if safe_target.is_relative_to(OUTPUT_DIR):
                delegated = run_read_file(str(safe_target), offset=1, limit=120)
                return {
                    "ok": bool(delegated.get("ok")),
                    "tool": "bash",
                    "original_command": command,
                    "command": normalized_command,
                    "exit_code": 0 if delegated.get("ok") else -1,
                    "duration_ms": float(delegated.get("duration_ms", 0.0)),
                    "content": delegated.get("content", ""),
                    "truncated": False,
                    "output_path": "",
                    "hint": (
                        "Detected .tool_outputs file read; returned direct line-window content "
                        "to avoid recursive truncation. Use open_file/read_file with line range "
                        "to continue reading."
                    ),
                    "from_cache": False,
                    "delegated_tool": "read_file",
                    "error": delegated.get("error", ""),
                }
        except Exception:
            pass

    prune_cache()
    cache_key = normalized_command.lower()
    if is_read_only_command(normalized_command):
        cached = COMMAND_CACHE.get(cache_key)
        if cached and time.time() - cached.created_at <= CACHE_TTL_SEC:
            payload = json.loads(cached.output_json)
            payload["from_cache"] = True
            return payload

    start = time.perf_counter()
    try:
        result = subprocess.run(
            normalized_command,
            shell=True,
            cwd=str(WORKDIR),
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            text=True,
            timeout=TOOL_TIMEOUT_SEC,
        )
        duration_ms = (time.perf_counter() - start) * 1000
        output = (result.stdout or "") + (result.stderr or "")
        preview, output_path, truncated = maybe_store_large_output(output)

        hint = normalization_hint
        if IS_WINDOWS and "not recognized as an internal or external command" in output:
            hint = (hint + " " if hint else "") + "Detected command-not-found on Windows shell."

        payload = {
            "ok": result.returncode == 0,
            "tool": "bash",
            "original_command": command,
            "command": normalized_command,
            "exit_code": result.returncode,
            "duration_ms": round(duration_ms, 2),
            "content": preview,
            "truncated": truncated,
            "output_path": output_path,
            "hint": hint,
            "from_cache": False,
        }
        if is_read_only_command(normalized_command):
            COMMAND_CACHE[cache_key] = CommandCacheEntry(
                output_json=json.dumps(payload, ensure_ascii=False),
                created_at=time.time(),
            )
        return payload
    except subprocess.TimeoutExpired:
        duration_ms = (time.perf_counter() - start) * 1000
        return {
            "ok": False,
            "tool": "bash",
            "error": "Command timed out",
            "original_command": command,
            "command": normalized_command,
            "exit_code": -1,
            "duration_ms": round(duration_ms, 2),
            "content": f"Command timed out after {TOOL_TIMEOUT_SEC}s",
            "truncated": False,
            "output_path": "",
            "hint": normalization_hint,
            "from_cache": False,
        }
    except Exception as e:
        duration_ms = (time.perf_counter() - start) * 1000
        return {
            "ok": False,
            "tool": "bash",
            "error": str(e),
            "original_command": command,
            "command": normalized_command,
            "exit_code": -1,
            "duration_ms": round(duration_ms, 2),
            "content": f"Error: {e}",
            "truncated": False,
            "output_path": "",
            "hint": normalization_hint,
            "from_cache": False,
        }


def run_read_file(path: str, offset: int = 1, limit: int = 200) -> dict[str, Any]:
    start = time.perf_counter()
    try:
        safe = safe_path(path)
        if not safe.exists():
            return {
                "ok": False,
                "tool": "read_file",
                "error": f"File not found: {safe}",
                "path": str(safe),
                "content": "",
            }
        if safe.is_dir():
            return {
                "ok": False,
                "tool": "read_file",
                "error": f"Path is directory: {safe}",
                "path": str(safe),
                "content": "",
            }

        offset = max(1, int(offset or 1))
        limit = min(1000, max(1, int(limit or 200)))
        text = safe.read_text(encoding="utf-8", errors="replace")
        lines = text.splitlines()
        start_idx = offset - 1
        end_idx = min(len(lines), start_idx + limit)
        selected = lines[start_idx:end_idx]
        content = "\n".join(f"{idx + 1:>4}→{line}" for idx, line in enumerate(selected, start=start_idx))
        duration_ms = (time.perf_counter() - start) * 1000
        return {
            "ok": True,
            "tool": "read_file",
            "path": str(safe),
            "offset": offset,
            "limit": limit,
            "line_count": len(lines),
            "returned_lines": len(selected),
            "duration_ms": round(duration_ms, 2),
            "content": content or "(empty file)",
        }
    except Exception as e:
        duration_ms = (time.perf_counter() - start) * 1000
        return {
            "ok": False,
            "tool": "read_file",
            "error": str(e),
            "path": path,
            "duration_ms": round(duration_ms, 2),
            "content": "",
        }


def run_write_file(path: str, content: str, append: bool = False) -> dict[str, Any]:
    start = time.perf_counter()
    try:
        safe = safe_path(path)
        safe.parent.mkdir(parents=True, exist_ok=True)
        text = str(content or "")
        if len(text) > MAX_WRITE_CHARS:
            return {
                "ok": False,
                "tool": "write_file",
                "error": f"Content too large ({len(text)} chars), max is {MAX_WRITE_CHARS}.",
                "path": str(safe),
                "content": "",
            }

        mode = "a" if append else "w"
        with safe.open(mode, encoding="utf-8", errors="replace") as f:
            f.write(text)
        duration_ms = (time.perf_counter() - start) * 1000
        return {
            "ok": True,
            "tool": "write_file",
            "path": str(safe),
            "append": bool(append),
            "written_chars": len(text),
            "duration_ms": round(duration_ms, 2),
            "content": f"Wrote {len(text)} chars to {safe}",
        }
    except Exception as e:
        duration_ms = (time.perf_counter() - start) * 1000
        return {
            "ok": False,
            "tool": "write_file",
            "error": str(e),
            "path": path,
            "duration_ms": round(duration_ms, 2),
            "content": "",
        }


def safe_extract_text(content: list[Any]) -> str:
    def _block_type(block: Any) -> str:
        if hasattr(block, "type"):
            return str(getattr(block, "type", "") or "")
        if isinstance(block, dict):
            return str(block.get("type", "") or "")
        return ""

    def _block_text(block: Any) -> str:
        if hasattr(block, "text"):
            return str(getattr(block, "text", "") or "")
        if isinstance(block, dict):
            return str(block.get("text", "") or "")
        return ""

    text_parts = []
    for block in content:
        try:
            btype = _block_type(block)
            if btype in {"text", "thinking"}:
                text = _block_text(block).strip()
                if text:
                    text_parts.append(text)
            elif not btype:
                # 兼容端可能不带 type，但仍含 text/content 字段
                if isinstance(block, dict):
                    for key in ("text", "content", "output_text", "message"):
                        value = block.get(key)
                        if isinstance(value, str) and value.strip():
                            text_parts.append(value.strip())
                            break
                    else:
                        as_text = str(block).strip()
                        if as_text:
                            text_parts.append(as_text)
                else:
                    as_text = str(block).strip()
                    if as_text:
                        # 尝试解析字符串化 JSON（某些兼容端会返回这种格式）
                        if as_text.startswith("{") and as_text.endswith("}"):
                            try:
                                parsed = json.loads(as_text)
                                if isinstance(parsed, dict):
                                    candidate = (
                                        parsed.get("text")
                                        or parsed.get("content")
                                        or parsed.get("output_text")
                                        or parsed.get("message")
                                    )
                                    if isinstance(candidate, str) and candidate.strip():
                                        text_parts.append(candidate.strip())
                                        continue
                            except Exception:
                                pass
                        text_parts.append(as_text)
        except Exception:
            continue
    merged = " ".join(t.strip() for t in text_parts if t.strip())
    return merged or "No text response generated by model."


def get_stop_reason(response: Any) -> str:
    try:
        return str(getattr(response, "stop_reason", "") or "")
    except Exception:
        return ""


def call_model(
    messages: list[dict[str, Any]],
    max_tokens: int,
    metrics: QueryMetrics,
    allow_tools: bool = True,
) -> Message:
    last_error = None
    request_max_tokens = max_tokens
    for attempt in range(MAX_API_RETRIES + 1):
        start = time.perf_counter()
        try:
            kwargs: dict[str, Any] = {
                "model": MODEL,
                "max_tokens": request_max_tokens,
                "messages": messages,
                "system": SYSTEM,
                "temperature": 0.0,
            }
            if allow_tools:
                kwargs["tools"] = TOOLS
                kwargs["tool_choice"] = {"type": "auto"}

            response = client.messages.create(
                **kwargs
            )
            metrics.llm_calls += 1
            metrics.llm_ms += (time.perf_counter() - start) * 1000
            return response
        except Exception as e:
            last_error = e
            err_text = str(e).lower()
            if allow_tools and "error parsing tool call" in err_text:
                request_max_tokens = min(max(request_max_tokens * 2, request_max_tokens + 512), 4096)
            if attempt >= MAX_API_RETRIES:
                break
            backoff = RETRY_BASE_DELAY_SEC * (2 ** attempt)
            time.sleep(backoff)
    raise RuntimeError(f"Model call failed after retries: {last_error}")


def execute_tools(
    normalized_calls: list[tuple[Any, str, dict[str, Any]]], metrics: QueryMetrics
) -> list[dict[str, Any]]:
    print("\n🔧 执行工具...")
    results = []
    for tool_call, name, args in normalized_calls:
        try:
            raw_input = getattr(tool_call, "input", {}) or {}
            print(f"  🧩 tool={name}, raw_input={repr(raw_input)[:200]}")

            start = time.perf_counter()
            if name == "bash":
                command = extract_command_from_args(args)
                print(f"  $ {command!r}")
                payload = run_bash(command)
            elif name in {"read_file", "open_file"}:
                path = str(args.get("path") or args.get("file") or "")
                offset, limit = parse_read_window(args)
                print(f"  $ {name} path={path} offset={offset} limit={limit}")
                if not path.strip():
                    payload = {
                        "ok": False,
                        "tool": name,
                        "error": "Empty path",
                        "content": "Missing required path argument.",
                    }
                else:
                    payload = run_read_file(path, offset=offset, limit=limit)
                    payload["tool"] = name
            elif name in {"write_file", "save_file", "export_file"}:
                path = str(args.get("path") or args.get("file") or "")
                content = str(args.get("content") or args.get("text") or "")
                append = bool(args.get("append", False))
                print(f"  $ {name} path={path} append={append} chars={len(content)}")
                if not path.strip():
                    payload = {
                        "ok": False,
                        "tool": name,
                        "error": "Empty path",
                        "content": "Missing required path argument.",
                    }
                else:
                    payload = run_write_file(path=path, content=content, append=append)
                    payload["tool"] = name
            else:
                payload = {
                    "ok": False,
                    "tool": name,
                    "error": f"Unknown tool: {name}",
                    "content": "",
                }
            elapsed_ms = (time.perf_counter() - start) * 1000
            metrics.tool_calls += 1
            metrics.tool_ms += elapsed_ms
            display_text = str(payload.get("content") or payload.get("error") or "(empty output)")
            preview = display_text[:200]
            print(f"  📤 {preview}{'...' if len(display_text) > 200 else ''}")

            results.append({
                "type": "tool_result",
                "tool_use_id": getattr(tool_call, "id", "call_1"),
                "content": json.dumps(payload, ensure_ascii=False),
            })
        except Exception as e:
            results.append({
                "type": "tool_result",
                "tool_use_id": getattr(tool_call, "id", "call_1"),
                "content": f"Error: {e}",
            })
    return results


def build_metrics_summary(metrics: QueryMetrics) -> str:
    summary = (
        f"llm_calls={metrics.llm_calls}, "
        f"llm_ms={metrics.llm_ms:.1f}, "
        f"tool_calls={metrics.tool_calls}, "
        f"tool_ms={metrics.tool_ms:.1f}"
    )
    if metrics.loop_break_reason:
        summary += f", loop_break={metrics.loop_break_reason}"
    return summary


def normalize_user_input(raw: str) -> str:
    # Handle BOM both as Unicode BOM and mojibake sequence ("ï»¿")
    cleaned = (raw or "").replace("\ufeff", "")
    if cleaned.startswith("ï»¿"):
        cleaned = cleaned[3:]
    return cleaned.strip()


def _coerce_to_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        text = value.strip()
        if text.startswith("{") and text.endswith("}"):
            try:
                obj = json.loads(text)
                if isinstance(obj, dict):
                    return obj
            except Exception:
                return {}
    return {}


def extract_tool_args(tool_call: Any) -> dict[str, Any]:
    raw_input = getattr(tool_call, "input", {}) or {}
    args = _coerce_to_dict(raw_input)
    if args:
        nested = _coerce_to_dict(args.get("arguments"))
        if nested:
            return nested
        return args
    return {}


def extract_command_from_args(args: dict[str, Any]) -> str:
    if "command" in args:
        value = args.get("command", "")
    elif "cmd" in args:
        value = args.get("cmd", "")
    elif "arguments" in args:
        nested = _coerce_to_dict(args.get("arguments"))
        if "command" in nested:
            value = nested.get("command", "")
        elif "cmd" in nested:
            value = nested.get("cmd", "")
        else:
            value = ""
    else:
        value = ""

    if isinstance(value, list):
        return " ".join(str(v) for v in value if str(v).strip()).strip()
    if isinstance(value, dict):
        if "command" in value:
            return str(value.get("command", "")).strip()
        if "cmd" in value:
            cmd_value = value.get("cmd", "")
            if isinstance(cmd_value, list):
                return " ".join(str(v) for v in cmd_value if str(v).strip()).strip()
            return str(cmd_value).strip()
    return str(value or "").strip()


def parse_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except Exception:
        return default


def parse_read_window(args: dict[str, Any]) -> tuple[int, int]:
    # 兼容两套参数:
    # 1) offset + limit
    # 2) line_start + line_end
    if "line_start" in args or "line_end" in args:
        line_start = parse_int(args.get("line_start", 1), 1)
        line_end = parse_int(args.get("line_end", line_start + 199), line_start + 199)
        line_start = max(1, line_start)
        line_end = max(line_start, line_end)
        return line_start, (line_end - line_start + 1)

    offset = parse_int(args.get("offset", 1), 1)
    limit = parse_int(args.get("limit", 200), 200)
    return offset, limit


def normalize_tool_name_and_args(name: str, args: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    lowered = (name or "").strip().lower()
    if lowered in {"open_file", "read", "readfile"}:
        path = str(args.get("path") or args.get("file") or "").strip()
        line_start = args.get("line_start", args.get("offset", 1))
        line_end = args.get("line_end")
        if line_end is None:
            limit = parse_int(args.get("limit", 200), 200)
            line_end = parse_int(line_start, 1) + max(1, limit) - 1
        return "open_file", {"path": path, "line_start": line_start, "line_end": line_end}

    if lowered in {"write", "writefile", "save", "save_file", "export_file"}:
        path = str(args.get("path") or args.get("file") or "").strip()
        content = str(args.get("content") or args.get("text") or "")
        append = bool(args.get("append", False))
        return "write_file", {"path": path, "content": content, "append": append}

    # 兼容模型把 git 子命令当作工具名（如 git_log/git_show）
    if lowered.startswith("git_"):
        mapped_subcmd = lowered.replace("_", " ")
        if mapped_subcmd == "git log":
            n = args.get("n") or args.get("limit") or 20
            try:
                n = int(n)
            except Exception:
                n = 20
            command = extract_command_from_args(args) or f"git log --oneline -n {n}"
            return "bash", {"command": command}
        if mapped_subcmd == "git show":
            target = str(args.get("target") or args.get("commit") or "").strip()
            command = extract_command_from_args(args) or (f"git show {target}".strip() if target else "git show")
            return "bash", {"command": command}
        if mapped_subcmd == "git status":
            command = extract_command_from_args(args) or "git status"
            return "bash", {"command": command}
        command = extract_command_from_args(args) or mapped_subcmd
        return "bash", {"command": command}

    # 兼容模型使用 run/shell/execute 作为通用命令工具名
    if lowered in {"run", "shell", "execute", "exec"}:
        command = extract_command_from_args(args)
        if not command and "path" in args:
            maybe_path = str(args.get("path", "")).strip()
            if maybe_path:
                command = f"dir {maybe_path}" if IS_WINDOWS else f"ls {maybe_path}"
        return "bash", {"command": command}

    # 兼容模型把 shell 命令当作工具名的情况（如 dir/type/ls/cat）
    if lowered in {"dir", "type", "ls", "cat", "pwd", "grep", "findstr"}:
        if lowered in {"dir", "ls", "pwd"}:
            path = str(args.get("path", "")).strip()
            depth = args.get("depth")
            command = "dir"
            if path:
                command = f"{command} {path}"
            if depth is not None:
                # cmd.exe 无深度参数，这里给提示但不报错
                command = f"{command}"
            return "bash", {"command": command}
        if lowered in {"type", "cat"}:
            path = str(args.get("path", "")).strip()
            return "bash", {"command": f"type {path}".strip()}
        if lowered in {"grep", "findstr"}:
            pattern = str(args.get("pattern", "")).strip()
            path = str(args.get("path", "")).strip()
            cmd = f"findstr {pattern} {path}".strip()
            return "bash", {"command": cmd}
    return name, args


def make_tool_signature(name: str, args: dict[str, Any]) -> str:
    normalized_input = canonicalize_tool_args_for_loop(name, args)
    try:
        normalized_args = json.dumps(normalized_input, sort_keys=True, ensure_ascii=False)
    except Exception:
        normalized_args = repr(normalized_input)
    return f"{name}::{normalized_args}"


def chat_with_tools(user_query: str, history: list[dict[str, Any]]) -> tuple[str, QueryMetrics]:
    metrics = QueryMetrics()
    cleaned_query = normalize_user_input(user_query)
    messages = compact_history(history) + [{"role": "user", "content": cleaned_query}]
    signature_counts: dict[str, int] = {}

    response = call_model(messages=messages, max_tokens=1024, metrics=metrics)
    current_messages = messages + [{"role": "assistant", "content": response.content}]

    for _ in range(MAX_TOOL_TURNS):
        tool_calls = [b for b in response.content if hasattr(b, "type") and b.type == "tool_use"]
        if not tool_calls:
            break

        normalized_calls = []
        for tc in tool_calls:
            name = getattr(tc, "name", "bash") or "bash"
            args = extract_tool_args(tc)
            normalized_name, normalized_args = normalize_tool_name_and_args(name, args)
            sig = make_tool_signature(normalized_name, normalized_args)
            signature_counts[sig] = signature_counts.get(sig, 0) + 1
            if signature_counts[sig] > MAX_SAME_TOOL_REPEAT:
                metrics.loop_break_reason = (
                    f"same tool repeated > {MAX_SAME_TOOL_REPEAT}: "
                    f"{normalized_name} {normalized_args}"
                )
                return (
                    "Stopped due to loop detection: repeated same tool call with same arguments. "
                    "Please refine your request or provide explicit constraints.",
                    metrics,
                )
            normalized_calls.append((tc, normalized_name, normalized_args))

        tool_results = execute_tools(normalized_calls, metrics)
        current_messages.append({"role": "user", "content": tool_results})
        invalid_count = 0
        for result in tool_results:
            try:
                payload = json.loads(result.get("content", "{}"))
                if payload.get("error") in {"Empty command", "Empty path"}:
                    invalid_count += 1
            except Exception:
                continue
        if invalid_count == len(tool_results):
            return (
                "Tool arguments are invalid/empty in this turn. "
                "Please retry with an explicit command, e.g. `bash: dir /s` on Windows.",
                metrics,
            )

        response = call_model(messages=current_messages, max_tokens=1536, metrics=metrics)
        current_messages.append({"role": "assistant", "content": response.content})

    else:
        return (
            f"Stopped after {MAX_TOOL_TURNS} tool turns to avoid infinite loops. "
            f"Try a narrower query.",
            metrics,
        )

    final_text = safe_extract_text(response.content)
    if final_text != "No text response generated by model.":
        # If provider stopped due to token cap, auto-continue and stitch output.
        if get_stop_reason(response) == "max_tokens":
            assembled = final_text
            continuation_messages = list(current_messages)
            for _ in range(MAX_FINAL_CONTINUATIONS):
                continuation_messages.append({
                    "role": "user",
                    "content": (
                        "Continue from exactly where you stopped. "
                        "Do not repeat prior text. Output only the remaining content."
                    ),
                })
                cont_resp = call_model(
                    messages=continuation_messages,
                    max_tokens=1536,
                    metrics=metrics,
                    allow_tools=False,
                )
                continuation_messages.append({"role": "assistant", "content": cont_resp.content})
                part = safe_extract_text(cont_resp.content)
                if part == "No text response generated by model.":
                    break
                assembled = f"{assembled}\n{part}".strip()
                if get_stop_reason(cont_resp) != "max_tokens":
                    break
            return assembled, metrics
        return final_text, metrics

    # 兜底：如果最后一轮没产出 text，做一次禁止工具的总结请求
    fallback_messages = current_messages + [{
        "role": "user",
        "content": (
            "Now provide the final answer only, based on gathered evidence. "
            "Do not call any tool. Return a concise but complete report."
        ),
    }]
    try:
        fallback = call_model(
            messages=fallback_messages,
            max_tokens=1536,
            metrics=metrics,
            allow_tools=False,
        )
        fallback_text = safe_extract_text(fallback.content)
        if fallback_text != "No text response generated by model.":
            return fallback_text, metrics
        # 最后兜底：给出结构预览，便于定位兼容端返回格式问题
        return (
            "No text response generated by model. "
            f"Raw content preview: {repr(fallback.content)[:600]}",
            metrics,
        )
    except Exception:
        pass

    return final_text, metrics

if __name__ == "__main__":
    print("🚀 强制工具调用 Agent (Windows/Linux 兼容)")
    print(f"📁 当前目录: {os.getcwd()}")

    history: list[dict[str, Any]] = []

    while True:
        try:
            query = normalize_user_input(input("\n🤖 tools >> "))
        except (EOFError, KeyboardInterrupt):
            break
        if query.lower() in ("q", "exit", "quit"):
            break

        if query:
            turn_start = time.perf_counter()
            response, metrics = chat_with_tools(query, history)
            total_ms = (time.perf_counter() - turn_start) * 1000
            print(f"\n✅ {response}")
            print(f"⏱️ {build_metrics_summary(metrics)}, total_ms={total_ms:.1f}")
            history.append({"role": "user", "content": query})
            history.append({"role": "assistant", "content": response})
