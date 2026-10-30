//! Llama tool-call format: `<|python_tag|>name(args)` python-tag calls
//! with a free-text tool listing.

use std::sync::LazyLock;

use regex::Regex;
use serde_json::Value;

use super::{Formatter, ParsedToolCall, ToolDef, ToolResult, decode_tag_arguments};

/// One `<|python_tag|>name(args)` call, byte-for-byte Go's
/// `llamaToolCallRegex` (non-greedy, not dot-all: `.` never matches `\n`).
static TOOL_CALL_RE: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r"<\|python_tag\|>\s*([a-zA-Z_][a-zA-Z0-9_]*)\((.*?)\)")
        .expect("valid llama tool-call regex")
});

/// Formats tools and parses tool calls for the Llama family
/// (Llama 3.x, ...).
pub struct LlamaFormatter;

impl Formatter for LlamaFormatter {
    fn format_tools(&self, tools: &[ToolDef]) -> String {
        if tools.is_empty() {
            return String::new();
        }

        let mut result = String::from("Available tools:\n");
        for tool in tools {
            result.push_str(&format!("- {}: {}\n", tool.name, tool.description));
            if let Some(schema) = &tool.input_schema {
                let schema_json = Value::Object(schema.clone()).to_string();
                result.push_str(&format!("  Parameters: {schema_json}\n"));
            }
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
            format!("{name}: {}", result.output)
        } else {
            format!("{name} error: {}", result.error)
        }
    }

    fn model_family(&self) -> &'static str {
        "llama"
    }

    fn validate_format(&self, response: &str) -> Result<(), String> {
        if response.is_empty() {
            return Ok(());
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
    use crate::toolformat::for_model;

    fn tool(name: &str, description: &str) -> ToolDef {
        ToolDef {
            name: name.to_owned(),
            description: description.to_owned(),
            input_schema: None,
        }
    }

    fn weather_tool() -> ToolDef {
        ToolDef {
            name: "get_weather".to_owned(),
            description: "Get current weather".to_owned(),
            input_schema: Some(
                serde_json::from_str(
                    r#"{"type":"object","properties":{"location":{"type":"string"}}}"#,
                )
                .expect("valid schema"),
            ),
        }
    }

    #[test]
    fn format_definitions_empty_tools_is_empty() {
        assert_eq!(LlamaFormatter.format_tools(&[]), "");
    }

    #[test]
    fn format_definitions_lists_available_tools() {
        let defs = LlamaFormatter.format_tools(&[weather_tool()]);
        assert_eq!(
            defs,
            "Available tools:\n\
             - get_weather: Get current weather\n\
             \x20 Parameters: {\"type\":\"object\",\"properties\":{\"location\":{\"type\":\"string\"}}}\n"
        );
    }

    #[test]
    fn format_definitions_without_schema_omits_parameters_line() {
        let defs = LlamaFormatter.format_tools(&[tool("get_weather", "Get current weather")]);
        assert_eq!(
            defs,
            "Available tools:\n- get_weather: Get current weather\n"
        );
    }

    #[test]
    fn parse_empty_response_has_no_calls() {
        assert_eq!(LlamaFormatter.parse_tool_calls("").unwrap(), Vec::new());
    }

    #[test]
    fn parse_no_tool_calls_is_empty() {
        let calls = LlamaFormatter
            .parse_tool_calls("Just some regular text response")
            .unwrap();
        assert!(calls.is_empty());
    }

    #[test]
    fn parse_python_tag_call() {
        // Ported from go/toolformat/fuzz_test.go (llama-3 seed).
        let calls = LlamaFormatter
            .parse_tool_calls("<|python_tag|>get_weather({\"location\": \"Tokyo\"})")
            .unwrap();
        assert_eq!(calls.len(), 1);
        assert_eq!(calls[0].name, "get_weather");
        assert_eq!(calls[0].args["location"], "Tokyo");
        assert!(calls[0].id.is_empty());
    }

    #[test]
    fn parse_multiple_tool_calls() {
        let response = "<|python_tag|>a({\"x\":1})<|python_tag|>b({\"y\":2})";
        let calls = LlamaFormatter.parse_tool_calls(response).unwrap();
        assert_eq!(calls.len(), 2);
        assert_eq!(calls[0].name, "a");
        assert_eq!(calls[1].name, "b");
    }

    #[test]
    fn parse_malformed_arguments_is_an_error() {
        assert!(
            LlamaFormatter
                .parse_tool_calls("<|python_tag|>get_weather(bad-json)")
                .is_err()
        );
        assert!(
            LlamaFormatter
                .parse_tool_calls("<|python_tag|>get_weather()")
                .is_err()
        );
    }

    #[test]
    fn format_result_success_is_name_colon_output() {
        let result = ToolResult {
            success: true,
            output: "Sunny, 22C".to_owned(),
            error: String::new(),
        };
        assert_eq!(
            LlamaFormatter.format_tool_result("get_weather", &result),
            "get_weather: Sunny, 22C"
        );
    }

    #[test]
    fn format_result_error_is_name_error_message() {
        let result = ToolResult {
            success: false,
            output: String::new(),
            error: "API unavailable".to_owned(),
        };
        assert_eq!(
            LlamaFormatter.format_tool_result("get_weather", &result),
            "get_weather error: API unavailable"
        );
    }

    #[test]
    fn model_family_is_llama() {
        assert_eq!(LlamaFormatter.model_family(), "llama");
    }

    #[test]
    fn validate_format_accepts_valid_and_empty() {
        assert!(
            LlamaFormatter
                .validate_format("<|python_tag|>get_weather({\"location\": \"Tokyo\"})")
                .is_ok()
        );
        assert!(LlamaFormatter.validate_format("").is_ok());
    }

    #[test]
    fn validate_format_rejects_missing_or_malformed_arguments() {
        assert!(
            LlamaFormatter
                .validate_format("<|python_tag|>get_weather()")
                .is_err()
        );
        assert!(
            LlamaFormatter
                .validate_format("<|python_tag|>get_weather(bad-json)")
                .is_err()
        );
    }

    #[test]
    fn selector_maps_llama_models() {
        // Ported from go TestGetFormatter's llama rows.
        assert_eq!(for_model("llama-3").model_family(), "llama");
        assert_eq!(for_model("Llama-3").model_family(), "llama");
    }
}
