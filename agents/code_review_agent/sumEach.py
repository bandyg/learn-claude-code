#!/usr/bin/env python3
"""
Read every file under ./diff, send one-by-one to agent_service /chat,
and write each reply to ./sum/<relative_path>.sum.
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import re
import time
import uuid
from pathlib import Path
from typing import Any

import httpx


BASE_DIR = Path(__file__).resolve().parent
DIFF_DIR = BASE_DIR / "diff"
SUM_DIR = BASE_DIR / "sum"

AGENT_BASE_URL = os.getenv("AGENT_BASE_URL", "http://127.0.0.1:5015").rstrip("/")
CHAT_URL = f"{AGENT_BASE_URL}/chat"
NEW_URL = f"{AGENT_BASE_URL}/new"

USER_ID = os.getenv("AGENT_USER_ID", "code-review-user")
SESSION_ID = os.getenv("AGENT_SESSION_ID", f"sumeach-{uuid.uuid4().hex[:8]}")
REQUEST_TIMEOUT_SEC = float(os.getenv("AGENT_TIMEOUT_SEC", "360"))
MAX_DIFF_CHARS = int(os.getenv("AGENT_MAX_DIFF_CHARS", "12000"))
MAX_RETRIES = int(os.getenv("AGENT_MAX_RETRIES", "2"))

MESSAGE_TEMPLATE = """你是严格代码审查助手。你收到的是 .diff 文件内容（Git unified diff），不是源码全文。

先理解 Git diff 语义（必须遵守）：
1) 以 'diff --git'、'index'、'@@' 开头的是元信息，不是业务代码。
2) 以 '---' 和 '+++' 开头的是文件头，不是业务代码行。
3) 只有 hunk 内以 '-' 或 '+' 开头的行才是变更行：
   - '-' 表示旧版本被删除/替换的行
   - '+' 表示新版本新增/替换后的行
4) 同一 hunk 中 '-' 和 '+' 可能表示“替换”而不是独立两次改动，要按语义成对理解。
5) 如果无法仅凭 diff 证明问题，必须返回空数组，不允许猜测。

硬性规则：
1) 只能分析变更行，不得分析未变更上下文。
2) 不得猜测无法从 DIFF 直接证明的内容。
3) 每条问题必须包含 evidence（原始变更行摘录，保留前缀 '+' 或 '-'）。
4) evidence 必须是单行证据；若需要多条证据，输出多条对象，不要把多行拼在一个 evidence。
5) 不要把“同一内容从 '-' 到 '+' 的等价替换（如空白、顺序、路径风格微调）”误判为 bug。
6) 输出中文，且仅输出 JSON，不要任何额外文字。

输出格式：
{{
  "potential_bugs": [{{"evidence":"", "reason":"", "impact":"", "fix":""}}],
  "potential_risks": [{{"evidence":"", "reason":"", "impact":"", "fix":""}}],
  "possible_improvements": [{{"evidence":"", "reason":"", "benefit":"", "fix":""}}]
}}

如果某一类没有内容，返回空数组 []。

filepath: {filepath}
以下是完整 DIFF 文本（仅基于该文本分析）：
```diff
{diff_text}
```"""

REPAIR_TEMPLATE = """请把下面文本修复并转换为严格 JSON，且仅输出 JSON。
要求：
1) 顶层必须是对象，且仅包含以下 key：potential_bugs, potential_risks, possible_improvements
2) 这三个 key 的值都必须是数组
3) 数组元素是对象，并尽量保留 evidence/reason/impact/fix/benefit 字段
4) 无法确定时返回空数组，不要编造

待修复文本如下：
{raw_reply}
"""


def iter_diff_files(diff_dir: Path) -> list[Path]:
    if not diff_dir.exists():
        raise FileNotFoundError(f"diff directory not found: {diff_dir}")
    files = [p for p in diff_dir.rglob("*") if p.is_file()]
    files.sort()
    return files


def build_sum_path(diff_file: Path) -> Path:
    rel_path = diff_file.relative_to(DIFF_DIR)
    target = SUM_DIR / rel_path
    if target.suffix == ".diff":
        target = target.with_suffix("")
    return target.with_suffix(target.suffix + ".sum")


def _is_failed_sum_file(sum_file: Path) -> bool:
    if not sum_file.exists():
        return False
    text = sum_file.read_text(encoding="utf-8", errors="replace").strip()
    return text.startswith("[ERROR]")


def _filter_failed_diff_files(diff_files: list[Path]) -> list[Path]:
    selected: list[Path] = []
    for diff_file in diff_files:
        sum_file = build_sum_path(diff_file)
        if _is_failed_sum_file(sum_file):
            selected.append(diff_file)
    return selected


def _truncate_diff_text(diff_text: str) -> str:
    if len(diff_text) <= MAX_DIFF_CHARS:
        return diff_text
    return diff_text[:MAX_DIFF_CHARS] + "\n... (truncated)"


def _extract_changed_lines(diff_text: str) -> list[str]:
    changed_lines: list[str] = []
    for line in diff_text.splitlines():
        if line.startswith("+++") or line.startswith("---"):
            continue
        if line.startswith("+") or line.startswith("-"):
            changed_lines.append(line[1:].strip())
    return [ln for ln in changed_lines if ln]


def _normalize_line(text: str) -> str:
    s = text.strip().strip("`").strip()
    if s.startswith("+") or s.startswith("-"):
        s = s[1:].strip()
    return s


def build_chat_payload(file_path: Path, diff_text: str) -> dict[str, Any]:
    message = MESSAGE_TEMPLATE.format(
        filepath=str(file_path),
        diff_text=_truncate_diff_text(diff_text),
    )
    return {
        "user_id": USER_ID,
        "session_id": "",  # runtime injected
        "message": message,
    }


def get_fresh_session_id(client: httpx.Client, current_session_id: str) -> str:
    payload = {"user_id": USER_ID, "session_id": current_session_id}
    response = client.post(NEW_URL, json=payload, timeout=REQUEST_TIMEOUT_SEC)
    response.raise_for_status()
    data = response.json()
    session_id = data.get("session_id", "")
    if not isinstance(session_id, str) or not session_id.strip():
        raise ValueError(f"Invalid session_id from /new: {data}")
    return session_id


def _extract_json_object(text: str) -> dict[str, Any]:
    raw = text.strip()
    candidates: list[str] = [raw]

    # fenced code block candidate
    fence = re.search(r"```(?:json)?\s*([\s\S]*?)```", raw, flags=re.IGNORECASE)
    if fence:
        candidates.append(fence.group(1).strip())

    # first balanced JSON-like object candidate
    start = raw.find("{")
    if start != -1:
        depth = 0
        in_str = False
        escaped = False
        for i, ch in enumerate(raw[start:], start=start):
            if in_str:
                if escaped:
                    escaped = False
                elif ch == "\\":
                    escaped = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    candidates.append(raw[start:i + 1].strip())
                    break

    errors: list[str] = []
    for cand in candidates:
        if not cand:
            continue

        # pass 1: strict json
        try:
            data = json.loads(cand)
            if isinstance(data, dict):
                return data
        except Exception as e:
            errors.append(f"json.loads failed: {e}")

        # pass 2: python-literal style dict (single quotes etc.)
        try:
            py_obj = ast.literal_eval(cand)
            if isinstance(py_obj, dict):
                return py_obj
        except Exception as e:
            errors.append(f"literal_eval failed: {e}")

        # pass 3: backslash fix for invalid escape sequences
        try:
            repaired = cand.replace("\\", "\\\\")
            data = json.loads(repaired)
            if isinstance(data, dict):
                return data
        except Exception as e:
            errors.append(f"json.loads(repaired) failed: {e}")

    raise ValueError("Reply does not contain valid JSON object. " + " | ".join(errors[:3]))


def _validate_review_json(reply_json: dict[str, Any], diff_text: str) -> None:
    required_keys = ["potential_bugs", "potential_risks", "possible_improvements"]
    for key in required_keys:
        if key not in reply_json or not isinstance(reply_json[key], list):
            raise ValueError(f"Missing or invalid key: {key}")

    changed_lines = [_normalize_line(x) for x in _extract_changed_lines(diff_text)]
    changed_set = {x for x in changed_lines if x}
    for key in required_keys:
        for item in reply_json[key]:
            if not isinstance(item, dict):
                raise ValueError(f"{key} item must be object.")
            evidence = str(item.get("evidence", "")).strip()
            if not evidence:
                raise ValueError(f"{key} item missing evidence.")
            evidence_lines = [
                _normalize_line(x) for x in evidence.splitlines()
                if _normalize_line(x)
            ]
            if not evidence_lines:
                raise ValueError(f"{key} item evidence is empty after normalize.")
            for ev in evidence_lines:
                if ev in changed_set:
                    continue
                if any(ev in ch or ch in ev for ch in changed_lines):
                    continue
                raise ValueError(f"Evidence not found in changed lines: {ev}")


def _format_reply_json(reply_json: dict[str, Any]) -> str:
    return json.dumps(reply_json, ensure_ascii=False, indent=2)


def _repair_reply_to_json(
    client: httpx.Client,
    raw_reply: str,
    session_id: str,
) -> dict[str, Any]:
    payload = {
        "user_id": USER_ID,
        "session_id": session_id,
        "message": REPAIR_TEMPLATE.format(raw_reply=raw_reply[:16000]),
    }
    response = client.post(CHAT_URL, json=payload, timeout=REQUEST_TIMEOUT_SEC)
    response.raise_for_status()
    data = response.json()
    repaired = data.get("reply", "")
    if not isinstance(repaired, str):
        raise ValueError("Repair reply is not string.")
    return _extract_json_object(repaired)


def call_chat(client: httpx.Client, file_path: Path, diff_text: str, session_id: str) -> str:
    payload = build_chat_payload(file_path, diff_text)
    payload["session_id"] = session_id
    response = client.post(CHAT_URL, json=payload, timeout=REQUEST_TIMEOUT_SEC)
    response.raise_for_status()
    data = response.json()
    reply = data.get("reply", "")
    if not isinstance(reply, str):
        raise ValueError(f"Invalid reply type for {file_path}: {type(reply)}")
    try:
        reply_json = _extract_json_object(reply)
    except Exception:
        reply_json = _repair_reply_to_json(client=client, raw_reply=reply, session_id=session_id)
    _validate_review_json(reply_json, diff_text)
    return _format_reply_json(reply_json)


def save_sum(diff_file: Path, reply: str) -> Path:
    output_path = build_sum_path(diff_file)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(reply, encoding="utf-8")
    return output_path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Summarize each diff file via agent_service")
    parser.add_argument(
        "--session-mode",
        choices=["per_file_reset", "single_run"],
        default=os.getenv("AGENT_SESSION_MODE", "per_file_reset"),
        help="per_file_reset: call /new for each file; single_run: reuse one session.",
    )
    parser.add_argument(
        "--only-failed",
        action="store_true",
        help="Only reprocess diff files whose corresponding .sum currently starts with [ERROR].",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    diff_files = iter_diff_files(DIFF_DIR)
    if args.only_failed:
        diff_files = _filter_failed_diff_files(diff_files)
    print(f"Found {len(diff_files)} diff files")
    print(f"Using /chat endpoint: {CHAT_URL}")
    print(f"Using /new endpoint: {NEW_URL}")
    print(
        f"user_id={USER_ID}, initial_session_id={SESSION_ID}, "
        f"session_mode={args.session_mode}"
    )

    current_session_id = SESSION_ID
    with httpx.Client() as client:
        for idx, diff_file in enumerate(diff_files, start=1):
            try:
                diff_text = diff_file.read_text(encoding="utf-8", errors="replace")
                if args.session_mode == "per_file_reset":
                    current_session_id = get_fresh_session_id(client, current_session_id)
                last_error = ""
                reply = ""
                for attempt in range(1, MAX_RETRIES + 1):
                    try:
                        reply = call_chat(
                            client=client,
                            file_path=diff_file.resolve(),
                            diff_text=diff_text,
                            session_id=current_session_id,
                        )
                        break
                    except Exception as e:
                        last_error = str(e)
                        if "timed out" in last_error.lower() and attempt < MAX_RETRIES:
                            time.sleep(min(2 * attempt, 5))
                        if attempt == MAX_RETRIES:
                            raise
                if not reply:
                    raise ValueError(f"Empty validated reply. last_error={last_error}")
                output_path = save_sum(diff_file, reply)
                print(
                    f"[{idx}/{len(diff_files)}] saved: {output_path} "
                    f"(session_id={current_session_id})"
                )
            except Exception as e:
                output_path = save_sum(diff_file, f"[ERROR] {e}")
                print(f"[{idx}/{len(diff_files)}] failed: {diff_file} -> {e}")
                print(f"error saved to: {output_path}")


if __name__ == "__main__":
    main()
