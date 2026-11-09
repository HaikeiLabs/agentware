//! LLM request/response types, context-window accounting, compaction, and an
//! OpenAI-compatible HTTP backend (port of `go/llm`).
//!
//! Wire-format note: message serialization follows the OpenAI snake_case
//! mapping from `go/llm/proxy/convert.go` (`ToOpenAIMessages`), not Go's
//! `serverBackend` default-field marshal; see `server.rs`.

pub mod anthropic_native;
pub mod backend;
pub mod compact;
pub mod context_window;
pub mod factory;
pub mod request;
pub mod response;
pub mod server;
pub mod tokens;

pub use anthropic_native::{
    format_anthropic_messages, format_anthropic_tools, parse_anthropic_response,
};
pub use backend::Backend;
pub use compact::{
    CompactError, CompactEvent, CompactionStrategy, PhaseReporter, TieredCompact, TokenCounter,
};
pub use context_window::{
    CompactCallback, ContextWindowManager, ThresholdCallback, default_counter,
    default_threshold_callback,
};
pub use factory::{Config, new_backend};
pub use request::{
    Message, MessageMeta, MessageType, Request, Role, ThinkingConfig, ToolDefinition,
};
pub use response::{Response, TokenUsage, ToolCall};
pub use server::{ServerBackend, ServerConfig};
pub use tokens::{
    estimate_messages_tokens, estimate_tokens, format_go_value, get_model_context_window,
};
