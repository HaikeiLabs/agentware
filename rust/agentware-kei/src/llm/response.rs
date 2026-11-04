//! Response-side types (port of `go/llm/response.go`).

use serde_json::Map;

/// The output from a completion (Go `Response`).
#[derive(Debug, Clone, PartialEq, Default)]
pub struct Response {
    pub content: String,
    /// The raw reasoning the backend returned on a separate channel (e.g.
    /// `reasoning_content` for DeepSeek-style models) when the backend
    /// captures it. It is never written into `content`, never returned to the
    /// end user, and never recorded on a default audit record; consumers
    /// normalize it with the reasoning adapter (`go/reasoning`, ported in
    /// `crate::reasoning`).
    pub reasoning: String,
    pub tool_calls: Vec<ToolCall>,
    pub finish_reason: String,
    pub usage_tokens: TokenUsage,
}

/// A structured tool invocation from the LLM (Go `ToolCall`).
#[derive(Debug, Clone, PartialEq, Default)]
pub struct ToolCall {
    pub id: String,
    pub name: String,
    pub args: Map<String, serde_json::Value>,
}

/// Prompt and completion token counts (Go `TokenUsage`).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub struct TokenUsage {
    pub prompt_tokens: i64,
    pub completion_tokens: i64,
    pub total_tokens: i64,
    /// The subset of `prompt_tokens` served from the provider's prompt cache.
    /// Zero when the backend does not report cache usage.
    pub cached_tokens: i64,
}
