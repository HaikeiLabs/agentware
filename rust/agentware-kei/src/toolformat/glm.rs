//! GLM function-calling formatter (family `"glm"`).
//!
//! GLM-4 uses standard OpenAI-compatible JSON function calling. GLM-5.3-Flash
//! on vLLM (verified 2026-10-29) uses the same shape with `function.arguments`
//! as a JSON string and `chatcmpl-tool-<hex>` ids — both argument forms decode
//! through the shared generic engine.

use super::generic;
use super::{Formatter, ParsedToolCall, ToolDef, ToolResult};

/// GLM and ChatGLM models.
pub struct GLMFormatter;

impl Formatter for GLMFormatter {
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
        "glm"
    }

    fn validate_format(&self, response: &str) -> Result<(), String> {
        generic::validate_json_tool_calls(response)
    }
}
