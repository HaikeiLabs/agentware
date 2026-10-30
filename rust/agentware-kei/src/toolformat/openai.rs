//! OpenAI function-calling formatter (family `"openai"`).
//!
//! Identical wire behavior to the generic engine; the separate family keeps
//! model-profile routing observable (`gpt-*`, `o1*`, `o3*`).

use super::generic;
use super::{Formatter, ParsedToolCall, ToolDef, ToolResult};

/// OpenAI-compatible chat models.
pub struct OpenAIFormatter;

impl Formatter for OpenAIFormatter {
    fn format_tools(&self, tools: &[ToolDef]) -> String {
        generic::format_function_definitions(tools)
    }

    fn parse_tool_calls(&self, response: &str) -> Result<Vec<ParsedToolCall>, String> {
        generic::parse_json_tool_calls(response)
    }

    fn format_tool_result(&self, _name: &str, result: &ToolResult) -> String {
        generic::format_plain_result(result)
    }

    fn model_family(&self) -> &'static str {
        "openai"
    }

    fn validate_format(&self, response: &str) -> Result<(), String> {
        generic::validate_json_tool_calls(response)
    }
}
