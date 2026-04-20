#!/usr/bin/env python3
# s01_ollama.py - Agent Loop with Ollama
"""
s01_ollama.py - The Agent Loop using Ollama as the backend.

Based on s01_agent_loop.py, adapted for Ollama's OpenAI-compatible API.

Usage:
    1. Install Ollama: https://ollama.com
    2. Pull a model:   ollama pull qwen3
    3. Start server:   ollama serve  (runs on localhost:11434 by default)
    4. Run this:       python agents/s01_ollama.py
"""

import os
import subprocess
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

from openai import OpenAI
from dotenv import load_dotenv

load_dotenv(override=True)

# === SECTION: Ollama configuration ===
# Default: Ollama runs locally on port 11434 with OpenAI-compatible API
OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434/v1")
OLLAMA_API_KEY = os.getenv("OLLAMA_API_KEY", "ollama")  # Ollama doesn't need real auth
MODEL = os.getenv("OLLAMA_MODEL", "qwen3")  # Change to your pulled model

# Initialize OpenAI client (compatible with Ollama's API)
client = OpenAI(base_url=OLLAMA_BASE_URL, api_key=OLLAMA_API_KEY)

SYSTEM = (
    f"You are a coding agent at {os.getcwd()}. "
    "Use bash to inspect and change the workspace. Act first, then report clearly."
)

TOOLS = [{
    "name": "bash",
    "description": "Run a shell command in the current workspace.",
    "input_schema": {
        "type": "object",
        "properties": {"command": {"type": "string"}},
        "required": ["command"],
    },
}]


@dataclass
class LoopState:
    messages: list
    turn_count: int = 1
    transition_reason: str | None = None


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


def extract_text(content) -> str:
    if not isinstance(content, list):
        return ""
    texts = []
    for block in content:
        text = getattr(block, "text", None)
        if text:
            texts.append(text)
    return "\n".join(texts).strip()


def execute_tool_calls(tool_calls) -> list[dict]:
    results = []
    for call in tool_calls:
        # OpenAI API: tool_calls have name and arguments (as dict or string)
        if call.type != "function":
            continue
        name = call.function.name
        args = call.function.arguments
        if isinstance(args, str):
            import json
            args = json.loads(args)
        if name == "bash":
            command = args.get("command", "")
            print(f"\033[33m$ {command}\033[0m")
            output = run_bash(command)
            print(output[:200])
            results.append({
                "type": "function",
                "id": call.id,
                "name": name,
                "content": output,
            })
        else:
            results.append({
                "type": "function",
                "id": call.id,
                "name": name,
                "content": f"Unknown tool: {name}",
            })
    return results


def run_one_turn(state: LoopState) -> bool:
    response = client.chat.completions.create(
        model=MODEL,
        system=SYSTEM,
        messages=state.messages,
        tools=TOOLS,
        max_tokens=8000,
    )
    message = response.choices[0].message
    state.messages.append({"role": "assistant", "content": message.content})

    # Ollama with tools: stop_reason is in response.choices[0].finish_reason
    finish_reason = response.choices[0].finish_reason
    if finish_reason != "tool_calls":
        state.transition_reason = None
        return False

    results = execute_tool_calls(response.choices[0].message.tool_calls or [])
    if not results:
        state.transition_reason = None
        return False

    state.messages.append({"role": "user", "content": results})
    state.turn_count += 1
    state.transition_reason = "tool_result"
    return True


def agent_loop(state: LoopState) -> None:
    while run_one_turn(state):
        pass


if __name__ == "__main__":
    print(f"Ollama Agent started. Model: {MODEL}")
    print(f"Backend: {OLLAMA_BASE_URL}")
    print("Input q to quit.\n")

    history = []
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

        final_text = extract_text(history[-1]["content"])
        if final_text:
            print(final_text)
        print()
