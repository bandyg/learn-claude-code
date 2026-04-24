"""
s01_agent_loop.py - The Agent Loop

This file teaches the smallest useful coding-agent pattern:

    user message
      -> model reply
      -> if tool_use: execute tools
      -> write tool_result back to messages
      -> continue

It intentionally keeps the loop small, but still makes the loop state explicit
so later chapters can grow from the same structure.
"""

# use the openai client
from anthropic import Anthropic
from dotenv import load_dotenv
import os

load_dotenv(override=True)

if os.getenv("ANTHROPIC_BASE_URL"):
    os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)

client = Anthropic(base_url=os.getenv("ANTHROPIC_BASE_URL"))
MODEL = os.environ["MODEL_ID"]

"""
流程是这样的：
你 → 程序 → LLM
           ↑
      tools 定义（告诉 LLM 有哪些工具可用）
LLM 返回（告诉程序要调哪个工具 + 参数）
           ↓
程序 → 执行工具 → 把结果写回消息 → 再调用 LLM
实际例子：
1. 你问 LLM："列出当前目录文件"
2. LLM 回复：{"type": "tool_use", "name": "bash", "input": {"command": "ls"}}
3. 你的程序解析这个回复，发现要调 bash
4. 你的程序执行 subprocess.run("ls", ...)
5. 程序把结果写回消息：[{"type": "tool_result", "content": "file1.py\nfile2.py"}]
6. 程序再次调用 LLM，这次带上 tool_result
关键点：
- TOOLS 只是告诉 LLM "你可以调这些工具"
- LLM 的回复只是声明要调什么，不负责执行
- 你的程序才是真正执行工具的那个
所以你的程序需要：
1. 解析 LLM 回复里的 tool_use 块
2. 根据 name 找到对应的处理函数
3. 执行，返回结果
4. 循环以上步骤，直到没有 tool_use 块

可以定义很多工具:
TOOLS = [
    # 1. bash - 1个必填参数
    {
        "name": "bash",
        "description": "执行 Shell 命令",
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {"type": "string"}
            },
            "required": ["command"],
        },
    },
    # 2. read_file - 1个必填参数 + 1个可选参数
    {
        "name": "read_file",
        "description": "读取文件内容",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "limit": {"type": "integer", "description": "最多读多少行"},  # 可选
            },
            "required": ["path"],
        },
    },
    # 3. write_file - 2个必填参数
    {
        "name": "write_file",
        "description": "写入文件",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "content": {"type": "string"},
            },
            "required": ["path", "content"],
        },
    },
    # 4. edit_file - 3个必填参数
    {
        "name": "edit_file",
        "description": "编辑文件",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "oldString": {"type": "string"},
                "newString": {"type": "string"},
            },
            "required": ["path", "oldString", "newString"],
        },
    },
    # 5. glob - 1个必填 + 1个可选
    {
        "name": "glob",
        "description": "按模式搜索文件",
        "input_schema": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string"},
                "path": {"type": "string", "description": "搜索目录，默认当前目录"},
            },
            "required": ["pattern"],
        },
    },
]
关键在于 stop_reason：
# 调用 LLM
response = client.messages.create(...)
# 检查 LLM 是否要求调工具
if response.stop_reason == "tool_use":
    # LLM 想要调工具
    
    # 遍历 LLM 的回复，找到所有 tool_use 块
    for block in response.content:
        if block.type == "tool_use":
            # block.name = "bash"
            # block.input = {"command": "ls"}
            
            # 执行工具
            output = run_bash(block.input["command"])
            
            # 把结果写回消息，附带 block.id 让 LLM 知道是哪个工具的结果
            results.append({
                "type": "tool_result",
                "tool_use_id": block.id,   # 关键：对应 LLM 刚才的 tool_use
                "content": output,
            })
确认机制：
LLM 返回	程序响应
stop_reason: "tool_use"	执行工具
stop_reason: "end_turn"	结束，直接输出文本
stop_reason: "max_tokens"	超出 token 限制
tool_use_id 的作用：
- LLM 可能一次请求多个工具
- 每个 tool_result 必须带上对应的 tool_use_id
- LLM 通过这个匹配结果和之前的调用
简单说：程序只管执行，执行完就继续调 LLM，LLM 自己会判断结果是否足够。
"""

TOOLS = [{
    "name": "bash",
    "description": "Run a shell command",
    "input_schema": {
        "type": "object",
        "properties": {"command": {"type": "string"}},
        "required": ["command"],
    },
}]

SYSTEM = "You are a helpful code review assistant."

messages = [] # all messages

def run_bash(command: str) -> str:
    import subprocess
    result = subprocess.run(command, shell=True, capture_output=True, text=True)
    return result.stdout + result.stderr

def call(prompt: str) -> str:
    # role: user & assistant
    messages.append({"role": "user", "content": prompt})

    while True:
        response = client.messages.create(
            model=MODEL,
            system=SYSTEM,
            messages=messages,
            tools=TOOLS,
            max_tokens=8000,
        )
        # 把 assistant 回复加入历史
        messages.append({"role": "assistant", "content": response.content})
        # 检查是否要调工具
        if response.stop_reason != "tool_use":
            break  # 结束，返回结果
        # 执行工具
        tool_results = []
        for block in response.content:
            if block.type == "tool_use":
                output = run_bash(block.input["command"])
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": output,
                })
        # 把工具结果追加到消息，继续循环
        messages.append({"role": "user", "content": tool_results})

    return response.content

if __name__ == "__main__":
    print("Agent 已启动，输入 q 退出")
    while True:
        prompt = input("\n你: ")
        if prompt.lower() in ("q", "exit", "quit"):
            break
        
        result = call(prompt)
        
        # 打印 LLM 的文本回复
        for block in result:
            if hasattr(block, "text") and block.text:
                print(f"\nLLM: {block.text}")
