//! Qwen tool-call format: tool calls wrapped in plain ASCII `<tool_call>...</tool_call>`
//! blocks, each containing a `<tool name="...">` tag with a JSON argument
//! payload.

use std::sync::LazyLock;

use regex::Regex;
use serde_json::Value;

use super::{Formatter, ParsedToolCall, ToolDef, ToolResult, decode_tag_arguments};

/// One `<tool_call><tool name="...">args</tool></tool_call>` block, byte-for-byte Go's
/// `qwenToolCallRegex` (non-greedy, not dot-all: `.` never matches `\n`).
static TOOL_CALL_RE: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r#"<tool_call>\s*<tool name="([^"]+)">\s*(.*?)\s*</tool>\s*</tool_call>"#)
        .expect("valid qwen tool-call regex")
});
/// Any `<tool ...>` opening tag, with or without attributes.
static ANY_TOOL_RE: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"<tool\b[^>]*>").expect("valid qwen any-tool regex"));

/// Formats tools and parses tool calls for the Qwen family
/// (Qwen2.5, QwQ, ...).
pub struct QwenFormatter;

impl Formatter for QwenFormatter {
    fn format_tools(&self, tools: &[ToolDef]) -> String {
        if tools.is_empty() {
            return String::new();
        }

        let mut result = String::new();
        for tool in tools {
            result.push_str("<tool_description>\n");
            result.push_str(&format!("<tool_name>{}</tool_name>\n", tool.name));
            result.push_str(&format!(
                "<description>{}</description>\n",
                tool.description
            ));
            if let Some(schema) = &tool.input_schema {
                let schema_json = Value::Object(schema.clone()).to_string();
                result.push_str(&format!("<parameters>{schema_json}</parameters>\n"));
            }
            result.push_str("</tool_description>\n");
        }
        result
    }

    fn parse_tool_calls(&self, response: &str) -> Result<Vec<ParsedToolCall>, String> {
        if response.is_empty() {
            return Ok(Vec::new());
        }

        let mut calls = Vec::new();
        for caps in TOOL_CALL_RE.captures_iter(response) {
            let name = &caps[1];
            let args_raw = &caps[2];
            // Go kept malformed payloads as a `_raw` fallback; this port
            // fails closed instead.
            let args = decode_tag_arguments(name, args_raw)?;
            calls.push(ParsedToolCall {
                id: String::new(),
                name: name.to_owned(),
                args,
                raw: caps[0].to_owned(),
            });
        }
        Ok(calls)
    }

    fn format_tool_result(&self, name: &str, result: &ToolResult) -> String {
        if result.success {
            format!(
                "<tool_response>\n<tool_name>{name}</tool_name>\n<result>{}</result>\n</tool_response>",
                result.output
            )
        } else {
            format!(
                "<tool_response>\n<tool_name>{name}</tool_name>\n<error>{}</error>\n</tool_response>",
                result.error
            )
        }
    }

    fn model_family(&self) -> &'static str {
        "qwen"
    }

    fn validate_format(&self, response: &str) -> Result<(), String> {
        if response.is_empty() {
            return Ok(());
        }

        if ANY_TOOL_RE.is_match(response) && !TOOL_CALL_RE.is_match(response) {
            return Err("invalid tool tag: missing 'name' attribute".to_owned());
        }

        for caps in TOOL_CALL_RE.captures_iter(response) {
            let name = &caps[1];
            let args_raw = &caps[2];
            if name.is_empty() {
                return Err("tool call missing function name".to_owned());
            }
            if args_raw.is_empty() {
                return Err(format!("tool call {name:?} missing arguments"));
            }
            decode_tag_arguments(name, args_raw)?;
        }
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn tool(name: &str, description: &str) -> ToolDef {
        ToolDef {
            name: name.to_owned(),
            description: description.to_owned(),
            input_schema: None,
        }
    }

    /// Build one `<tool_call><tool name="...">args</tool></tool_call>` block.
    fn call(name: &str, args: &str) -> String {
        format!("<tool_call><tool name=\"{name}\">{args}</tool></tool_call>")
    }

    #[test]
    fn format_definitions_empty_tools_is_empty() {
        assert_eq!(QwenFormatter.format_tools(&[]), "");
    }

    #[test]
    fn format_definitions_single_tool_is_non_empty() {
        let defs = QwenFormatter.format_tools(&[tool("my_tool", "Does something")]);
        assert!(!defs.is_empty());
        assert!(defs.contains("<tool_name>my_tool</tool_name>"));
    }

    #[test]
    fn parse_empty_response_has_no_calls() {
        assert_eq!(QwenFormatter.parse_tool_calls("").unwrap(), Vec::new());
    }

    #[test]
    fn parse_valid_tool_call() {
        let calls = QwenFormatter
            .parse_tool_calls(&call("my_tool", "{\"arg1\": \"value1\"}"))
            .unwrap();
        assert_eq!(calls.len(), 1);
        assert_eq!(calls[0].name, "my_tool");
        assert_eq!(calls[0].args["arg1"], "value1");
        assert!(calls[0].id.is_empty());
    }

    #[test]
    fn parse_multiple_tool_calls() {
        let response = format!("{}{}", call("a", "{\"x\":1}"), call("b", "{\"y\":2}"));
        let calls = QwenFormatter.parse_tool_calls(&response).unwrap();
        assert_eq!(calls.len(), 2);
        assert_eq!(calls[0].name, "a");
        assert_eq!(calls[1].name, "b");
    }

    #[test]
    fn parse_no_tool_calls_is_empty() {
        let calls = QwenFormatter
            .parse_tool_calls("Just some regular text response")
            .unwrap();
        assert!(calls.is_empty());
    }

    #[test]
    fn parse_malformed_arguments_is_an_error() {
        assert!(
            QwenFormatter
                .parse_tool_calls(&call("t", "bad-json"))
                .is_err()
        );
        assert!(QwenFormatter.parse_tool_calls(&call("t", "")).is_err());
    }

    #[test]
    fn format_result_success_is_non_empty() {
        let result = ToolResult {
            success: true,
            output: "executed successfully".to_owned(),
            error: String::new(),
        };
        let output = QwenFormatter.format_tool_result("my_tool", &result);
        assert!(!output.is_empty());
        assert!(output.contains("<result>executed successfully</result>"));
    }

    #[test]
    fn format_result_error_is_non_empty() {
        let result = ToolResult {
            success: false,
            output: String::new(),
            error: "something failed".to_owned(),
        };
        let output = QwenFormatter.format_tool_result("my_tool", &result);
        assert!(!output.is_empty());
        assert!(output.contains("<error>something failed</error>"));
    }

    #[test]
    fn model_family_is_qwen() {
        assert_eq!(QwenFormatter.model_family(), "qwen");
    }

    #[test]
    fn validate_format_accepts_valid_and_empty() {
        assert!(
            QwenFormatter
                .validate_format(&call("get_weather", "{\"loc\":\"NYC\"}"))
                .is_ok()
        );
        assert!(QwenFormatter.validate_format("").is_ok());
    }

    #[test]
    fn validate_format_rejects_malformed_calls() {
        let missing_name = "<tool_call><tool>{\"loc\":\"NYC\"}</tool></tool_call>";
        assert!(QwenFormatter.validate_format(missing_name).is_err());
        assert!(QwenFormatter.validate_format(&call("t", "")).is_err());
        assert!(
            QwenFormatter
                .validate_format(&call("t", "bad-json"))
                .is_err()
        );
    }
}
