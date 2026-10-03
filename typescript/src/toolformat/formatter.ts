import type { AnyTool, Result } from "../tools/index.js";

export interface ParsedToolCall {
  id: string;
  name: string;
  args: Record<string, unknown>;
  raw: string;
}

export interface ToolFormatter {
  formatToolDefinitions(tools: AnyTool[]): string;
  parseToolCalls(response: string): ParsedToolCall[];
  formatToolResult(name: string, result: Result): string;
  modelFamily(): string;
  validateFormat(response: string): string | null;
}

const qwenToolCallRegex = /<tool_call>\s*<tool name="([^"]+)">\s*([\s\S]*?)\s*<\/tool>\s*<\/tool_call>/g;

const anthropicInvokeRegex = /<invoke tool="([^"]+)">([\s\S]*?)<\/invoke>/g;

export class GenericFormatter implements ToolFormatter {
  formatToolDefinitions(tools: AnyTool[]): string {
    if (tools.length === 0) return "";

    const schemas: Record<string, unknown>[] = [];
    for (const tool of tools) {
      const func: Record<string, unknown> = {
        name: tool.name,
        description: tool.description,
      };
      if ("inputSchema" in tool && typeof (tool as Record<string, unknown>).inputSchema === "function") {
        const schema = (tool as any).inputSchema();
        if (schema) {
          func.parameters = schema;
        }
      }
      schemas.push({ type: "function", function: func });
    }
    return JSON.stringify(schemas);
  }

  parseToolCalls(response: string): ParsedToolCall[] {
    if (!response) return [];

    let parsed: any[];
    try {
      parsed = JSON.parse(response);
    } catch {
      return [];
    }
    if (!Array.isArray(parsed)) return [];

    const result: ParsedToolCall[] = [];
    for (const c of parsed) {
      if (!c || !c.name) continue;
      let args: Record<string, unknown> = {};
      if (typeof c.arguments === "string") {
        try {
          args = JSON.parse(c.arguments);
        } catch {
          args = {};
        }
      } else if (typeof c.arguments === "object" && c.arguments !== null) {
        args = c.arguments as Record<string, unknown>;
      }
      result.push({
        id: typeof c.id === "string" ? c.id : "",
        name: c.name,
        args,
        raw: response,
      });
    }
    return result;
  }

  formatToolResult(_name: string, result: Result): string {
    if (result.success) {
      return typeof result.data === "string" ? result.data : JSON.stringify(result.data);
    }
    return `Error: ${result.error}`;
  }

  modelFamily(): string {
    return "generic";
  }

  validateFormat(response: string): string | null {
    if (!response) return null;

    let parsed: any[];
    try {
      parsed = JSON.parse(response);
    } catch {
      return "invalid JSON format";
    }
    if (!Array.isArray(parsed)) {
      return "expected JSON array";
    }

    for (const c of parsed) {
      if (!c || !c.name) {
        return "tool call missing 'name' field";
      }
      if (c.arguments === undefined || c.arguments === null) {
        return `tool call "${c.name}" missing 'arguments'`;
      }
    }
    return null;
  }
}

export class OpenAIFOrmatter extends GenericFormatter {
  modelFamily(): string {
    return "openai";
  }
}

export class DeepSeekFormatter extends GenericFormatter {
  modelFamily(): string {
    return "deepseek";
  }
}

export class GLMFormatter extends GenericFormatter {
  modelFamily(): string {
    return "glm";
  }
}

export class AnthropicFormatter extends GenericFormatter {
  parseToolCalls(response: string): ParsedToolCall[] {
    if (!response) return [];

    const matches = response.matchAll(anthropicInvokeRegex);
    const result: ParsedToolCall[] = [];
    for (const m of matches) {
      if (m.length < 3) continue;
      const name = m[1];
      const argsRaw = m[2];
      let args: Record<string, unknown>;
      try {
        args = JSON.parse(argsRaw);
      } catch {
        args = { _raw: argsRaw };
      }
      result.push({
        id: "",
        name,
        args,
        raw: m[0],
      });
    }
    return result;
  }

  formatToolResult(_name: string, result: Result): string {
    if (result.success) {
      const output = typeof result.data === "string" ? result.data : JSON.stringify(result.data);
      return `<function_results><result tool="${_name}">${output}</result></function_results>`;
    }
    return `<function_results><result tool="${_name}">Error: ${result.error}</result></function_results>`;
  }

  modelFamily(): string {
    return "anthropic";
  }

  validateFormat(response: string): string | null {
    if (!response) return null;

    const matches = response.matchAll(anthropicInvokeRegex);
    let found = false;
    for (const m of matches) {
      found = true;
      if (m.length < 2 || !m[1]) {
        return "tool call missing 'tool' attribute";
      }
      const argsRaw = m[2];
      if (!argsRaw) {
        return `tool call "${m[1]}" missing arguments`;
      }
      try {
        JSON.parse(argsRaw);
      } catch {
        return `tool call "${m[1]}" has invalid JSON arguments`;
      }
    }
    if (!found && response.includes("<invoke")) {
      return "malformed tool call: missing tool attribute";
    }
    return null;
  }
}

export class QwenFormatter implements ToolFormatter {
  formatToolDefinitions(tools: AnyTool[]): string {
    if (tools.length === 0) return "";

    let result = "";
    for (const tool of tools) {
      result += `<tool_description>\n<tool_name>${tool.name}</tool_name>\n<description>${tool.description}</description>\n`;
      if ("inputSchema" in tool && typeof (tool as Record<string, unknown>).inputSchema === "function") {
        const schema = (tool as any).inputSchema();
        if (schema) {
          result += `<parameters>${JSON.stringify(schema)}</parameters>\n`;
        }
      }
      result += "</tool_description>\n";
    }
    return result;
  }

  parseToolCalls(response: string): ParsedToolCall[] {
    if (!response) return [];

    const matches = response.matchAll(qwenToolCallRegex);
    const result: ParsedToolCall[] = [];
    for (const m of matches) {
      if (m.length < 3) continue;
      const name = m[1];
      const argsRaw = m[2];
      let args: Record<string, unknown>;
      try {
        args = JSON.parse(argsRaw);
      } catch {
        args = { _raw: argsRaw };
      }
      result.push({
        id: "",
        name,
        args,
        raw: m[0],
      });
    }
    return result;
  }

  formatToolResult(_name: string, result: Result): string {
    if (result.success) {
      const output = typeof result.data === "string" ? result.data : JSON.stringify(result.data);
      return `<tool_response>\n<tool_name>${_name}</tool_name>\n<result>${output}</result>\n</tool_response>`;
    }
    return `<tool_response>\n<tool_name>${_name}</tool_name>\n<error>${result.error}</error>\n</tool_response>`;
  }

  modelFamily(): string {
    return "qwen";
  }

  validateFormat(response: string): string | null {
    if (!response) return null;

    const matches = response.matchAll(qwenToolCallRegex);
    let found = false;
    for (const m of matches) {
      found = true;
      if (m.length < 2 || !m[1]) {
        return "tool call missing function name";
      }
      const name = m[1];
      const argsRaw = m[2];
      if (!argsRaw || !argsRaw.trim()) {
        return `tool call "${name}" missing arguments`;
      }
      try {
        JSON.parse(argsRaw);
      } catch {
        return `tool call "${name}" has invalid JSON arguments`;
      }
    }
    if (!found && response.includes("<tool")) {
      return "malformed tool call";
    }
    return null;
  }
}

export function getFormatter(modelName: string): ToolFormatter {
  const lower = modelName.toLowerCase();
  if (lower.includes("anthropic") || lower.includes("claude")) return new AnthropicFormatter();
  if (lower.includes("qwen") || lower.includes("qwq")) return new QwenFormatter();
  if (lower.includes("deepseek")) return new DeepSeekFormatter();
  if (lower.includes("glm") || lower.includes("chatglm")) return new GLMFormatter();
  if (lower.includes("gpt") || lower.includes("o1") || lower.includes("o3")) return new OpenAIFOrmatter();
  return new GenericFormatter();
}
