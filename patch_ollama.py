#!/usr/bin/env python3
import re
import sys

with open('agents/s01_ollama1.py', 'r', encoding='utf-8') as f:
    content = f.read()

# Add fallback parsing before 'if not tool_calls:'
old = '    # Check if model called tools\n    tool_calls = assistant_msg.get("tool_calls", [])\n\n    if not tool_calls:'

new = '    # Check if model called tools\n    tool_calls = assistant_msg.get("tool_calls", [])\n\n    # Fallback: if no tool_calls but content looks like JSON tool call, parse it\n    if not tool_calls:\n        text = assistant_msg.get("content", "")\n        if text and (text.strip().startswith(("{")) or \'"name"\' in text):\n            parsed = extract_tool_calls_from_content(text)\n            if parsed:\n                tool_calls = parsed\n\n    if not tool_calls:'

if old in content:
    content = content.replace(old, new)
    with open('agents/s01_ollama1.py', 'w', encoding='utf-8') as f:
        f.write(content)
    print('Patched successfully')
else:
    print('Pattern not found')
    lines = content.split('\n')
    for i, line in enumerate(lines[200:220], start=201):
        print(f"{i}: {line}")
