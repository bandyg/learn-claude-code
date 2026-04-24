# handwriting the agent
import sys
import io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
sys.stdin = io.TextIOWrapper(sys.stdin.buffer, encoding='utf-8')

from anthropic import Anthropic
from dotenv import load_dotenv
import os

load_dotenv(override=True)

system_prompt = "You are a helpful computer assistant."

llmClient = Anthropic(base_url=os.getenv("ANTHROPIC_BASE_URL"))
MODEL = os.environ["MODEL_ID"]

TOOLS = [{
    "name": "bash",
    "description": "Run a shell command",
    "input_schema": {
        "type": "object",
        "properties": {"command": {"type": "string"}},
        "required": ["command"],
    },
}]

def run_bash(command: str) -> str:
    import subprocess
    result = subprocess.run(command, shell=True, capture_output=True, text=True)
    return result.stdout + result.stderr

messages = []

def call(prompt):
    """
    调用原神智能体
    """

    messages.append({"role": "user", "content": prompt})
    while True:
        llmResponse = llmClient.messages.create(
            model=MODEL,
            system=system_prompt,
            tools=TOOLS,
            messages=messages,
            max_tokens=4096,
        )
        messages.append({"role": "assistant", "content": llmResponse.content})
        if llmResponse.stop_reason != "tool_use":
            break  # 结束，返回结果
        # 执行工具
        tool_results = []
        for block in llmResponse.content:
            if block.type == "tool_use":
                output = run_bash(block.input["command"])
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": output,
                })
        # 把工具结果追加到消息，继续循环
        messages.append({"role": "user", "content": tool_results})

    return llmResponse.content

class Agent:
    def __init__(self):
        self.messages: list[dict] = [] # conversation history
        self.tools: list[dict] = [] # available tools
        self.model: str = MODEL # model id
        self.system_prompt: str = "" # system instructions

if __name__ == "__main__":
    print("Agent原神启动，输入 q 退出")
    while True:
        prompt = input("请输入：")
        if prompt.strip().lower() in ("q", "exit", "quit"):
            break

        response = call(prompt)
        for block in response:
            if hasattr(block, 'text'):
                print(block.text)
        print()
    print("Agent原神退出")