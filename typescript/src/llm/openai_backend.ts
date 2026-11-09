import type { Message, ToolDefinition } from "./request.js";
import { Role } from "./request.js";
import type { Response, ToolCall, TokenUsage } from "./response.js";
import type { AsyncBackend } from "./backend.js";

export interface FetchLikeResponse {
  ok: boolean;
  status: number;
  text(): Promise<string>;
}

export interface FetchLikeInit {
  method?: string;
  headers?: Record<string, string>;
  body?: string;
  signal?: AbortSignal;
}

export type FetchFn = (
  url: string,
  init?: FetchLikeInit
) => Promise<FetchLikeResponse>;

export class OpenAIError extends Error {
  readonly status?: number;

  constructor(message: string, status?: number) {
    super(message);
    this.name = "OpenAIError";
    this.status = status;
  }
}

export interface OpenAIBackendConfig {
  model: string;
  baseUrl?: string;
  apiKey?: string;
  fetchFn?: FetchFn;
  timeoutMs?: number;
  maxResponseBytes?: number;
  maxToolArgBytes?: number;
  contextWindowSize?: number;
  temperature?: number;
  maxTokens?: number;
}

const DEFAULT_BASE_URL = "https://api.openai.com/v1";
const DEFAULT_TIMEOUT_MS = 60_000;
const DEFAULT_MAX_RESPONSE_BYTES = 1_048_576;
const DEFAULT_MAX_TOOL_ARG_BYTES = 65_536;
const DEFAULT_CONTEXT_WINDOW = 128_000;
const MAX_ERROR_DETAIL_CHARS = 512;

interface OpenAIChatMessage {
  role: string;
  content?: string | null;
  reasoning_content?: string | null;
  tool_call_id?: string;
  tool_calls?: Array<{
    id: string;
    type: "function";
    function: { name: string; arguments: string };
    extra_content?: unknown;
  }>;
}

interface OpenAIChatCompletion {
  choices?: Array<{
    finish_reason?: string;
    message?: {
      content?: string | null;
      reasoning_content?: string | null;
      tool_calls?: Array<{
        id?: string;
        function?: { name?: string; arguments?: string };
        extra_content?: unknown;
      }>;
    };
  }>;
  usage?: {
    prompt_tokens?: number;
    completion_tokens?: number;
    total_tokens?: number;
  };
}

export function parseToolArguments(
  raw: string | null | undefined,
  maxBytes: number
): Record<string, unknown> {
  if (!raw) {
    return {};
  }
  const bounded = raw.length > maxBytes ? raw.slice(0, maxBytes) : raw;
  try {
    const parsed: unknown = JSON.parse(bounded);
    if (typeof parsed === "object" && parsed !== null && !Array.isArray(parsed)) {
      return parsed as Record<string, unknown>;
    }
  } catch {
    // fall through to raw fallback
  }
  return { _raw: bounded };
}

function toOpenAIMessage(message: Message): OpenAIChatMessage {
  switch (message.role) {
    case Role.TOOL:
      return {
        role: "tool",
        tool_call_id: message.tool_call_id ?? "",
        content: message.content,
      };
    case Role.ASSISTANT:
      if (message.tool_calls && message.tool_calls.length > 0) {
        return {
          role: "assistant",
          content: message.content || null,
          tool_calls: message.tool_calls.map((tc) => ({
            id: tc.id,
            type: "function",
            function: { name: tc.name, arguments: JSON.stringify(tc.arguments) },
            ...(tc.metadata !== undefined ? { extra_content: tc.metadata } : {}),
          })),
        };
      }
      return { role: "assistant", content: message.content };
    default:
      return { role: message.role, content: message.content };
  }
}

function toOpenAITool(tool: ToolDefinition): Record<string, unknown> {
  return {
    type: "function",
    function: {
      name: tool.name,
      description: tool.description,
      parameters: tool.input_schema,
    },
  };
}

function truncateDetail(text: string): string {
  if (text.length <= MAX_ERROR_DETAIL_CHARS) {
    return text;
  }
  return text.slice(0, MAX_ERROR_DETAIL_CHARS) + "...";
}

function toError(err: unknown): Error {
  return err instanceof Error ? err : new Error(String(err));
}

export class OpenAIBackend implements AsyncBackend {
  private readonly model: string;
  private readonly baseUrl: string;
  private readonly apiKey: string;
  private readonly fetchFn: FetchFn;
  private readonly timeoutMs: number;
  private readonly maxResponseBytes: number;
  private readonly maxToolArgBytes: number;
  private readonly contextWindow: number;
  private readonly temperature?: number;
  private readonly maxTokens?: number;

  constructor(config: OpenAIBackendConfig) {
    this.model = config.model;
    this.baseUrl = (config.baseUrl ?? DEFAULT_BASE_URL).replace(/\/+$/, "");
    this.apiKey = config.apiKey ?? "";
    this.fetchFn = config.fetchFn ?? fetch;
    this.timeoutMs = config.timeoutMs ?? DEFAULT_TIMEOUT_MS;
    this.maxResponseBytes = config.maxResponseBytes ?? DEFAULT_MAX_RESPONSE_BYTES;
    this.maxToolArgBytes = config.maxToolArgBytes ?? DEFAULT_MAX_TOOL_ARG_BYTES;
    this.contextWindow = config.contextWindowSize ?? DEFAULT_CONTEXT_WINDOW;
    this.temperature = config.temperature;
    this.maxTokens = config.maxTokens;
  }

  async complete(
    messages: Message[],
    tools?: ToolDefinition[]
  ): Promise<Response> {
    const payload: Record<string, unknown> = {
      model: this.model,
      messages: messages.map(toOpenAIMessage),
    };
    if (this.temperature !== undefined) {
      payload.temperature = this.temperature;
    }
    if (this.maxTokens !== undefined) {
      payload.max_tokens = this.maxTokens;
    }
    if (tools && tools.length > 0) {
      payload.tools = tools.map(toOpenAITool);
      payload.tool_choice = "auto";
    }

    const headers: Record<string, string> = {
      "Content-Type": "application/json",
    };
    if (this.apiKey) {
      headers.Authorization = `Bearer ${this.apiKey}`;
    }

    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), this.timeoutMs);

    try {
      let res: FetchLikeResponse;
      try {
        res = await this.fetchFn(`${this.baseUrl}/chat/completions`, {
          method: "POST",
          headers,
          body: JSON.stringify(payload),
          signal: controller.signal,
        });
      } catch (err) {
        if (
          controller.signal.aborted ||
          (err instanceof Error && err.name === "AbortError")
        ) {
          throw new OpenAIError(
            `request timed out after ${this.timeoutMs}ms`
          );
        }
        throw new OpenAIError(`request failed: ${toError(err).message}`);
      }

      const text = await res.text();

      if (text.length > this.maxResponseBytes) {
        throw new OpenAIError(
          `response body exceeds limit of ${this.maxResponseBytes} bytes`
        );
      }

      if (!res.ok) {
        throw new OpenAIError(
          `HTTP ${res.status}: ${truncateDetail(text)}`,
          res.status
        );
      }

      let data: unknown;
      try {
        data = JSON.parse(text);
      } catch {
        throw new OpenAIError("malformed JSON in response body");
      }

      return this.parseCompletion(data);
    } finally {
      clearTimeout(timer);
    }
  }

  private parseCompletion(data: unknown): Response {
    const completion = data as OpenAIChatCompletion;
    const choice = completion.choices?.[0];
    const message = choice?.message;

    if (!message) {
      throw new OpenAIError("unexpected response shape: missing choices[0].message");
    }

    const toolCalls: ToolCall[] = [];
    if (message.tool_calls) {
      for (let i = 0; i < message.tool_calls.length; i++) {
        const tc = message.tool_calls[i];
        const name = tc.function?.name;
        if (!name) {
          continue;
        }
        toolCalls.push({
          ...(tc.extra_content !== undefined ? { metadata: tc.extra_content } : {}),
          id: tc.id ?? `call_${i}`,
          name,
          arguments: parseToolArguments(
            tc.function?.arguments,
            this.maxToolArgBytes
          ),
        });
      }
    }

    const usage: TokenUsage = {
      prompt_tokens: completion.usage?.prompt_tokens ?? 0,
      completion_tokens: completion.usage?.completion_tokens ?? 0,
      total_tokens: completion.usage?.total_tokens ?? 0,
    };

    return {
      content: message.content ?? "",
      reasoning: message.reasoning_content ?? "",
      tool_calls: toolCalls,
      finish_reason: choice?.finish_reason ?? "",
      usage_tokens: usage,
    };
  }

  supportsNativeToolCalling(): boolean {
    return true;
  }

  modelName(): string {
    return this.model;
  }

  contextWindowSize(): number {
    return this.contextWindow;
  }
}
