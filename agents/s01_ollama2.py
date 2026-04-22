#!/usr/bin/env python3
# s01_anthropic_force_tools.py - 强制工具调用版
"""
强制模型使用工具，完美解决 qwen3 不调用工具问题。
"""

import os
import json
import subprocess
from dataclasses import dataclass
from typing import List, Any
import anthropic
import platform

from dotenv import load_dotenv
load_dotenv(override=True)

# === 配置 ===
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "ollama")
ANTHROPIC_BASE_URL = os.getenv("ANTHROPIC_BASE_URL", "http://192.168.1.99:11434")
MODEL = os.getenv("MODEL", "qwen3:14b")

# 检测操作系统
IS_WINDOWS = platform.system() == "Windows"

# === 强制工具使用的 SYSTEM 提示 ===
SYSTEM = f"""You are a coding agent. 

CRITICAL RULES (MUST FOLLOW):
1. For ANY file system operation, IMMEDIATELY use 'bash' tool FIRST
2. List files: bash '{ 'dir' if IS_WINDOWS else 'ls -la'}'
3. Read file: bash '{ 'type filename' if IS_WINDOWS else 'cat filename'}'
4. Check dir: bash '{ 'dir /s' if IS_WINDOWS else 'find . -type f'}'
5. Should pass back the tool_call name in the result.

NEVER say "I don't have access". ALWAYS use tools."""

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
    }
]

def run_bash(command: str) -> str:
    dangerous = ["rm -rf", "sudo", "shutdown", "del /f /s /q"]
    if any(d in command.lower() for d in dangerous):
        return "❌ Dangerous command blocked"
    
    try:
        result = subprocess.run(
            command, shell=True, cwd=os.getcwd(), 
            encoding='utf-8',  # ✅ 强制 UTF-8
            errors='replace',  # 无法解码字符替换为 ?
            capture_output=True, text=True, timeout=30
        )
        output = result.stdout + result.stderr
        return output.strip()[:4000] or "(no output)"
    except Exception as e:
        return f"❌ Error: {e}"

def safe_extract_text(content: List[Any]) -> str:
    text_parts = []
    for block in content:
        try:
            if hasattr(block, 'type') and block.type == "text":
                text_parts.append(getattr(block, 'text', ''))
            elif hasattr(block, 'type') and block.type == "thinking":
                if hasattr(block, 'text'):
                    text_parts.append(block.text)
        except:
            pass
    return " ".join(t.strip() for t in text_parts if t.strip())

def execute_tools(tool_calls: List) -> List[dict]:
    print("\n🔧 执行工具...")
    results = []
    for tool_call in tool_calls:
        try:
            # Ollama 格式处理
            name = getattr(tool_call, 'name', 'bash') or 'bash'
            tool_call.name = name
            args = getattr(tool_call, 'input', {}) or {}
            command = args.get('command', '')
            
            print(f"  $ {command}")
            output = run_bash(command)
            print(f"  📤 {output[:200]}{'...' if len(output) > 200 else ''}")
            
            results.append({
                "type": "tool_result",
                "tool_use_id": getattr(tool_call, 'id', 'call_1'),
                "content": output,
            })
        except Exception as e:
            results.append({
                "type": "tool_result",
                "tool_use_id": getattr(tool_call, 'id', 'call_1'),
                "content": f"Error: {e}",
            })
    return results

def chat_with_tools(user_query: str, history: List[dict]) -> str:
    messages = history + [{"role": "user", "content": user_query}]
    
    # 强制工具使用
    response = client.messages.create(
        model=MODEL,
        max_tokens=2048,
        messages=messages,
        system=SYSTEM,
        tools=TOOLS,
        tool_choice={"type": "auto"},  # 让模型决定，但提示很强
        temperature=0.0,
    )
    
    # 处理工具调用循环
    current_messages = messages + [{"role": "assistant", "content": response.content}]
    
    while True:
        tool_calls = [b for b in response.content if hasattr(b, 'type') and b.type == "tool_use"]
        if not tool_calls:
            break
            
        tool_results = execute_tools(tool_calls)
        current_messages.append({"role": "user", "content": tool_results})
        
        response = client.messages.create(
            model=MODEL,
            max_tokens=4096,
            messages=current_messages,
            system=SYSTEM,
            tools=TOOLS,
            temperature=0.0,
        )
        current_messages.append({"role": "assistant", "content": response.content})
    
    return safe_extract_text(response.content)

if __name__ == "__main__":
    print("🚀 强制工具调用 Agent (Windows/Linux 兼容)")
    print(f"📁 当前目录: {os.getcwd()}")
    
    history = [{"role": "system", "content": SYSTEM}]
    
    while True:
        query = input("\n🤖 tools >> ").strip()
        if query.lower() in ("q", "exit", "quit"):
            break
            
        if query:
            response = chat_with_tools(query, history)
            print(f"\n✅ {response}")
            history.append({"role": "user", "content": query})
            history.append({"role": "assistant", "content": response})