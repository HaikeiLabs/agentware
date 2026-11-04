//! Backend abstraction (port of `go/llm/backend.go`).

use super::request::Request;
use super::response::Response;

/// The abstraction over any OpenAI-compatible LLM API (Go `Backend`).
pub trait Backend: Send + Sync {
    /// Sends a conversation to the LLM and returns its response.
    fn complete(&self, req: &Request) -> Result<Response, String>;

    /// Returns true if the backend can handle tool definitions in the request
    /// and return structured tool calls.
    fn supports_native_tool_calling(&self) -> bool;

    /// Returns the model identifier being used.
    fn model_name(&self) -> String;

    /// Returns the maximum number of tokens this backend can process per
    /// request. Returns 0 if unknown.
    fn context_window_size(&self) -> i64;
}
