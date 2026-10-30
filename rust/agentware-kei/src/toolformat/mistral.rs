//! Mistral tool-call format: `[TOOL_CALLS][tool_call>name: {json}]
//! [/TOOL_CALLS]` blocks with a `[TOOL_DEFINITIONS]` listing.

use std::sync::LazyLock;

use regex::Regex;
use serde_json::Value;

use super::{Formatter, ParsedToolCall, ToolDef, ToolResult, decode_tag_arguments};

/// `[TOOL_CALLS][…][/TOOL_CALLS]` wrapper; group 1 is the inner entry list.
/// Byte-for-byte Go's `mistralToolCallRegex` (non-greedy, not dot-all).
static OUTER_CALL_RE: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r"\[TOOL_CALLS\]\s*\[(.*?)\]\s*\[/TOOL_CALLS\]")
        .expect("valid mistral tool-call regex")
});
/// One `tool_call>name: {json}` entry inside the wrapper, byte-for-byte Go's
/// `mistralToolRegex` (Go's `\w` is ASCII-only, hence the explicit class).
static ENTRY_RE: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r"tool_call>\s*([0-9A-Za-z_]+)\s*:\s*(\{.*?\})").expect("valid mistral entry regex")
});

/// Formats tools and parses tool calls for the Mistral family
/// (Mistral 7B, Mistral Large, ...).
pub struct MistralFormatter;

impl Formatter for MistralFormatter {
    fn format_tools(&self, tools: &[ToolDef]) -> String {
        if tools.is_empty() {
            return String::new();
        }

        let mut result = String::from("[TOOL_DEFINITIONS]\n");
        for tool in tools {
            result.push_str(&format!("{{{}: {}}}", tool.name, tool.description));
            if let Some(schema) = &tool.input_schema {
                let schema_json = Value::Object(schema.clone()).to_string();
                result.push_str(&format!(" | params: {schema_json}"));
            }
            result.push('\n');
        }
        result.push_str("[/TOOL_DEFINITIONS]\n");
        result
    }

    fn parse_tool_calls(&self, response: &str) -> Result<Vec<ParsedToolCall>, String> {
        if response.is_empty() {
            return Ok(Vec::new());
        }

        if !OUTER_CALL_RE.is_match(response) {
            return Ok(Vec::new());
        }

        // An outer match returns immediately, even with no inner entries.
        let mut calls = Vec::new();
        for outer in OUTER_CALL_RE.captures_iter(response) {
            let inner = &outer[1];
            for entry in ENTRY_RE.captures_iter(inner) {
                let name = &entry[1];
                let args_raw = &entry[2];
                // Go kept malformed payloads as a `_raw` fallback; this port
                // fails closed instead.
                let args = decode_tag_arguments(name, args_raw)?;
                calls.push(ParsedToolCall {
                    id: String::new(),
                    name: name.to_owned(),
                    args,
                    raw: entry[0].to_owned(),
                });
            }
        }
        Ok(calls)
    }

    fn format_tool_result(&self, name: &str, result: &ToolResult) -> String {
        if result.success {
            format!("[TOOL_RESULT]\n{name}\n{}\n[/TOOL_RESULT]", result.output)
        } else {
            format!(
                "[TOOL_RESULT]\n{name}\nerror: {}\n[/TOOL_RESULT]",
                result.error
            )
        }
    }

    fn model_family(&self) -> &'static str {
        "mistral"
    }

    fn validate_format(&self, response: &str) -> Result<(), String> {
        if response.is_empty() {
            return Ok(());
        }

        for outer in OUTER_CALL_RE.captures_iter(response) {
            let inner = &outer[1];
            for entry in ENTRY_RE.captures_iter(inner) {
                let name = &entry[1];
                let args_raw = &entry[2];
                if name.is_empty() {
                    return Err("tool call missing function name".to_owned());
                }
                if args_raw.is_empty() {
                    return Err(format!("tool call {name:?} missing arguments"));
                }
                decode_tag_arguments(name, args_raw)?;
            }
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
        assert_eq!(MistralFormatter.format_tools(&[]), "");
    }

    #[test]
    fn format_definitions_wraps_in_tool_definitions_block() {
        let defs = MistralFormatter.format_tools(&[weather_tool()]);
        assert_eq!(
            defs,
            "[TOOL_DEFINITIONS]\n\
             {get_weather: Get current weather} | params: \
             {\"type\":\"object\",\"properties\":{\"location\":{\"type\":\"string\"}}}\n\
             [/TOOL_DEFINITIONS]\n"
        );
    }

    #[test]
    fn format_definitions_without_schema_omits_params() {
        let defs = MistralFormatter.format_tools(&[tool("get_weather", "Get current weather")]);
        assert_eq!(
            defs,
            "[TOOL_DEFINITIONS]\n{get_weather: Get current weather}\n[/TOOL_DEFINITIONS]\n"
        );
    }

    #[test]
    fn parse_empty_response_has_no_calls() {
        assert_eq!(MistralFormatter.parse_tool_calls("").unwrap(), Vec::new());
    }

    #[test]
    fn parse_no_tool_calls_is_empty() {
        let calls = MistralFormatter
            .parse_tool_calls("Just some regular text response")
            .unwrap();
        assert!(calls.is_empty());
    }

    #[test]
    fn parse_fuzz_seed_call() {
        // Ported from go/toolformat/fuzz_test.go (mistral-7b seed). The raw
        // span is the inner entry, not the outer wrapper.
        let response =
            "[TOOL_CALLS][tool_call>get_weather: {\"location\": \"Tokyo\"}][/TOOL_CALLS]";
        let calls = MistralFormatter.parse_tool_calls(response).unwrap();
        assert_eq!(calls.len(), 1);
        assert_eq!(calls[0].name, "get_weather");
        assert_eq!(calls[0].args["location"], "Tokyo");
        assert_eq!(
            calls[0].raw,
            "tool_call>get_weather: {\"location\": \"Tokyo\"}"
        );
    }

    #[test]
    fn parse_multiple_entries_in_one_block() {
        let response = "[TOOL_CALLS][tool_call>a: {\"x\":1} tool_call>b: {\"y\":2}][/TOOL_CALLS]";
        let calls = MistralFormatter.parse_tool_calls(response).unwrap();
        assert_eq!(calls.len(), 2);
        assert_eq!(calls[0].name, "a");
        assert_eq!(calls[1].name, "b");
    }

    #[test]
    fn parse_multiple_outer_blocks() {
        let response = "[TOOL_CALLS][tool_call>a: {\"x\":1}][/TOOL_CALLS] \
                        [TOOL_CALLS][tool_call>b: {\"y\":2}][/TOOL_CALLS]";
        let calls = MistralFormatter.parse_tool_calls(response).unwrap();
        assert_eq!(calls.len(), 2);
        assert_eq!(calls[0].name, "a");
        assert_eq!(calls[1].name, "b");
    }

    #[test]
    fn parse_outer_match_without_entries_is_empty() {
        let calls = MistralFormatter
            .parse_tool_calls("[TOOL_CALLS][][/TOOL_CALLS]")
            .unwrap();
        assert!(calls.is_empty());
    }

    #[test]
    fn parse_empty_json_arguments_are_kept() {
        let calls = MistralFormatter
            .parse_tool_calls("[TOOL_CALLS][tool_call>t: {}][/TOOL_CALLS]")
            .unwrap();
        assert_eq!(calls.len(), 1);
        assert!(calls[0].args.is_empty());
    }

    #[test]
    fn parse_malformed_arguments_is_an_error() {
        assert!(
            MistralFormatter
                .parse_tool_calls("[TOOL_CALLS][tool_call>t: {bad}][/TOOL_CALLS]")
                .is_err()
        );
    }

    #[test]
    fn format_result_success_wraps_in_tool_result() {
        let result = ToolResult {
            success: true,
            output: "Sunny, 22C".to_owned(),
            error: String::new(),
        };
        assert_eq!(
            MistralFormatter.format_tool_result("get_weather", &result),
            "[TOOL_RESULT]\nget_weather\nSunny, 22C\n[/TOOL_RESULT]"
        );
    }

    #[test]
    fn format_result_error_wraps_in_tool_result() {
        let result = ToolResult {
            success: false,
            output: String::new(),
            error: "API unavailable".to_owned(),
        };
        assert_eq!(
            MistralFormatter.format_tool_result("get_weather", &result),
            "[TOOL_RESULT]\nget_weather\nerror: API unavailable\n[/TOOL_RESULT]"
        );
    }

    #[test]
    fn model_family_is_mistral() {
        assert_eq!(MistralFormatter.model_family(), "mistral");
    }

    #[test]
    fn validate_format_accepts_valid_and_empty() {
        assert!(
            MistralFormatter
                .validate_format(
                    "[TOOL_CALLS][tool_call>get_weather: {\"location\": \"Tokyo\"}][/TOOL_CALLS]"
                )
                .is_ok()
        );
        assert!(MistralFormatter.validate_format("").is_ok());
    }

    #[test]
    fn validate_format_rejects_invalid_json_arguments() {
        assert!(
            MistralFormatter
                .validate_format("[TOOL_CALLS][tool_call>t: {bad}][/TOOL_CALLS]")
                .is_err()
        );
    }

    #[test]
    fn selector_maps_mistral_models() {
        // Ported from go TestGetFormatter's mistral rows.
        assert_eq!(for_model("mistral-large").model_family(), "mistral");
        assert_eq!(for_model("Mistral-Large").model_family(), "mistral");
    }
}
