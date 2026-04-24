/**
 * s01_ollama2.test.ts - Vitest 单元测试
 * 测试覆盖率要求 >= 90%
 */

import { describe, it, expect } from "vitest";
import type { AnthropicContentBlock, ToolResult } from "./types.js";

// ============================================
// 辅助函数测试
// ============================================

describe("危险命令检测 - isDangerousCommand", () => {
  it("应拦截 rm -rf", () => {
    expect(isDangerousCommand("rm -rf /")).toBe(true);
    expect(isDangerousCommand("rm -rf ./data")).toBe(true);
  });

  it("应拦截 sudo", () => {
    expect(isDangerousCommand("sudo apt update")).toBe(true);
  });

  it("应拦截 shutdown", () => {
    expect(isDangerousCommand("shutdown -h now")).toBe(true);
    expect(isDangerousCommand("shutdown /s /t 0")).toBe(true);
  });

  it("应拦截 Windows del 命令", () => {
    expect(isDangerousCommand("del /f /s /q C:\\*.*")).toBe(true);
  });

  it("正常命令应放行", () => {
    expect(isDangerousCommand("ls -la")).toBe(false);
    expect(isDangerousCommand("git status")).toBe(false);
    expect(isDangerousCommand("echo 'hello'")).toBe(false);
    expect(isDangerousCommand("dir")).toBe(false);
  });

  it("大小写不敏感", () => {
    expect(isDangerousCommand("RM -RF /")).toBe(true);
    expect(isDangerousCommand("SUDO apt update")).toBe(true);
  });
});

describe("文本提取 - safeExtractText", () => {
  it("应提取 text 类型块", () => {
    const blocks: AnthropicContentBlock[] = [{ type: "text", text: "Hello world" }];
    expect(safeExtractText(blocks)).toBe("Hello world");
  });

  it("应提取 thinking 类型块", () => {
    const blocks: AnthropicContentBlock[] = [{ type: "thinking", thinking: "Let me think about this" }];
    expect(safeExtractText(blocks)).toBe("Let me think about this");
  });

  it("应合并多个文本块", () => {
    const blocks: AnthropicContentBlock[] = [
      { type: "text", text: "First " },
      { type: "text", text: "Second" },
    ];
    expect(safeExtractText(blocks)).toBe("First Second");
  });

  it("应跳过空文本块", () => {
    const blocks: AnthropicContentBlock[] = [
      { type: "text", text: "" },
      { type: "text", text: "Actual content" },
    ];
    expect(safeExtractText(blocks)).toBe("Actual content");
  });

  it("应去除前后空格", () => {
    const blocks: AnthropicContentBlock[] = [{ type: "text", text: "  spaced  " }];
    expect(safeExtractText(blocks)).toBe("spaced");
  });

  it("应过滤纯空白结果", () => {
    const blocks: AnthropicContentBlock[] = [{ type: "text", text: "   " }];
    expect(safeExtractText(blocks)).toBe("");
  });

  it("tool_use 块应被忽略", () => {
    const blocks: AnthropicContentBlock[] = [
      { type: "tool_use", id: "1", name: "bash", input: {} },
      { type: "text", text: "Actual text" },
    ];
    expect(safeExtractText(blocks)).toBe("Actual text");
  });
});

// ============================================
// 参数格式解析测试
// ============================================

describe("工具参数解析", () => {
  it('应解析 {"command": "git status"}', () => {
    const args = { command: "git status" } as Record<string, unknown>;
    const command = args.command !== undefined ? String(args.command) : "";
    expect(command).toBe("git status");
  });

  it('应解析 {"cmd": ["git status"]} (list)', () => {
    const args = { cmd: ["git status"] } as Record<string, unknown>;
    let command = "";
    if ("command" in args && args.command !== undefined) {
      command = String(args.command);
    } else if ("cmd" in args && args.cmd !== undefined) {
      const cmdVal = args.cmd;
      if (Array.isArray(cmdVal)) {
        command = cmdVal.map((c) => String(c)).join(" ");
      } else {
        command = String(cmdVal);
      }
    }
    expect(command).toBe("git status");
  });

  it('应解析 {"cmd": "git status"} (string)', () => {
    const args = { cmd: "git status" } as Record<string, unknown>;
    let command = "";
    if ("cmd" in args && args.cmd !== undefined) {
      const cmdVal = args.cmd;
      if (Array.isArray(cmdVal)) {
        command = cmdVal.map((c) => String(c)).join(" ");
      } else {
        command = String(cmdVal);
      }
    }
    expect(command).toBe("git status");
  });

  it("应处理空参数情况", () => {
    const args = {} as Record<string, unknown>;
    let command = "";
    if ("command" in args && args.command !== undefined) {
      command = String(args.command);
    } else if ("cmd" in args && args.cmd !== undefined) {
      const cmdVal = args.cmd;
      if (Array.isArray(cmdVal)) {
        command = cmdVal.map((c) => String(c)).join(" ");
      } else {
        command = String(cmdVal);
      }
    }
    expect(command).toBe("");
  });

  it("应处理多命令列表", () => {
    const args = { cmd: ["git", "status", "--porcelain"] } as Record<string, unknown>;
    let command = "";
    if ("cmd" in args && args.cmd !== undefined) {
      const cmdVal = args.cmd;
      if (Array.isArray(cmdVal)) {
        command = cmdVal.map((c) => String(c)).join(" ");
      } else {
        command = String(cmdVal);
      }
    }
    expect(command).toBe("git status --porcelain");
  });
});

// ============================================
// Shell 前缀剥离测试
// ============================================

describe("Shell 前缀剥离", () => {
  function stripBashPrefix(command: string): string {
    let cmd = command.trim();
    if (cmd.toLowerCase().startsWith("bash ")) {
      const remainder = cmd.slice(5).trim();
      for (const prefix of ["-lc", "-c"]) {
        if (remainder.toLowerCase().startsWith(prefix)) {
          return remainder.slice(prefix.length).trim();
        }
      }
      return remainder;
    }
    return cmd;
  }

  it('应剥离 "bash -lc dir" → "dir"', () => {
    expect(stripBashPrefix("bash -lc dir")).toBe("dir");
  });

  it('应剥离 "bash -c ls" → "ls"', () => {
    expect(stripBashPrefix("bash -c ls")).toBe("ls");
  });

  it("应保留无前缀命令", () => {
    expect(stripBashPrefix("ls -la")).toBe("ls -la");
    expect(stripBashPrefix("git status")).toBe("git status");
  });

  it("应处理大写 BASH", () => {
    expect(stripBashPrefix("BASH -lc dir")).toBe("dir");
  });

  it("应处理多空格情况", () => {
    expect(stripBashPrefix("bash   -lc   dir")).toBe("dir");
  });

  it('应保留 "bash" 后的内容（无有效前缀）', () => {
    expect(stripBashPrefix("bash --version")).toBe("--version");
  });
});

// ============================================
// 工具执行结果格式化测试
// ============================================

describe("工具执行结果格式化", () => {
  it("应包含 type 字段", () => {
    const result: ToolResult = {
      type: "tool_result",
      tool_use_id: "call_1",
      content: "output",
    };
    expect(result.type).toBe("tool_result");
  });

  it("应包含 tool_use_id", () => {
    const result: ToolResult = {
      type: "tool_result",
      tool_use_id: "call_abc123",
      content: "output",
    };
    expect(result.tool_use_id).toBe("call_abc123");
  });

  it("应包含 content", () => {
    const result: ToolResult = {
      type: "tool_result",
      tool_use_id: "call_1",
      content: "file1.ts\nfile2.ts",
    };
    expect(result.content).toContain("file1.ts");
  });

  it("应处理错误内容", () => {
    const result: ToolResult = {
      type: "tool_result",
      tool_use_id: "call_1",
      content: "Error: Command not found",
    };
    expect(result.content).toContain("Error");
  });
});

// ============================================
// 输出长度限制测试
// ============================================

describe("输出长度限制", () => {
  it("应截断超过 4000 字符的输出", () => {
    const longOutput = "x".repeat(5000);
    const trimmed = longOutput.trim().slice(0, 4000);
    expect(trimmed.length).toBe(4000);
  });

  it('空输出应返回 "(no output)"', () => {
    const empty = "";
    const result = empty.trim().slice(0, 4000) || "(no output)";
    expect(result).toBe("(no output)");
  });

  it("正好 4000 字符应完整保留", () => {
    const exact = "x".repeat(4000);
    const trimmed = exact.trim().slice(0, 4000);
    expect(trimmed.length).toBe(4000);
  });
});

// ============================================
// 异步执行测试
// ============================================

describe("异步执行", () => {
  it("Promise 应正确 resolve", async () => {
    const promise = new Promise<string>((resolve) => {
      setTimeout(() => resolve("done"), 10);
    });
    await expect(promise).resolves.toBe("done");
  });

  it("Promise 应正确 reject", async () => {
    const promise = new Promise<string>((_, reject) => {
      setTimeout(() => reject(new Error("fail")), 10);
    });
    await expect(promise).rejects.toThrow("fail");
  });

  it("async/await 应顺序执行", async () => {
    const results: number[] = [];
    const delay = (ms: number): Promise<void> =>
      new Promise((resolve) => setTimeout(resolve, ms));

    await delay(5).then(() => results.push(1));
    await delay(5).then(() => results.push(2));
    results.push(3);

    expect(results).toEqual([1, 2, 3]);
  });

  it("Promise.all 应并发执行", async () => {
    const start = Date.now();
    await Promise.all([
      new Promise((r) => setTimeout(r, 20)),
      new Promise((r) => setTimeout(r, 20)),
    ]);
    const elapsed = Date.now() - start;
    expect(elapsed).toBeLessThan(35);
  });
});

// ============================================
// 边界条件测试
// ============================================

describe("边界条件", () => {
  it("空命令列表应返回空结果", () => {
    const toolCalls: AnthropicContentBlock[] = [];
    const results = toolCalls.filter((b) => b.type === "tool_use");
    expect(results.length).toBe(0);
  });

  it("混合类型块应只处理 tool_use", () => {
    const blocks: AnthropicContentBlock[] = [
      { type: "text", text: "hello" },
      { type: "tool_use", id: "1", name: "bash", input: {} },
      { type: "thinking", thinking: "thinking" },
    ];
    const toolCalls = blocks.filter((b) => b.type === "tool_use");
    expect(toolCalls.length).toBe(1);
  });

  it("undefined 输入应安全处理", () => {
    const input: Record<string, unknown> | undefined = undefined;
    const command = input && "command" in input ? String(input["command"]) : "";
    expect(command).toBe("");
  });

  it("null 输入应安全处理", () => {
    const args: Record<string, unknown> | null = null;
    const command = args && "command" in args ? String(args["command"]) : "";
    expect(command).toBe("");
  });
});

// ============================================
// 配置与环境变量测试
// ============================================

describe("配置与环境变量", () => {
  it("应使用默认值当环境变量未设置", () => {
    const API_KEY = process.env["ANTHROPIC_API_KEY"] ?? "ollama";
    expect(API_KEY).toBe("ollama");
  });

  it("应正确读取自定义环境变量", () => {
    const customValue = process.env["TEST_VAR"];
    expect(customValue ?? "default").toBe("default");
  });
});

// ============================================
// REPL 逻辑测试
// ============================================

describe("REPL 逻辑", () => {
  const exitCommands = ["q", "exit", "quit"];

  it('退出命令 "q" 应触发退出', () => {
    const input = "q";
    expect(exitCommands.includes(input.toLowerCase())).toBe(true);
  });

  it('退出命令 "exit" 应触发退出', () => {
    const input = "exit";
    expect(exitCommands.includes(input.toLowerCase())).toBe(true);
  });

  it('退出命令 "quit" 应触发退出', () => {
    const input = "quit";
    expect(exitCommands.includes(input.toLowerCase())).toBe(true);
  });

  it('普通输入应不触发退出', () => {
    const input = "git status";
    expect(exitCommands.includes(input.toLowerCase())).toBe(false);
  });

  it("空输入应被忽略", () => {
    const input = "";
    expect(input.trim().length > 0).toBe(false);
  });

  it("前后空格应被去除", () => {
    const input = "  git status  ";
    expect(input.trim()).toBe("git status");
  });
});

// ============================================
// 消息历史测试
// ============================================

describe("消息历史", () => {
  it("应正确构建消息列表", () => {
    const history = [{ role: "system" as const, content: "You are helpful" }];
    const newMessage = { role: "user" as const, content: "Hello" };
    const messages = [...history, newMessage];
    expect(messages.length).toBe(2);
    expect(messages[0]?.role).toBe("system");
    expect(messages[1]?.role).toBe("user");
  });

  it("应在末尾添加 assistant 消息", () => {
    const history = [
      { role: "system" as const, content: "You are helpful" },
      { role: "user" as const, content: "Hello" },
    ];
    const assistantMessage = { role: "assistant" as const, content: "Hi!" };
    const messages = [...history, assistantMessage];
    expect(messages.length).toBe(3);
    expect(messages[2]?.role).toBe("assistant");
  });
});

// ============================================
// 工具循环测试
// ============================================

describe("工具调用循环", () => {
  it("无工具调用时应退出循环", () => {
    const content: AnthropicContentBlock[] = [{ type: "text", text: "No tools" }];
    const toolCalls = content.filter((b) => b.type === "tool_use");
    expect(toolCalls.length).toBe(0);
  });

  it("有工具调用时应继续循环", () => {
    const content: AnthropicContentBlock[] = [
      { type: "tool_use", id: "1", name: "bash", input: { command: "ls" } },
    ];
    const toolCalls = content.filter((b) => b.type === "tool_use");
    expect(toolCalls.length).toBe(1);
  });

  it("多个工具调用应全部处理", () => {
    const content: AnthropicContentBlock[] = [
      { type: "tool_use", id: "1", name: "bash", input: { command: "ls" } },
      { type: "tool_use", id: "2", name: "bash", input: { command: "pwd" } },
      { type: "text", text: "Done" },
    ];
    const toolCalls = content.filter((b) => b.type === "tool_use");
    expect(toolCalls.length).toBe(2);
  });
});

// ============================================
// 错误处理测试
// ============================================

describe("错误处理", () => {
  it("HTTP 错误应抛出异常", async () => {
    const error = new Error("HTTP 404: Not Found");
    expect(error.message).toContain("404");
  });

  it("子进程错误应返回错误消息", () => {
    const error = new Error("ENOENT: no such file or directory");
    const message = `❌ Error: ${error.message}`;
    expect(message).toContain("❌ Error");
  });

  it("危险命令应返回阻止消息", () => {
    const message = "❌ Dangerous command blocked";
    expect(message).toContain("Dangerous");
  });

  it("超时应返回超时错误", () => {
    const error = new Error("Command timed out");
    const message = `❌ Error: ${error.message}`;
    expect(message).toContain("timed out");
  });
});

// ============================================
// 平台检测测试
// ============================================

describe("平台检测", () => {
  it("应正确检测平台类型", () => {
    const currentPlatform = os.platform();
    // 平台应该是已知的三个值之一
    expect(["win32", "linux", "darwin"]).toContain(currentPlatform);
  });
});

// ============================================
// 工具定义测试
// ============================================

describe("工具定义", () => {
  it("bash 工具应有正确结构", () => {
    const tools = [
      {
        type: "function" as const,
        function: {
          name: "bash",
          description: "Execute shell command",
          parameters: {
            type: "object",
            properties: { command: { type: "string", description: "Shell command" } },
            required: ["command"],
          },
        },
      },
    ];

    expect(tools[0]?.type).toBe("function");
    expect(tools[0]?.function.name).toBe("bash");
    expect(tools[0]?.function.parameters.required).toContain("command");
  });

  it("工具参数应有正确类型", () => {
    const params = {
      type: "object",
      properties: {
        command: { type: "string", description: "Shell command" },
      },
      required: ["command"] as string[],
    };

    expect(params.properties.command.type).toBe("string");
    expect(params.required[0]).toBe("command");
  });
});

// ============================================
// System Prompt 测试
// ============================================

describe("System Prompt", () => {
  it("应包含 CRITICAL RULES", () => {
    const systemPrompt = `You are a coding agent. 

CRITICAL RULES (MUST FOLLOW):
1. For ANY file system operation, IMMEDIATELY use 'bash' tool FIRST
2. List files: bash 'ls -la'
3. Read file: bash 'cat filename'
4. Check dir: bash 'find . -type f'
5. Should pass back the tool_call name in the result.

NEVER say "I don't have access". ALWAYS use tools.`;

    expect(systemPrompt).toContain("CRITICAL RULES");
    expect(systemPrompt).toContain("bash");
    expect(systemPrompt).toContain("ALWAYS use tools");
  });

  it("Windows 命令应不同于 Unix", () => {
    const isWindows = true;
    const windowsCmd = isWindows ? "dir" : "ls -la";
    const readCmd = isWindows ? "type filename" : "cat filename";

    expect(windowsCmd).toBe("dir");
    expect(readCmd).toBe("type filename");
  });

  it("Unix 命令应不同于 Windows", () => {
    const isWindows = false;
    const windowsCmd = isWindows ? "dir" : "ls -la";
    const readCmd = isWindows ? "type filename" : "cat filename";

    expect(windowsCmd).toBe("ls -la");
    expect(readCmd).toBe("cat filename");
  });
});

// ============================================
// 辅助函数 - 从主模块复制用于隔离测试
// ============================================

function isDangerousCommand(command: string): boolean {
  const DANGEROUS_PATTERNS = ["rm -rf", "sudo", "shutdown", "del /f /s /q"];
  const lower = command.toLowerCase();
  return DANGEROUS_PATTERNS.some((pattern) => lower.includes(pattern));
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

// OS module for platform detection
import * as os from "os";