#!/usr/bin/env node
// s01_ollama2.ts - 强制工具调用版 (TypeScript)
// 强制模型使用工具，完美解决 qwen3 不调用工具问题。

import { exec } from "child_process";
import { promisify } from "util";
import * as os from "os";

const execAsync = promisify(exec);

// === 类型定义 ===
type AnthropicContentBlock =
  | { type: "text"; text: string }
  | { type: "thinking"; thinking: string }
  | { type: "tool_use"; id: string; name: string; input: Record<string, unknown> };

interface ToolResult {
  type: "tool_result";
  tool_use_id: string;
  content: string;
}

interface ChatMessage {
  role: "system" | "user" | "assistant";
  content: string | ToolResult[];
}

interface ToolDefinition {
  type: "function";
  function: {
    name: string;
    description: string;
    parameters: {
      type: "object";
      properties: Record<string, { type: string; description: string }>;
      required: string[];
    };
  };
}

interface OllamaMessageParams {
  model: string;
  max_tokens: number;
  messages: ChatMessage[];
  system?: string;
  tools?: ToolDefinition[];
  tool_choice?: { type: string };
  temperature?: number;
}

// === 配置 ===
const ANTHROPIC_API_KEY = process.env["ANTHROPIC_API_KEY"] ?? "ollama";
const ANTHROPIC_BASE_URL = process.env["ANTHROPIC_BASE_URL"] ?? "http://192.168.1.99:11434";
const MODEL = process.env["MODEL"] ?? "qwen3:14b";

// 检测操作系统
const IS_WINDOWS = os.platform() === "win32";

// === 强制工具使用的 SYSTEM 提示 ===
const SYSTEM = `You are a coding agent. 

CRITICAL RULES (MUST FOLLOW):
1. For ANY file system operation, IMMEDIATELY use 'bash' tool FIRST
2. List files: bash '${IS_WINDOWS ? "dir" : "ls -la"}'
3. Read file: bash '${IS_WINDOWS ? "type filename" : "cat filename"}'
4. Check dir: bash '${IS_WINDOWS ? "dir /s" : "find . -type f"}'

IMPORTANT: When calling the bash tool, you MUST respond with a valid JSON object like:
{"command": "ls -la"}
or
{"cmd": ["ls", "-la"]}

NEVER send bare strings or malformed JSON like {"dir","agents/s01"}.

ALWAYS use tools when asked to perform file operations.`;

// === Anthropic 客户端 (兼容 Ollama API) ===
const TOOLS: ToolDefinition[] = [
  {
    type: "function",
    function: {
      name: "bash",
      description: `Execute shell command. Windows: use 'dir', 'type file'. Linux/Mac: 'ls', 'cat file'.`,
      parameters: {
        type: "object",
        properties: { command: { type: "string", description: "Shell command" } },
        required: ["command"],
      },
    },
  },
];

// 异步 HTTP 客户端 (兼容 Anthropic/Ollama)
async function anthropicChat(
  params: OllamaMessageParams
): Promise<{ content: AnthropicContentBlock[]; stop_reason?: string }> {
  const url = `${ANTHROPIC_BASE_URL}/v1/messages`;
  const body = {
    model: params.model,
    max_tokens: params.max_tokens,
    messages: params.messages.map((m) => ({
      role: m.role,
      content: typeof m.content === "string" ? m.content : JSON.stringify(m.content),
    })),
    system: params.system,
    tools: params.tools,
    tool_choice: params.tool_choice,
    temperature: params.temperature,
  };

  const response = await fetch(url, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${ANTHROPIC_API_KEY}`,
    },
    body: JSON.stringify(body),
  });

  if (!response.ok) {
    const errorText = await response.text();
    throw new Error(`HTTP ${response.status}: ${errorText}`);
  }

  const data = (await response.json()) as {
    content?: Array<{
      type: string;
      text?: string;
      name?: string;
      input?: Record<string, unknown>;
    }>;
    stop_reason?: string;
  };

  const content: AnthropicContentBlock[] = (data.content ?? []).map((block) => {
    if (block.type === "text") {
      return { type: "text", text: block.text ?? "" } as AnthropicContentBlock;
    } else if (block.type === "tool_use") {
      return {
        type: "tool_use",
        id: `call_${Date.now()}_${Math.random().toString(36).slice(2, 8)}`,
        name: block.name ?? "bash",
        input: block.input ?? {},
      } as AnthropicContentBlock;
    }
    return { type: "text", text: "" } as AnthropicContentBlock;
  });

  return { content, stop_reason: data.stop_reason };
}

// 危险命令黑名单
const DANGEROUS_PATTERNS = ["rm -rf", "sudo", "shutdown", "del /f /s /q"];

function isDangerousCommand(command: string): boolean {
  const lower = command.toLowerCase();
  return DANGEROUS_PATTERNS.some((pattern) => lower.includes(pattern));
}

async function runBash(command: string): Promise<string> {
  if (isDangerousCommand(command)) {
    return "❌ Dangerous command blocked";
  }

  const cwd = process.cwd();

  try {
    const { stdout, stderr } = await execAsync(command, {
      cwd,
      encoding: "utf-8",
      timeout: 30_000,
      maxBuffer: 1024 * 1024,
    });
    const output = (stdout ?? "") + (stderr ?? "");
    const trimmed = output.trim();
    return trimmed.length > 0 ? trimmed.slice(0, 4000) : "(no output)";
  } catch (err) {
    const error = err as { message?: string };
    return `❌ Error: ${error.message ?? String(err)}`;
  }
}

function safeExtractText(content: AnthropicContentBlock[]): string {
  const textParts: string[] = [];

  for (const block of content) {
    try {
      if (block.type === "text") {
        textParts.push((block as { type: "text"; text: string }).text ?? "");
      } else if (block.type === "thinking") {
        const thinkingBlock = block as { type: "thinking"; thinking?: string };
        if (thinkingBlock.thinking) {
          textParts.push(thinkingBlock.thinking);
        }
      }
    } catch {
      // Skip blocks that don't match expected structure
    }
  }

  return textParts.map((t) => t.trim()).filter((t) => t.length > 0).join(" ");
}

// 工具调用输入格式（兼容多种模型）
interface ToolCallInput {
  command?: string;
  cmd?: string | string[];
}

async function executeToolsAsync(toolCalls: AnthropicContentBlock[]): Promise<ToolResult[]> {
  console.log("\n🔧 执行工具...");
  const results: ToolResult[] = [];

  for (const block of toolCalls) {
    if (block.type !== "tool_use") continue;

    const toolCall = block as {
      type: "tool_use";
      id: string;
      name?: string;
      input?: ToolCallInput;
    };

    try {
      const args = toolCall.input ?? {};

      let command = "";
      if ("command" in args && typeof args.command === "string") {
        command = args.command;
      } else if ("cmd" in args) {
        const cmdVal = args.cmd;
        if (Array.isArray(cmdVal)) {
          command = cmdVal.map((c) => String(c)).join(" ");
        } else if (typeof cmdVal === "string") {
          command = cmdVal;
        }
      }

      // Validate command is a non-empty string before executing
      if (!command || typeof command !== "string" || command.trim() === "") {
        results.push({
          type: "tool_result",
          tool_use_id: toolCall.id ?? `call_${Date.now()}`,
          content: "❌ Error: Empty or invalid command",
        });
        continue;
      }

      command = command.trim();
      if (command.toLowerCase().startsWith("bash ")) {
        const remainder = command.slice(5).trim();
        for (const prefix of ["-lc", "-c"]) {
          if (remainder.toLowerCase().startsWith(prefix)) {
            command = remainder.slice(prefix.length).trim();
            break;
          }
        }
      }

      console.log(`  $ ${command}`);
      const output = await runBash(command);
      const preview = output.length > 200 ? output.slice(0, 200) + "..." : output;
      console.log(`  📤 ${preview}`);

      results.push({
        type: "tool_result",
        tool_use_id: toolCall.id ?? `call_${Date.now()}`,
        content: output,
      });
    } catch (err) {
      results.push({
        type: "tool_result",
        tool_use_id: toolCall.id ?? `call_${Date.now()}`,
        content: `Error: ${String(err)}`,
      });
    }
  }

  return results;
}

// 类型别名：与 Python List[Any] 对应
type MessageContent = string | ToolResult[];

// 本地消息接口（避免与 types.ts 冲突）
interface ChatMessageLocal {
  role: "system" | "user" | "assistant";
  content: MessageContent;
}

async function chatWithTools(userQuery: string, history: ChatMessageLocal[]): Promise<string> {
  const messages: ChatMessageLocal[] = [...history, { role: "user", content: userQuery }];

  // 强制工具使用
  const response = await anthropicChat({
    model: MODEL,
    max_tokens: 2048,
    messages: messages as unknown as ChatMessage[],
    system: SYSTEM,
    tools: TOOLS,
    tool_choice: { type: "auto" },
    temperature: 0.0,
  });

  // 处理工具调用循环
  const currentMessages: ChatMessageLocal[] = [
    ...messages,
    { role: "assistant", content: response.content as unknown as MessageContent },
  ];

  // eslint-disable-next-line no-constant-condition
  while (true) {
    const toolCalls = response.content.filter((block) => block.type === "tool_use");

    if (toolCalls.length === 0) {
      break;
    }

    const toolResults = await executeToolsAsync(toolCalls);
    currentMessages.push({ role: "user", content: toolResults });

    const nextResponse = await anthropicChat({
      model: MODEL,
      max_tokens: 4096,
      messages: currentMessages as unknown as ChatMessage[],
      system: SYSTEM,
      tools: TOOLS,
      temperature: 0.0,
    });

    currentMessages.push({
      role: "assistant",
      content: nextResponse.content as unknown as MessageContent,
    });

    // Check if we should continue loop
    const hasMoreTools = nextResponse.content.some((block) => block.type === "tool_use");
    if (!hasMoreTools) {
      // Update response for final extraction
      Object.assign(response, nextResponse);
      break;
    }
  }

  return safeExtractText(response.content);
}

// === REPL 入口 ===
async function main(): Promise<void> {
  console.log("🚀 强制工具调用 Agent (Windows/Linux 兼容)");
  console.log(`📁 当前目录: ${process.cwd()}`);

  const history: ChatMessageLocal[] = [{ role: "system", content: SYSTEM }];

  // 异步逐行读取 stdin
  const readline = await import("readline");
  const rl = readline.createInterface({
    input: process.stdin,
    output: process.stdout,
  });

  const prompt = (): void => {
    rl.question("\n🤖 tools >> ", async (query) => {
      const trimmed = query.trim();
      const exitCommands = ["q", "exit", "quit"];
      if (exitCommands.includes(trimmed.toLowerCase())) {
        rl.close();
        return;
      }

      if (trimmed) {
        try {
          const response = await chatWithTools(trimmed, history);
          console.log(`\n✅ ${response}`);
          history.push({ role: "user", content: trimmed });
          history.push({ role: "assistant", content: response });
        } catch (err) {
          console.error(`\n❌ Error: ${err}`);
        }
      }

      prompt();
    });
  };

  prompt();
}

main().catch(console.error);

export {
  SYSTEM,
  TOOLS,
  runBash,
  isDangerousCommand,
  safeExtractText,
  executeToolsAsync,
  chatWithTools,
  main,
};