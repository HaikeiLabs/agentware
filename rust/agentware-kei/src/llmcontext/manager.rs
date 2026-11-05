//! Context-manager seam (port of `go/llmcontext/manager.go`).

use crate::llm::Message;
use serde_json::Map;
use serde_json::Value;

/// Manages the durable conversation history for an agent run (Go
/// `ContextManager`). The default implementation writes each entry to a file,
/// making the run recoverable across process restarts.
pub trait ContextManager {
    /// Records an outbound prompt message.
    fn append_prompt(&self, job_id: &str, message: &Message) -> Result<(), String>;

    /// Records an inbound LLM response.
    fn append_response(&self, job_id: &str, message: &Message) -> Result<(), String>;

    /// Records the parsed tool calls for this round.
    fn append_tool_calls(
        &self,
        job_id: &str,
        calls: &[crate::toolformat::ParsedToolCall],
    ) -> Result<(), String>;

    /// Records the results of tool executions for this round.
    fn append_tool_results(&self, job_id: &str, results: &[ToolResultEntry]) -> Result<(), String>;

    /// Reconstructs the full message history for a job.
    fn get_history(&self, job_id: &str) -> Result<Vec<Message>, String>;

    /// Deletes all context for a job (call after completion in production).
    fn purge(&self, job_id: &str) -> Result<(), String>;
}

/// Pairs a tool call with its result for context storage (Go
/// `ToolResultEntry`).
#[derive(Debug, Clone, PartialEq, Default)]
pub struct ToolResultEntry {
    pub call_id: String,
    pub tool_name: String,
    pub args: Map<String, Value>,
    pub output: String,
    pub success: bool,
}
