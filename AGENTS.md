# AGENTS.md - Agentic Coding Guidelines for learn-claude-code

## Project Overview

This is a **teaching repository** that builds a coding agent harness from scratch (Python).
Each `agents/s*.py` file is a self-contained chapter implementation - not a library.
Agents operate as REPL loops that read input, call the LLM, execute tools, and return results.

---

## Build / Run / Test Commands

### Running Agents
```sh
# Install dependencies
pip install -r requirements.txt

# Copy and configure environment
cp .env.example .env
# Edit .env: set ANTHROPIC_API_KEY (or ANTHROPIC_BASE_URL for compatible endpoints)
# Set MODEL_ID (e.g., "claude-sonnet-4-20250514")

# Run individual chapter agents
python agents/s01_agent_loop.py
python agents/s02_tool_use.py
python agents/s03_todo_write.py
# ... through s19

# Run the full combined agent
python agents/s_full.py
```

### Web Interface (optional)
```sh
cd web && npm install && npm run dev
```

### No Formal Test Suite
This is a teaching codebase - there are **no pytest tests or CI pipelines**.
Verify changes by running the agent REPL and interacting manually.

---

## Code Style Guidelines

### File Structure
- **Shebang**: `#!/usr/bin/env python3` at top of executable scripts
- **Module docstring**: Triple-quoted explanation of what this file implements
- **Section comments**: `# === SECTION: name ===` to divide major logic blocks
- **One class per major section** when state needs to persist; standalone functions otherwise

### Imports
```python
# Standard library first, then third-party, then local
import os
import subprocess
import uuid
from pathlib import Path
from dataclasses import dataclass, field

from anthropic import Anthropic
from dotenv import load_dotenv
```

### Type Annotations
- Use modern Python type hints: `str | None`, `list[PlanItem]`, `dict[str, Any]`
- Return types on public functions: `def run_bash(command: str) -> str:`
- `dataclass` for structured state containers

### Naming Conventions
| Thing | Convention | Example |
|---|---|---|
| Functions/methods | `snake_case` | `run_bash`, `safe_path` |
| Classes | `PascalCase` | `TodoManager`, `BackgroundManager` |
| Constants | `SCREAMING_SNAKE` | `TOKEN_THRESHOLD`, `VALID_MSG_TYPES` |
| Instance variables | `snake_case` | `self.state`, `self.items` |
| Private helpers | `_leading_underscore` | `_persist_tool_result`, `_load` |

### Path Handling (Mandatory)
```python
def safe_path(p: str) -> Path:
    """Reject paths that escape WORKDIR to prevent directory traversal."""
    path = (WORKDIR / p).resolve()
    if not path.is_relative_to(WORKDIR):
        raise ValueError(f"Path escapes workspace: {p}")
    return path
```
- All file operations MUST go through `safe_path()` or equivalent validation
- Never trust user-supplied paths directly

### Error Handling
```python
# Return string errors from tool handlers (preserves tool_result contract)
def run_read(path: str) -> str:
    try:
        return safe_path(path).read_text()
    except Exception as e:
        return f"Error: {e}"

# Raise on invalid internal state (for validation during development)
if len(items) > 20:
    raise ValueError("Max 20 items")
```

### Tool Handler Pattern
```python
# Lambda dispatch map for simple handlers
TOOL_HANDLERS = {
    "bash":       lambda **kw: run_bash(kw["command"]),
    "read_file":  lambda **kw: run_read(kw["path"], kw.get("limit")),
    "write_file": lambda **kw: run_write(kw["path"], kw["content"]),
    "edit_file":  lambda **kw: run_edit(kw["path"], kw["old_text"], kw["new_text"]),
}

# In the loop, execute via:
for block in response.content:
    if block.type == "tool_use":
        handler = TOOL_HANDLERS.get(block.name)
        output = handler(**block.input) if handler else f"Unknown tool: {block.name}"
```

### Message Formatting
- Use `<tag>` XML-like brackets for system-level metadata: `<reminder>`, `<background-results>`, `<inbox>`
- Use brackets for plan markers: `[x]` completed, `[>]` in_progress, `[ ]` pending

### JSON for Structured Data
- Task state, inbox messages, and protocol envelopes use JSON on disk
- Use `json.dumps(task, indent=2)` for human-readable storage
- Use absolute `Path` objects for file operations, `.relative_to(WORKDIR)` for display

### Concurrency
- Mutating tools (`write_file`, `edit_file`) must be serialized
- Read-only tools (`read_file`, `bash` with read-only commands) can run in parallel
- Use `threading.Thread(target=..., daemon=True)` for background tasks

### No `as any`, No `@ts-ignore`
This is Python - no type suppression pragmas needed. Write correct types.

### REPL Prompt Format
```python
if __name__ == "__main__":
    history = []
    while True:
        try:
            query = input("\033[36ms02 >> \033[0m")  # Cyan prompt with script name
        except (EOFError, KeyboardInterrupt):
            break
        if query.strip().lower() in ("q", "exit", ""):
            break
        history.append({"role": "user", "content": query})
        agent_loop(history)
        print()
```

---

## Key Patterns

### Agent Loop Skeleton
```python
def agent_loop(messages: list):
    while True:
        response = client.messages.create(model=MODEL, system=SYSTEM,
                                           messages=messages, tools=TOOLS, max_tokens=8000)
        messages.append({"role": "assistant", "content": response.content})
        if response.stop_reason != "tool_use":
            return
        results = []
        for block in response.content:
            if block.type == "tool_use":
                output = TOOL_HANDLERS[block.name](**block.input)
                results.append({"type": "tool_result", "tool_use_id": block.id, "content": output})
        messages.append({"role": "user", "content": results})
```

### Tool Output Size Limits
Large outputs get persisted to disk and replaced with a preview marker:
```python
def maybe_persist_output(tool_use_id: str, output: str, trigger_chars: int = 50000) -> str:
    if len(output) <= trigger_chars:
        return output
    stored_path = _persist_tool_result(tool_use_id, output)
    return _build_persisted_marker(stored_path, output)
```

### Environment Setup Pattern
```python
from dotenv import load_dotenv
load_dotenv(override=True)
if os.getenv("ANTHROPIC_BASE_URL"):
    os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)
client = Anthropic(base_url=os.getenv("ANTHROPIC_BASE_URL"))
MODEL = os.environ["MODEL_ID"]
```

---

## Directory Structure

```
learn-claude-code/
├── agents/              # Chapter implementations (s01-s19, s_full.py)
├── docs/zh/             # Chinese teaching docs (mainline)
├── docs/en/             # English docs
├── docs/ja/             # Japanese docs
├── skills/              # Skill files for s05
├── web/                 # Web teaching interface
├── .env.example         # Environment template
└── requirements.txt     # Dependencies
```

---

## Working in This Repository

1. Each `agents/s*.py` file is **standalone and runnable** - no import across chapters
2. `s_full.py` combines all mechanisms into oneREPL with commands: `/compact`, `/tasks`, `/team`, `/inbox`
3. When adding features, follow the existing section comment style
4. Validate paths before file operations; return string errors from tool handlers
5. Use `dataclass` for structured state that needs field names; `dict` for simple lookups