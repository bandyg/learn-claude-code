#!/usr/bin/env python3
# s01_ollama.py - Agent Loop with Ollama (Native API)
"""
s01_ollama.py - The Agent Loop using Ollama's native API.

Based on s01_agent_loop.py, adapted for Ollama's native /api/chat endpoint.

Usage:
    1. Install Ollama: https://ollama.com
    2. Pull a model:   ollama pull qwen3
    3. Start server:   ollama serve  (runs on localhost:11434 by default)
    4. Run this:       python agents/s01_ollama.py
"""

import os
import json
import subprocess
import urllib.request
import urllib.error
from dataclasses import dataclass

try:
    import readline
    readline.parse_and_bind('set bind-tty-special-chars off')
    readline.parse_and_bind('set input-meta on')
    readline.parse_and_bind('set output-meta on')
    readline.parse_and_bind('set convert-meta off')
    readline.parse_and_bind('set enable-meta-keybindings on')
except ImportError:
    pass

from dotenv import load_dotenv

load_dotenv(override=True)

# === SECTION: Ollama configuration ===
OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://192.168.1.99:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "ministral-3:14b")

SYSTEM = (
    f"You are a coding agent at {os.getcwd()} on Windows. "
    "IMPORTANT: Use Windows commands (dir, cd, type, del, copy, etc.), NOT Linux commands (ls, pwd, cat, rm, etc.). "
    "The workspace is D:\\projects\\learn-claude-code. Act first, then report clearly."
)

# === SECTION: Ollama API ===
def ollama_chat(model: str, messages: list, tools: list = None, timeout: int = 300) -> dict:
    """Call Ollama's native /api/chat endpoint."""
    url = f"{OLLAMA_BASE_URL}/api/chat"
    payload = {
        "model": model,
        "messages": messages,
        "stream": False,
    }
    if tools:
        payload["tools"] = tools

    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        error_body = e.read().decode("utf-8") if e.fp else ""
        raise Exception(f"Ollama API error {e.code}: {error_body}")


# === SECTION: Tools ===
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "bash",
            "description": "Run a shell command in the current workspace.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "The shell command to execute"},
                },
                "required": ["command"],
            },
        },
    }
]


def run_bash(command: str) -> str:
    dangerous = ["rm -rf /", "sudo", "shutdown", "reboot", "> /dev/"]
    if any(item in command for item in dangerous):
        return "Error: Dangerous command blocked"
    try:
        result = subprocess.run(
            command,
            shell=True,
            cwd=os.getcwd(),
            capture_output=True,
            text=True,
            timeout=120,
        )
    except subprocess.TimeoutExpired:
        return "Error: Timeout (120s)"
    except (FileNotFoundError, OSError) as e:
        return f"Error: {e}"

    output = (result.stdout + result.stderr).strip()
    return output[:50000] if output else "(no output)"


def execute_tool_calls(tool_calls: list) -> list:
    """Execute tool calls and return results in Ollama format."""
    results = []
    for call in tool_calls:
        func = call.get("function", {})
        name = func.get("name", "")
        args = func.get("arguments", {})
        if isinstance(args, str):
            args = json.loads(args)

        if name == "bash":
            command = args.get("command", "")
            print(f"\033[33m$ {command}\033[0m")
            output = run_bash(command)
            print(output[:200])
            results.append({
                "role": "tool",
                "content": output,
                "tool_call_id": call.get("id", ""),
            })
        else:
            results.append({
                "role": "tool",
                "content": f"Unknown tool: {name}",
                "tool_call_id": call.get("id", ""),
            })
    return results


def extract_text(response: dict) -> str:
    """Extract assistant's text content from Ollama response."""
    return response.get("message", {}).get("content", "")


@dataclass
class LoopState:
    messages: list
    turn_count: int = 1
    transition_reason: str | None = None


def run_one_turn(state: LoopState) -> bool:
    response = ollama_chat(
        model=OLLAMA_MODEL,
        messages=state.messages,
        tools=TOOLS,
        timeout=300,
    )

    assistant_msg = response.get("message", {})
    state.messages.append(assistant_msg)

    # Check if model called tools
    tool_calls = assistant_msg.get("tool_calls", [])

    if not tool_calls:
        state.transition_reason = None
        return False

    # Execute tools
    tool_results = execute_tool_calls(tool_calls)
    if not tool_results:
        state.transition_reason = None
        return False

    # Append tool results and continue (each as separate message)
    for tr in tool_results:
        state.messages.append(tr)
    state.turn_count += 1
    state.transition_reason = "tool_result"
    return True


def agent_loop(state: LoopState) -> None:
    while run_one_turn(state):
        pass


if __name__ == "__main__":
    print(f"Ollama Agent started. Model: {OLLAMA_MODEL}")
    print(f"Backend: {OLLAMA_BASE_URL}")
    print("Input q to quit.\n")

    history = [{"role": "system", "content": SYSTEM}]
    while True:
        try:
            query = input("\033[36ms01-ollama >> \033[0m")
        except (EOFError, KeyboardInterrupt):
            break
        if query.strip().lower() in ("q", "exit", ""):
            break

        history.append({"role": "user", "content": query})
        state = LoopState(messages=history)
        agent_loop(state)

        # Get last assistant message
        for msg in reversed(history):
            if isinstance(msg, dict) and msg.get("role") == "assistant":
                text = msg.get("content", "")
                if text:
                    print(text)
                break
        print()
