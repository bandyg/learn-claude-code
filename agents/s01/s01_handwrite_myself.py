
class Agent:
    def __init__(self):
        self.messages: list[dict] = [] # conversation history
        self.tools: list[dict] = [] # available tools
        self.model: str = MODEL # model id
        self.system_prompt: str = "" # system instructions
        # why this agent does not have llm agent? can I create one in the class?
        self.llmClient = None; #

state = Agent()

from anthropic import Anthropic
load_dotenv(override=True)

TOOLS = [{
    "name": "bash",
    "description": "Run a shell command",
    "input_schema": {
        "type": "object",
        "properties": {"command": {"type": "string"}},
        "required": ["command"],
    },
}]

state.system_prompt = "You are a helpful computer assistant."
state.model = os.environ["MODEL_ID"]
state.llmClient = Anthropic(base_url=os.getenv("ANTHROPIC_BASE_URL"))
state.tools = TOOLS

def run_bash(command: str) -> str:
    import subprocess
    result = subprocess.run(command, shell=True, capture_output=True, text=True)
    return result.stdout + result.stderr

def loop_state(prompt):
    """
    调用原神智能体
    """
    state.messages.append({"role": "user", "content": prompt})

    while True:
        llmResponse = state.llmClient.messages.create(
        model=state.model,
        system=state.system_prompt,
        tools=state.tools,
        messages=state.messages,
        max_tokens=4096,
    )
        if(llmResponse.stop_reason != "tool_use"):
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
        state.messages.append({"role": "user", "content": tool_results})
        
    return llmResponse.content  

if __name__ == "__main__":
    print("Agent原神启动，输入 q 退出")
    while True:
        prompt = input("请输入：")
        if prompt.strip().lower() in ("q", "exit", "quit"):
            break

        response = loop_state(prompt)
        for block in response:
            if hasattr(block, 'text'):
                print(block.text)
        print()
    print("Agent原神退出")


