//! DeepSeek function-calling formatter (family `"deepseek"`).
//!
//! DeepSeek uses the standard OpenAI-compatible JSON function-calling shape,
//! so it shares the generic engine.

use super::generic;
use super::{Formatter, ParsedToolCall, ToolDef, ToolResult};

/// DeepSeek chat and reasoning models.
pub struct DeepSeekFormatter;

impl Formatter for DeepSeekFormatter {
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
        "deepseek"
    }

    fn validate_format(&self, response: &str) -> Result<(), String> {
        generic::validate_json_tool_calls(response)
    }
}
