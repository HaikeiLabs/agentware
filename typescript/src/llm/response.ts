export interface ToolCall {
  id: string;
  name: string;
  arguments: Record<string, unknown>;
  /** Opaque model-family tool-call metadata; do not log or display. */
  metadata?: unknown;
}

export interface TokenUsage {
  prompt_tokens: number;
  completion_tokens: number;
  total_tokens: number;
}

export interface Response {
  content: string;
  /** Raw reasoning returned on a separate channel (e.g. reasoning_content for
   * DeepSeek-style models). Never written into content, never returned to the
   * end user, and never recorded on a default audit record; consumers
   * normalize it with the reasoning adapter. */
  reasoning: string;
  tool_calls: ToolCall[];
  finish_reason: string;
  usage_tokens: TokenUsage;
}