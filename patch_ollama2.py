#!/usr/bin/env python3

with open('agents/s01_ollama1.py', 'r', encoding='utf-8') as f:
    content = f.read()

# Fix: detect JSON tool calls even when content starts with text (not just "{")
old_check = '''    if not tool_calls:
        content = assistant_msg.get("content", "")
        if content and content.strip().startswith(("{", "```json")):
            parsed = extract_tool_calls_from_content(content)
            if parsed:
                tool_calls = parsed

    if not tool_calls:'''

new_check = '''    if not tool_calls:
        content = assistant_msg.get("content", "")
        # More flexible detection: content might start with text before the JSON
        # Look for JSON object with "name" and "arguments" keys anywhere in content
        if content and '"name"' in content and '"arguments"' in content:
            parsed = extract_tool_calls_from_content(content)
            if parsed:
                tool_calls = parsed

    if not tool_calls:'''

if old_check in content:
    content = content.replace(old_check, new_check)
    with open('agents/s01_ollama1.py', 'w', encoding='utf-8') as f:
        f.write(content)
    print('Patched successfully')
else:
    print('Pattern not found - showing lines 205-215:')
    lines = content.split('\n')
    for i in range(204, 216):
        print(f"{i+1}: {repr(lines[i])}")
