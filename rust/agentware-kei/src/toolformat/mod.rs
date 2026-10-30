//! Model-family tool formatters: render tool definitions for a model prompt
//! and parse tool calls out of a model response.
//!
//! Port of `go/toolformat`. The [`Formatter`] trait is implemented once per
//! model family (openai/generic, anthropic, deepseek, qwen, glm, llama,
//! mistral, minimax, nemotron); [`for_model`] picks the family from a model id
//! by substring, mirroring Go's `GetFormatter`.

pub mod anthropic;
pub mod deepseek;
pub mod generic;
pub mod glm;
pub mod llama;
pub mod minimax;
pub mod mistral;
pub mod nemotron;
pub mod openai;
pub mod qwen;

use serde_json::{Map, Value};

pub use anthropic::AnthropicFormatter;
pub use deepseek::DeepSeekFormatter;
pub use generic::GenericFormatter;
pub use glm::GLMFormatter;
pub use llama::LlamaFormatter;
pub use minimax::MiniMaxFormatter;
pub use mistral::MistralFormatter;
pub use nemotron::NemotronFormatter;
pub use openai::OpenAIFormatter;
pub use qwen::QwenFormatter;

/// A tool definition as the model sees it (mirrors Go `tools.Tool`:
/// name, description, JSON input schema).
#[derive(Debug, Clone, PartialEq)]
pub struct ToolDef {
    pub name: String,
    pub description: String,
    pub input_schema: Option<Map<String, Value>>,
}

/// Result of executing a tool (mirrors Go `tools.Result`).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ToolResult {
    pub success: bool,
    pub output: String,
    pub error: String,
}

/// A tool call parsed from a model response (mirrors Go `ParsedToolCall`).
#[derive(Debug, Clone, PartialEq)]
pub struct ParsedToolCall {
    pub id: String,
    pub name: String,
    pub args: Map<String, Value>,
    pub raw: String,
}

/// Decodes the JSON argument payload embedded in a tag-based tool call
/// (anthropic/qwen/llama/minimax). Empty or malformed arguments are an error
/// — Go kept them as a `_raw` fallback; this port fails closed instead.
pub(super) fn decode_tag_arguments(
    name: &str,
    args_raw: &str,
) -> Result<Map<String, Value>, String> {
    if args_raw.is_empty() {
        return Err(format!("tool call {name:?} has missing arguments"));
    }
    match serde_json::from_str::<Value>(args_raw) {
        Ok(Value::Object(map)) => Ok(map),
        Ok(_) => Err(format!(
            "tool call {name:?} arguments must be a JSON object"
        )),
        Err(error) => Err(format!(
            "tool call {name:?} has invalid JSON arguments: {error}"
        )),
    }
}

/// Formats tool definitions and parses tool calls for one model family.
pub trait Formatter {
    /// Render the tool-definitions block for the model prompt.
    /// Empty input renders as an empty string.
    fn format_tools(&self, tools: &[ToolDef]) -> String;

    /// Parse tool calls from a model response. An empty response yields an
    /// empty list; malformed tool-call arguments are an error, never a
    /// silently dropped call.
    fn parse_tool_calls(&self, response: &str) -> Result<Vec<ParsedToolCall>, String>;

    /// Render a tool result for the next model turn.
    fn format_tool_result(&self, name: &str, result: &ToolResult) -> String;

    /// Family identifier, e.g. `"openai"`, `"glm"`.
    fn model_family(&self) -> &'static str;

    /// Validate that a response is well-formed for this family.
    fn validate_format(&self, response: &str) -> Result<(), String>;
}

/// Select the formatter for a model id by substring (case-insensitive),
/// mirroring Go's `GetFormatter` precedence. Families not yet ported fall
/// through to the generic default.
pub fn for_model(model_name: &str) -> Box<dyn Formatter> {
    let lower = model_name.to_lowercase();
    if lower.contains("nemotron") {
        Box::new(NemotronFormatter)
    } else if lower.contains("qwen") || lower.contains("qwq") {
        Box::new(QwenFormatter)
    } else if lower.contains("llama") {
        Box::new(LlamaFormatter)
    } else if lower.contains("mistral") {
        Box::new(MistralFormatter)
    } else if lower.contains("anthropic") || lower.contains("claude") {
        Box::new(AnthropicFormatter)
    } else if lower.contains("deepseek") {
        Box::new(DeepSeekFormatter)
    } else if lower.contains("glm") || lower.contains("chatglm") {
        Box::new(GLMFormatter)
    } else if lower.contains("gpt") || lower.contains("o1") || lower.contains("o3") {
        Box::new(OpenAIFormatter)
    } else {
        Box::new(GenericFormatter)
    }
}
