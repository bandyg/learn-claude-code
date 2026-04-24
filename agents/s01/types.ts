// types.ts - 共享类型定义
// 对应 Python: from typing import List, Any

/**
 * Anthropic 内容块类型
 * 对应 Python: List[Any] 中的每个 block
 */
export type AnthropicContentBlock =
  | { type: "text"; text: string }
  | { type: "thinking"; thinking: string }
  | { type: "tool_use"; id: string; name: string; input: Record<string, unknown> };

/**
 * 工具调用结果
 * 对应 Python: execute_tools 返回的 dict
 */
export interface ToolResult {
  type: "tool_result";
  tool_use_id: string;
  content: string;
}

/**
 * 聊天消息
 * 对应 Python: List[dict] with role and content
 */
export interface ChatMessage {
  role: "system" | "user" | "assistant";
  content: string | ToolResult[];
}

/**
 * 工具定义
 * 对应 Python: TOOLS 列表中的每个工具
 */
export interface ToolDefinition {
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

/**
 * Ollama 响应格式
 */
export interface OllamaResponse {
  content: AnthropicContentBlock[];
  stop_reason?: string;
}

/**
 * Ollama 消息参数
 */
export interface OllamaMessageParams {
  model: string;
  max_tokens: number;
  messages: ChatMessage[];
  system?: string;
  tools?: ToolDefinition[];
  tool_choice?: { type: string };
  temperature?: number;
}

/**
 * 工具调用输入格式（兼容多种模型）
 */
export interface ToolCallInput {
  command?: string;
  cmd?: string | string[];
}