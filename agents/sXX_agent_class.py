#!/usr/bin/env python3
# sXX_agent_class.py - Full Agent Class
"""
A complete Agent class that unifies all mechanisms from s01-s12.

Components:
  1. LLM Client & Model config
  2. System Prompt builder (with memory + skills injection)
  3. Tool definitions + dispatch handlers
  4. Permission pipeline (deny → mode → allow → ask)
  5. Hook system (SessionStart, PreToolUse, PostToolUse)
  6. Memory manager (cross-session persistence)
  7. Todo manager (session-scoped planning)
  8. Task manager (durable work graph)
  9. Context compaction (micro-compact + auto-summarize)
  10. Background task runner
  11. Agent loop with full pipeline
"""

import json
import os
import re
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from queue import Queue

from anthropic import Anthropic
from dotenv import load_dotenv

load_dotenv(override=True)

# === SECTION: environment ===
WORKDIR = Path.cwd()
client = Anthropic(base_url=os.getenv("ANTHROPIC_BASE_URL"))
MODEL = os.environ["MODEL_ID"]

# === SECTION: constants ===
TOOL_RESULTS_DIR = WORKDIR / ".task_outputs" / "tool-results"
PERSIST_THRESHOLD = 30000
PREVIEW_CHARS = 2000
KEEP_RECENT = 3
CONTEXT_LIMIT = 50000
TOKEN_THRESHOLD = 100000
TRANSCRIPT_DIR = WORKDIR / ".transcripts"
MEMORY_DIR = WORKDIR / ".memory"
TASKS_DIR = WORKDIR / ".tasks"
TEAM_DIR = WORKDIR / ".team"
INBOX_DIR = TEAM_DIR / "inbox"

# === SECTION: state containers ===
@dataclass
class LoopState:
    messages: list
    turn_count: int = 1
    has_compacted: bool = False
    last_summary: str = ""


@dataclass
class CompactState:
    has_compacted: bool = False
    last_summary: str = ""
    recent_files: list[str] = field(default_factory=list)


# === SECTION: path safety ===
def safe_path(p: str) -> Path:
    path = (WORKDIR / p).resolve()
    if not path.is_relative_to(WORKDIR):
        raise ValueError(f"Path escapes workspace: {p}")
    return path


# === SECTION: tool implementations ===
def run_bash(command: str, tool_use_id: str = "") -> str:
    dangerous = ["rm -rf /", "sudo", "shutdown", "reboot", "> /dev/"]
    if any(d in command for d in dangerous):
        return "Error: Dangerous command blocked"
    try:
        r = subprocess.run(command, shell=True, cwd=WORKDIR,
                           capture_output=True, text=True, timeout=120)
        out = (r.stdout + r.stderr).strip()
        if not out:
            return "(no output)"
        return _maybe_persist(tool_use_id, out, PERSIST_THRESHOLD)
    except subprocess.TimeoutExpired:
        return "Error: Timeout (120s)"


def run_read(path: str, tool_use_id: str = "", limit: int = None) -> str:
    try:
        lines = safe_path(path).read_text().splitlines()
        if limit and limit < len(lines):
            lines = lines[:limit] + [f"... ({len(lines) - limit} more)"]
        out = "\n".join(lines)
        return _maybe_persist(tool_use_id, out)
    except Exception as e:
        return f"Error: {e}"


def run_write(path: str, content: str) -> str:
    try:
        fp = safe_path(path)
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(content)
        return f"Wrote {len(content)} bytes to {path}"
    except Exception as e:
        return f"Error: {e}"


def run_edit(path: str, old_text: str, new_text: str) -> str:
    try:
        fp = safe_path(path)
        c = fp.read_text()
        if old_text not in c:
            return f"Error: Text not found in {path}"
        fp.write_text(c.replace(old_text, new_text, 1))
        return f"Edited {path}"
    except Exception as e:
        return f"Error: {e}"


# === SECTION: output persistence (s06) ===
def _persist_tool_result(tool_use_id: str, content: str) -> Path:
    TOOL_RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    safe_id = re.sub(r"[^a-zA-Z0-9_.-]", "_", tool_use_id or "unknown")
    path = TOOL_RESULTS_DIR / f"{safe_id}.txt"
    if not path.exists():
        path.write_text(content)
    return path.relative_to(WORKDIR)


def _maybe_persist(tool_use_id: str, output: str, trigger: int = PERSIST_THRESHOLD) -> str:
    if len(output) <= trigger:
        return output
    stored_path = _persist_tool_result(tool_use_id, output)
    preview = output[:PREVIEW_CHARS]
    return (
        f"<persisted-output>\n"
        f"Full output saved to: {stored_path}\n"
        f"Preview:\n{preview}\n"
        f"</persisted-output>"
    )


# === SECTION: memory (s09) ===
MEMORY_TYPES = ("user", "feedback", "project", "reference")


class MemoryManager:
    def __init__(self, memory_dir: Path = None):
        self.memory_dir = memory_dir or MEMORY_DIR
        self.memories = {}

    def load_all(self):
        self.memories = {}
        if not self.memory_dir.exists():
            return
        for md_file in sorted(self.memory_dir.glob("*.md")):
            if md_file.name == "MEMORY.md":
                continue
            parsed = self._parse_frontmatter(md_file.read_text())
            if parsed:
                name = parsed.get("name", md_file.stem)
                self.memories[name] = {
                    "description": parsed.get("description", ""),
                    "type": parsed.get("type", "project"),
                    "content": parsed.get("content", ""),
                    "file": md_file.name,
                }

    def load_memory_prompt(self) -> str:
        if not self.memories:
            return ""
        sections = ["# Memories (persistent across sessions)", ""]
        for mem_type in MEMORY_TYPES:
            typed = {k: v for k, v in self.memories.items() if v["type"] == mem_type}
            if not typed:
                continue
            sections.append(f"## [{mem_type}]")
            for name, mem in typed.items():
                sections.append(f"### {name}: {mem['description']}")
                if mem["content"].strip():
                    sections.append(mem["content"].strip())
                sections.append("")
        return "\n".join(sections)

    def save_memory(self, name: str, description: str, mem_type: str, content: str) -> str:
        if mem_type not in MEMORY_TYPES:
            return f"Error: type must be one of {MEMORY_TYPES}"
        safe_name = re.sub(r"[^a-zA-Z0-9_-]", "_", name.lower())
        if not safe_name:
            return "Error: invalid memory name"
        self.memory_dir.mkdir(parents=True, exist_ok=True)
        frontmatter = (
            f"---\nname: {name}\ndescription: {description}\ntype: {mem_type}\n---\n{content}\n"
        )
        file_path = self.memory_dir / f"{safe_name}.md"
        file_path.write_text(frontmatter)
        self.memories[name] = {
            "description": description, "type": mem_type,
            "content": content, "file": file_path.name,
        }
        return f"Saved memory '{name}' [{mem_type}]"

    def _parse_frontmatter(self, text: str) -> dict | None:
        match = re.match(r"^---\s*\n(.*?)\n---\s*\n(.*)", text, re.DOTALL)
        if not match:
            return None
        result = {"content": match.group(2).strip()}
        for line in match.group(1).splitlines():
            if ":" in line:
                k, _, v = line.partition(":")
                result[k.strip()] = v.strip()
        return result


# === SECTION: todos (s03) ===
@dataclass
class PlanItem:
    content: str
    status: str = "pending"
    active_form: str = ""


class TodoManager:
    def __init__(self):
        self.items: list[PlanItem] = []
        self.rounds_since_update: int = 0

    def update(self, items: list) -> str:
        if len(items) > 20:
            raise ValueError("Max 20 todos")
        normalized, ip = [], 0
        for i, item in enumerate(items):
            content = str(item.get("content", "")).strip()
            status = str(item.get("status", "pending")).lower()
            af = str(item.get("activeForm", "")).strip()
            if not content:
                raise ValueError(f"Item {i}: content required")
            if status not in ("pending", "in_progress", "completed"):
                raise ValueError(f"Item {i}: invalid status")
            if status == "in_progress":
                ip += 1
            normalized.append(PlanItem(content=content, status=status, active_form=af))
        if ip > 1:
            raise ValueError("Only one in_progress allowed")
        self.items = normalized
        self.rounds_since_update = 0
        return self.render()

    def render(self) -> str:
        if not self.items:
            return "No todos."
        lines = []
        for item in self.items:
            m = {"completed": "[x]", "in_progress": "[>]", "pending": "[ ]"}.get(item.status, "[?]")
            suffix = f" <- {item.active_form}" if item.status == "in_progress" and item.active_form else ""
            lines.append(f"{m} {item.content}{suffix}")
        done = sum(1 for t in self.items if t.status == "completed")
        lines.append(f"\n({done}/{len(self.items)} completed)")
        return "\n".join(lines)

    def has_open_items(self) -> bool:
        return any(item.status != "completed" for item in self.items)

    def note_round(self, used_todo: bool):
        if used_todo:
            self.rounds_since_update = 0
        else:
            self.rounds_since_update += 1


# === SECTION: tasks (s12) ===
class TaskManager:
    def __init__(self, tasks_dir: Path = None):
        self.dir = tasks_dir or TASKS_DIR
        self.dir.mkdir(exist_ok=True)
        self._next_id = self._max_id() + 1

    def _max_id(self) -> int:
        ids = [int(f.stem.split("_")[1]) for f in self.dir.glob("task_*.json")]
        return max(ids) if ids else 0

    def _load(self, task_id: int) -> dict:
        p = self.dir / f"task_{task_id}.json"
        if not p.exists():
            raise ValueError(f"Task {task_id} not found")
        return json.loads(p.read_text())

    def _save(self, task: dict):
        (self.dir / f"task_{task['id']}.json").write_text(json.dumps(task, indent=2))

    def create(self, subject: str, description: str = "") -> str:
        task = {
            "id": self._next_id, "subject": subject, "description": description,
            "status": "pending", "blockedBy": [], "blocks": [], "owner": "",
        }
        self._save(task)
        self._next_id += 1
        return json.dumps(task, indent=2)

    def update(self, task_id: int, status: str = None, owner: str = None,
               add_blocked_by: list = None, add_blocks: list = None) -> str:
        task = self._load(task_id)
        if owner is not None:
            task["owner"] = owner
        if status:
            if status not in ("pending", "in_progress", "completed", "deleted"):
                raise ValueError(f"Invalid: {status}")
            task["status"] = status
            if status == "completed":
                self._clear_dependency(task_id)
        if add_blocked_by:
            task["blockedBy"] = list(set(task["blockedBy"] + add_blocked_by))
        if add_blocks:
            task["blocks"] = list(set(task["blocks"] + add_blocks))
        self._save(task)
        return json.dumps(task, indent=2)

    def _clear_dependency(self, completed_id: int):
        for f in self.dir.glob("task_*.json"):
            t = json.loads(f.read_text())
            if completed_id in t.get("blockedBy", []):
                t["blockedBy"].remove(completed_id)
                self._save(t)

    def list_all(self) -> str:
        tasks = [json.loads(f.read_text()) for f in sorted(self.dir.glob("task_*.json"))]
        if not tasks:
            return "No tasks."
        lines = []
        for t in tasks:
            m = {"pending": "[ ]", "in_progress": "[>]", "completed": "[x]"}.get(t["status"], "[?]")
            blocked = f" (blocked by: {t['blockedBy']})" if t.get("blockedBy") else ""
            owner = f" @{t['owner']}" if t.get("owner") else ""
            lines.append(f"{m} #{t['id']}: {t['subject']}{owner}{blocked}")
        return "\n".join(lines)


# === SECTION: background tasks (s13) ===
class BackgroundManager:
    def __init__(self):
        self.tasks = {}
        self.notifications = Queue()

    def run(self, command: str, timeout: int = 120) -> str:
        tid = str(uuid.uuid4())[:8]
        self.tasks[tid] = {"status": "running", "command": command, "result": None}
        threading.Thread(target=self._exec, args=(tid, command, timeout), daemon=True).start()
        return f"Background task {tid} started"

    def _exec(self, tid: str, command: str, timeout: int):
        try:
            r = subprocess.run(command, shell=True, cwd=WORKDIR,
                               capture_output=True, text=True, timeout=timeout)
            out = (r.stdout + r.stderr).strip()[:50000]
            self.tasks[tid].update({"status": "completed", "result": out or "(no output)"})
        except Exception as e:
            self.tasks[tid].update({"status": "error", "result": str(e)})
        self.notifications.put({
            "task_id": tid, "status": self.tasks[tid]["status"],
            "result": self.tasks[tid]["result"][:500],
        })

    def check(self, tid: str = None) -> str:
        if tid:
            t = self.tasks.get(tid)
            return f"[{t['status']}] {t.get('result', '(running)')}" if t else f"Unknown: {tid}"
        return "\n".join(f"{k}: [{v['status']}] {v['command'][:60]}" for k, v in self.tasks.items()) or "No bg tasks."

    def drain(self) -> list:
        notifs = []
        while not self.notifications.empty():
            notifs.append(self.notifications.get_nowait())
        return notifs


# === SECTION: permissions (s07) ===
MODES = ("default", "plan", "auto")
WRITE_TOOLS = {"write_file", "edit_file", "bash"}


class BashSecurityValidator:
    VALIDATORS = [
        ("shell_metachar", r"[;&|`$]"),
        ("sudo", r"\bsudo\b"),
        ("rm_rf", r"\brm\s+(-[a-zA-Z]*)?rf"),
        ("cmd_substitution", r"\$\("),
    ]

    def validate(self, command: str) -> list:
        return [(n, p) for n, p in self.VALIDATORS if re.search(p, command)]

    def is_safe(self, command: str) -> bool:
        return not self.validate(command)

    def describe(self, command: str) -> str:
        failures = self.validate(command)
        if not failures:
            return "No issues"
        return "Security flags: " + ", ".join(f"{n} ({p})" for n, p in failures)


class PermissionManager:
    DEFAULT_RULES = [
        {"tool": "bash", "content": "rm -rf /", "behavior": "deny"},
        {"tool": "bash", "content": "sudo *", "behavior": "deny"},
        {"tool": "read_file", "path": "*", "behavior": "allow"},
    ]

    def __init__(self, mode: str = "default", rules: list = None):
        if mode not in MODES:
            raise ValueError(f"Unknown mode: {mode}")
        self.mode = mode
        self.rules = rules or list(self.DEFAULT_RULES)
        self.validator = BashSecurityValidator()

    def check(self, tool_name: str, tool_input: dict) -> dict:
        if tool_name == "bash":
            failures = self.validator.validate(tool_input.get("command", ""))
            if failures:
                severe = {"sudo", "rm_rf"}
                if any(f[0] in severe for f in failures):
                    return {"behavior": "deny", "reason": self.validator.describe(tool_input["command"])}
                return {"behavior": "ask", "reason": self.validator.describe(tool_input["command"])}

        for rule in self.rules:
            if rule["behavior"] == "deny" and self._matches(rule, tool_name, tool_input):
                return {"behavior": "deny", "reason": f"Blocked: {rule}"}

        if self.mode == "plan" and tool_name in WRITE_TOOLS:
            return {"behavior": "deny", "reason": "Plan mode: writes blocked"}
        if self.mode == "auto" and tool_name not in WRITE_TOOLS:
            return {"behavior": "allow", "reason": "Auto mode: read-only"}

        for rule in self.rules:
            if rule["behavior"] == "allow" and self._matches(rule, tool_name, tool_input):
                return {"behavior": "allow", "reason": f"Allowed: {rule}"}

        return {"behavior": "ask", "reason": f"No rule for {tool_name}"}

    def ask_user(self, tool_name: str, tool_input: dict) -> bool:
        preview = json.dumps(tool_input, ensure_ascii=False)[:200]
        print(f"\n  [Permission] {tool_name}: {preview}")
        try:
            answer = input("  Allow? (y/n/always): ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            return False
        if answer == "always":
            self.rules.append({"tool": tool_name, "path": "*", "behavior": "allow"})
            return True
        return answer in ("y", "yes")

    def _matches(self, rule: dict, tool_name: str, tool_input: dict) -> bool:
        if rule.get("tool") and rule["tool"] != "*" and rule["tool"] != tool_name:
            return False
        if "path" in rule and rule["path"] != "*":
            if not Path(tool_input.get("path", "")).match(rule["path"]):
                return False
        if "content" in rule:
            if not re.match(rule["content"], tool_input.get("command", "")):
                return False
        return True


# === SECTION: hooks (s08) ===
HOOK_EVENTS = ("PreToolUse", "PostToolUse", "SessionStart")


class HookManager:
    def __init__(self, config_path: Path = None):
        self.hooks = {e: [] for e in HOOK_EVENTS}
        config_path = config_path or (WORKDIR / ".hooks.json")
        if config_path.exists():
            try:
                config = json.loads(config_path.read_text())
                for event in HOOK_EVENTS:
                    self.hooks[event] = config.get("hooks", {}).get(event, [])
            except Exception as e:
                print(f"[Hook config error: {e}]")

    def run_hooks(self, event: str, context: dict = None) -> dict:
        result = {"blocked": False, "messages": []}
        hooks = self.hooks.get(event, [])
        for hook_def in hooks:
            matcher = hook_def.get("matcher")
            if matcher and context:
                if matcher != "*" and matcher != context.get("tool_name", ""):
                    continue
            command = hook_def.get("command", "")
            if not command:
                continue
            env = dict(os.environ)
            if context:
                env["HOOK_EVENT"] = event
                env["HOOK_TOOL_NAME"] = context.get("tool_name", "")
                env["HOOK_TOOL_INPUT"] = json.dumps(context.get("tool_input", {}), ensure_ascii=False)[:10000]
            try:
                r = subprocess.run(command, shell=True, cwd=WORKDIR, env=env,
                                   capture_output=True, text=True, timeout=30)
                if r.returncode == 0:
                    if r.stdout.strip():
                        print(f"  [hook:{event}] {r.stdout.strip()[:100]}")
                elif r.returncode == 1:
                    result["blocked"] = True
                    result["block_reason"] = r.stderr.strip() or "Blocked"
                elif r.returncode == 2:
                    msg = r.stderr.strip()
                    if msg:
                        result["messages"].append(msg)
            except subprocess.TimeoutExpired:
                print(f"  [hook:{event}] Timeout")
            except Exception as e:
                print(f"  [hook:{event}] Error: {e}")
        return result


# === SECTION: compaction (s06) ===
def _track_recent_file(state: CompactState, path: str) -> None:
    if path in state.recent_files:
        state.recent_files.remove(path)
    state.recent_files.append(path)
    if len(state.recent_files) > 5:
        state.recent_files[:] = state.recent_files[-5:]


def _micro_compact(messages: list) -> list:
    tool_results = []
    for msg in messages:
        if msg["role"] == "user" and isinstance(msg.get("content"), list):
            for part in msg["content"]:
                if isinstance(part, dict) and part.get("type") == "tool_result":
                    tool_results.append(part)
    if len(tool_results) <= KEEP_RECENT:
        return messages
    for part in tool_results[:-KEEP_RECENT]:
        if not isinstance(part.get("content"), str) or len(part["content"]) <= 120:
            continue
        part["content"] = "[Earlier tool result compacted]"
    return messages


def _auto_compact(messages: list, state: CompactState, focus: str = None) -> list:
    TRANSCRIPT_DIR.mkdir(exist_ok=True)
    path = TRANSCRIPT_DIR / f"transcript_{int(time.time())}.jsonl"
    with open(path, "w") as f:
        for msg in messages:
            f.write(json.dumps(msg, default=str) + "\n")
    conversation = json.dumps(messages, default=str)[:80000]
    prompt = (
        "Summarize for continuity:\n"
        "1) Task overview and success criteria\n"
        "2) Current state: completed work, files touched\n"
        "3) Key decisions and discoveries\n"
        "4) Next steps and blockers\n"
        "Be concise.\n\n" + conversation
    )
    resp = client.messages.create(model=MODEL, messages=[{"role": "user", "content": prompt}], max_tokens=4000)
    summary = resp.content[0].text
    state.has_compacted = True
    state.last_summary = summary
    continuation = (
        "Session continued from prior conversation.\n\n"
        f"{summary}\n\nContinue without asking questions."
    )
    if focus:
        continuation += f"\n\nFocus: {focus}"
    if state.recent_files:
        continuation += "\n\nRecent files: " + ", ".join(state.recent_files)
    return [{"role": "user", "content": continuation}]


def _estimate_tokens(messages: list) -> int:
    return len(json.dumps(messages, default=str)) // 4


# === SECTION: system prompt builder ===
def build_system_prompt(agent) -> str:
    parts = [f"You are a coding agent at {WORKDIR}. Use tools to solve tasks."]
    mem_prompt = agent.memory.load_memory_prompt()
    if mem_prompt:
        parts.append(mem_prompt)
    if agent.skills.descriptions():
        parts.append(f"Skills: {agent.skills.descriptions()}")
    return "\n\n".join(parts)


# === SECTION: skill loader (s05) ===
class SkillLoader:
    def __init__(self, skills_dir: Path = WORKDIR / "skills"):
        self.skills = {}
        if skills_dir.exists():
            for f in sorted(skills_dir.rglob("SKILL.md")):
                text = f.read_text()
                match = re.match(r"^---\n(.*?)\n---\n(.*)", text, re.DOTALL)
                meta, body = {}, text
                if match:
                    for line in match.group(1).strip().splitlines():
                        if ":" in line:
                            k, v = line.split(":", 1)
                            meta[k.strip()] = v.strip()
                    body = match.group(2).strip()
                name = meta.get("name", f.parent.name)
                self.skills[name] = {"meta": meta, "body": body}

    def descriptions(self) -> str:
        if not self.skills:
            return ""
        return "\n".join(f"  - {n}: {s['meta'].get('description', '-')}" for n, s in self.skills.items())

    def load(self, name: str) -> str:
        s = self.skills.get(name)
        if not s:
            return f"Error: Unknown skill '{name}'"
        return f"<skill name=\"{name}\">\n{s['body']}\n</skill>"


# === SECTION: tool dispatch ===
TOOL_HANDLERS = {
    "bash":          lambda **kw: run_bash(kw["command"], kw.get("tool_use_id", "")),
    "read_file":     lambda **kw: run_read(kw["path"], kw.get("tool_use_id", ""), kw.get("limit")),
    "write_file":    lambda **kw: run_write(kw["path"], kw["content"]),
    "edit_file":     lambda **kw: run_edit(kw["path"], kw["old_text"], kw["new_text"]),
    "TodoWrite":     lambda **kw: agent.todo.update(kw["items"]),
    "load_skill":    lambda **kw: agent.skills.load(kw["name"]),
    "task_create":   lambda **kw: agent.tasks.create(kw["subject"], kw.get("description", "")),
    "task_update":   lambda **kw: agent.tasks.update(kw["task_id"], kw.get("status"), kw.get("owner")),
    "task_list":     lambda **kw: agent.tasks.list_all(),
    "save_memory":    lambda **kw: agent.memory.save_memory(kw["name"], kw["description"], kw["type"], kw["content"]),
    "background_run": lambda **kw: agent.bg.run(kw["command"], kw.get("timeout", 120)),
    "check_background": lambda **kw: agent.bg.check(kw.get("task_id")),
}

TOOLS = [
    {"name": "bash", "description": "Run a shell command.",
     "input_schema": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}},
    {"name": "read_file", "description": "Read file contents.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "limit": {"type": "integer"}}, "required": ["path"]}},
    {"name": "write_file", "description": "Write content to a file.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}},
    {"name": "edit_file", "description": "Replace exact text in a file.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "old_text": {"type": "string"}, "new_text": {"type": "string"}}, "required": ["path", "old_text", "new_text"]}},
    {"name": "TodoWrite", "description": "Update session plan.",
     "input_schema": {"type": "object", "properties": {"items": {"type": "array", "items": {"type": "object", "properties": {"content": {"type": "string"}, "status": {"type": "string", "enum": ["pending", "in_progress", "completed"]}, "activeForm": {"type": "string"}}, "required": ["content", "status"]}}}, "required": ["items"]}},
    {"name": "load_skill", "description": "Load specialized knowledge.",
     "input_schema": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}},
    {"name": "task_create", "description": "Create a persistent task.",
     "input_schema": {"type": "object", "properties": {"subject": {"type": "string"}, "description": {"type": "string"}}, "required": ["subject"]}},
    {"name": "task_update", "description": "Update a task.",
     "input_schema": {"type": "object", "properties": {"task_id": {"type": "integer"}, "status": {"type": "string", "enum": ["pending", "in_progress", "completed", "deleted"]}, "owner": {"type": "string"}}, "required": ["task_id"]}},
    {"name": "task_list", "description": "List all tasks.",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "save_memory", "description": "Save persistent memory.",
     "input_schema": {"type": "object", "properties": {"name": {"type": "string"}, "description": {"type": "string"}, "type": {"type": "string", "enum": ["user", "feedback", "project", "reference"]}, "content": {"type": "string"}}, "required": ["name", "description", "type", "content"]}},
    {"name": "background_run", "description": "Run command in background.",
     "input_schema": {"type": "object", "properties": {"command": {"type": "string"}, "timeout": {"type": "integer"}}, "required": ["command"]}},
    {"name": "check_background", "description": "Check background task.",
     "input_schema": {"type": "object", "properties": {"task_id": {"type": "string"}}}},

]


# === SECTION: Agent class ===
class Agent:
    """
    Full-featured coding agent with all mechanisms combined.

    Components wired together:
      - LLM client + model
      - Tool definitions + dispatch
      - Permission pipeline
      - Hook system
      - Memory (cross-session)
      - Todo (session plan)
      - Task (durable work graph)
      - Background runner
      - Context compaction
      - System prompt assembly
    """

    def __init__(self, mode: str = "default"):
        # Core
        self.client = client
        self.model = MODEL
        self.workdir = WORKDIR

        # Subsystems
        self.memory = MemoryManager()
        self.todo = TodoManager()
        self.tasks = TaskManager()
        self.bg = BackgroundManager()
        self.perms = PermissionManager(mode=mode)
        self.hooks = HookManager()
        self.skills = SkillLoader()

        # Compact state
        self.compact_state = CompactState()

        # REPL history (session-scoped)
        self.messages = []

    # -- public API --
    def run(self, prompt: str) -> str:
        """Single prompt → response. Accumulates in messages."""
        self.messages.append({"role": "user", "content": prompt})
        self._agent_loop()
        return self._extract_text(self.messages[-1]["content"])

    def reset(self):
        """Clear session history, keep persistent state."""
        self.messages = []

    # -- internal loop --
    def _agent_loop(self):
        """The full agent loop: compact → hooks → LLM → permissions → execute → repeat."""
        rounds_without_todo = 0
        while True:
            # s06: micro-compact + auto-compact
            _micro_compact(self.messages)
            if _estimate_tokens(self.messages) > TOKEN_THRESHOLD:
                print("[auto-compact]")
                self.messages[:] = _auto_compact(self.messages, self.compact_state)

            # s13: drain background notifications
            notifs = self.bg.drain()
            if notifs:
                txt = "\n".join(f"[bg:{n['task_id']}] {n['status']}" for n in notifs)
                self.messages.append({"role": "user", "content": f"<background-results>\n{txt}\n</background-results>"})
                self.messages.append({"role": "assistant", "content": "Noted background results."})

            # Build system prompt
            system = build_system_prompt(self)

            # LLM call
            response = self.client.messages.create(
                model=self.model, system=system, messages=self.messages,
                tools=TOOLS, max_tokens=8000,
            )
            self.messages.append({"role": "assistant", "content": response.content})

            if response.stop_reason != "tool_use":
                return

            # Execute tools
            results = []
            used_todo = False
            for block in response.content:
                if block.type != "tool_use":
                    continue

                tool_input = dict(block.input or {})
                tool_input["tool_use_id"] = block.id

                # s08: PreToolUse hooks
                ctx = {"tool_name": block.name, "tool_input": tool_input}
                pre = self.hooks.run_hooks("PreToolUse", ctx)
                for msg in pre.get("messages", []):
                    results.append({"type": "tool_result", "tool_use_id": block.id,
                                    "content": f"[Hook]: {msg}"})
                if pre.get("blocked"):
                    results.append({"type": "tool_result", "tool_use_id": block.id,
                                    "content": f"Blocked: {pre['block_reason']}"})
                    continue

                # s07: Permission check
                decision = self.perms.check(block.name, tool_input)
                if decision["behavior"] == "deny":
                    output = f"Permission denied: {decision['reason']}"
                    print(f"  [DENIED] {block.name}")
                elif decision["behavior"] == "ask":
                    if not self.perms.ask_user(block.name, tool_input):
                        output = "Permission denied by user"
                        print(f"  [USER DENIED] {block.name}")
                    else:
                        output = self._dispatch(block.name, tool_input)
                        print(f"> {block.name}: {str(output)[:200]}")
                else:
                    output = self._dispatch(block.name, tool_input)
                    print(f"> {block.name}: {str(output)[:200]}")

                results.append({"type": "tool_result", "tool_use_id": block.id, "content": str(output)})

                if block.name == "TodoWrite":
                    used_todo = True

            # s03: todo reminder
            self.todo.note_round(used_todo)
            if self.todo.has_open_items() and self.todo.rounds_since_update >= 3:
                results.insert(0, {"type": "text", "text": "<reminder>Update your todos.</reminder>"})

            self.messages.append({"role": "user", "content": results})

    def _dispatch(self, name: str, tool_input: dict) -> str:
        handler = TOOL_HANDLERS.get(name)
        try:
            return handler(**tool_input) if handler else f"Unknown: {name}"
        except Exception as e:
            return f"Error: {e}"

    def _extract_text(self, content) -> str:
        if not isinstance(content, list):
            return ""
        texts = []
        for block in content:
            txt = getattr(block, "text", None)
            if txt:
                texts.append(txt)
        return "\n".join(texts).strip()


# === SECTION: REPL ===
if __name__ == "__main__":
    print("Full Agent initialized. Commands: /compact /tasks /reset /mode <mode>")
    agent = Agent()

    # s09: load memories at startup
    agent.memory.load_all()

    # s08: fire SessionStart hooks
    agent.hooks.run_hooks("SessionStart", {"tool_name": "", "tool_input": {}})

    while True:
        try:
            query = input("\033[36magent >> \033[0m")
        except (EOFError, KeyboardInterrupt):
            break
        if query.strip().lower() in ("q", "exit", ""):
            break
        if query.strip() == "/compact":
            if agent.messages:
                print("[compact]")
                agent.messages[:] = _auto_compact(agent.messages, agent.compact_state)
            continue
        if query.strip() == "/tasks":
            print(agent.tasks.list_all())
            continue
        if query.strip() == "/reset":
            agent.reset()
            print("[session reset]")
            continue
        if query.startswith("/mode"):
            parts = query.split()
            if len(parts) == 2 and parts[1] in MODES:
                agent.perms.mode = parts[1]
                print(f"[mode: {parts[1]}]")
            continue

        response = agent.run(query)
        if response:
            print(response)
        print()
