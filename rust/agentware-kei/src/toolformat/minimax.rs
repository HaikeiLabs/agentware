//! MiniMax tool-call format: XML-style `<tool_call><tool_name>…</tool_name><parameters>…</parameters></tool_call>`
//! blocks, inline `action|function_call|tool_call = name(args)` calls
//! (dot-all), or a JSON array of `{id, name, arguments}` calls.

use std::sync::LazyLock;

use regex::Regex;
use serde_json::Value;

use super::{Formatter, ParsedToolCall, ToolDef, ToolResult, decode_tag_arguments};

/// Inline `action=name(args)`-style call, byte-for-byte Go's
/// `minimaxToolCallRegex` (dot-all via `(?s)`: `.` matches `\n` here).
static INLINE_CALL_RE: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(
        r#"(?s)(?:action|function_call|tool_call)[\"']?\s*[:=]\s*[\"']?([a-zA-Z_][a-zA-Z0-9_]*)[\"']?\s*\((.*?)\)"#,
    )
    .expect("valid minimax inline tool-call regex")
});
/// `<tool_call><tool_name>…<parameters>…` block, byte-for-byte Go's
/// `minimaxToolCallRegex2` (non-greedy, not dot-all).
static BLOCK_CALL_RE: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r#"<tool_call>\s*<tool_name>([^<]+)</tool_name>\s*<parameters>(.*?)</parameters>\s*</tool_call>"#)
        .expect("valid minimax block tool-call regex")
});

/// Formats tools and parses tool calls for the MiniMax family
/// (MiniMax-Text, MiniMax-M1, ...).
pub struct MiniMaxFormatter;

impl Formatter for MiniMaxFormatter {
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
        let response = response.trim();

        // XML-style blocks win over everything else; a block match returns
        // immediately, even if every block was skipped for an empty name.
        if BLOCK_CALL_RE.is_match(response) {
            let mut calls = Vec::new();
            for caps in BLOCK_CALL_RE.captures_iter(response) {
                let name = caps[1].trim();
                if name.is_empty() {
                    continue;
                }
                let args = decode_tag_arguments(name, caps[2].trim())?;
                calls.push(ParsedToolCall {
                    id: String::new(),
                    name: name.to_owned(),
                    args,
                    raw: caps[0].to_owned(),
                });
            }
            return Ok(calls);
        }

        if INLINE_CALL_RE.is_match(response) {
            let mut calls = Vec::new();
            for caps in INLINE_CALL_RE.captures_iter(response) {
                let name = caps[1].trim();
                let args = decode_tag_arguments(name, caps[2].trim())?;
                calls.push(ParsedToolCall {
                    id: String::new(),
                    name: name.to_owned(),
                    args,
                    raw: caps[0].to_owned(),
                });
            }
            return Ok(calls);
        }

        // Fall back to a JSON array of {id, name, arguments} calls. Go's
        // typed unmarshal rejects the whole payload unless every element is
        // an object whose id/name fields are strings (or absent/null);
        // calls whose arguments are absent, null or not an object are
        // skipped.
        let Ok(entries) = serde_json::from_str::<Vec<Value>>(response) else {
            return Ok(Vec::new());
        };
        if entries.is_empty() {
            return Ok(Vec::new());
        }
        let mut calls = Vec::new();
        for entry in &entries {
            let Some(object) = entry.as_object() else {
                return Ok(Vec::new());
            };
            let id = match object.get("id") {
                None | Some(Value::Null) => String::new(),
                Some(Value::String(id)) => id.clone(),
                Some(_) => return Ok(Vec::new()),
            };
            let name = match object.get("name") {
                None | Some(Value::Null) => String::new(),
                Some(Value::String(name)) => name.clone(),
                Some(_) => return Ok(Vec::new()),
            };
            let Some(args) = object.get("arguments").and_then(Value::as_object) else {
                continue;
            };
            calls.push(ParsedToolCall {
                id,
                name,
                args: args.clone(),
                raw: response.to_owned(),
            });
        }
        Ok(calls)
    }

    fn format_tool_result(&self, name: &str, result: &ToolResult) -> String {
        if result.success {
            format!(
                " result<tool_name>{name}</tool_name><result>{}</result> result",
                result.output
            )
        } else {
            format!(
                " result<tool_name>{name}</tool_name><error>{}</error> result",
                result.error
            )
        }
    }

    fn model_family(&self) -> &'static str {
        "minimax"
    }

    fn validate_format(&self, response: &str) -> Result<(), String> {
        if response.is_empty() {
            return Ok(());
        }

        for call in self.parse_tool_calls(response)? {
            if call.name.is_empty() {
                return Err("tool call missing function name".to_owned());
            }
            if call.args.is_empty() {
                return Err(format!("tool call {:?} missing arguments", call.name));
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
        assert_eq!(MiniMaxFormatter.format_tools(&[]), "");
    }

    #[test]
    fn format_definitions_lists_available_tools() {
        let defs = MiniMaxFormatter.format_tools(&[weather_tool()]);
        assert_eq!(
            defs,
            "Available tools:\n\
             - get_weather: Get current weather\n\
             \x20 Parameters: {\"type\":\"object\",\"properties\":{\"location\":{\"type\":\"string\"}}}\n"
        );
    }

    #[test]
    fn format_definitions_without_schema_omits_parameters_line() {
        let defs = MiniMaxFormatter.format_tools(&[tool("get_weather", "Get current weather")]);
        assert_eq!(
            defs,
            "Available tools:\n- get_weather: Get current weather\n"
        );
    }

    #[test]
    fn parse_empty_response_has_no_calls() {
        assert_eq!(MiniMaxFormatter.parse_tool_calls("").unwrap(), Vec::new());
    }

    #[test]
    fn parse_no_tool_calls_is_empty() {
        let calls = MiniMaxFormatter
            .parse_tool_calls("Just some regular text response")
            .unwrap();
        assert!(calls.is_empty());
    }

    #[test]
    fn parse_block_call() {
        let calls = MiniMaxFormatter
            .parse_tool_calls(
                "<tool_call><tool_name>get_weather</tool_name><parameters>{\"location\":\"Tokyo\"}</parameters></tool_call>",
            )
            .unwrap();
        assert_eq!(calls.len(), 1);
        assert_eq!(calls[0].name, "get_weather");
        assert_eq!(calls[0].args["location"], "Tokyo");
        assert!(calls[0].id.is_empty());
    }

    #[test]
    fn parse_block_calls_trim_names_and_parameters() {
        let calls = MiniMaxFormatter
            .parse_tool_calls(
                "<tool_call> <tool_name>  get_weather  </tool_name>  <parameters>  {\"location\":\"Tokyo\"}  </parameters> </tool_call>",
            )
            .unwrap();
        assert_eq!(calls.len(), 1);
        assert_eq!(calls[0].name, "get_weather");
        assert_eq!(calls[0].args["location"], "Tokyo");
    }

    #[test]
    fn parse_multiple_block_calls() {
        let response = "<tool_call><tool_name>a</tool_name><parameters>{\"x\":1}</parameters></tool_call><tool_call><tool_name>b</tool_name><parameters>{\"y\":2}</parameters></tool_call>";
        let calls = MiniMaxFormatter.parse_tool_calls(response).unwrap();
        assert_eq!(calls.len(), 2);
        assert_eq!(calls[0].name, "a");
        assert_eq!(calls[1].name, "b");
    }

    #[test]
    fn parse_block_match_returns_without_falling_through() {
        // Go returns the (possibly empty) block result instead of trying the
        // inline or JSON forms when a block matched.
        let response = "<tool_call><tool_name>   </tool_name><parameters>{\"location\":\"Tokyo\"}</parameters></tool_call> action=get_weather({})";
        let calls = MiniMaxFormatter.parse_tool_calls(response).unwrap();
        assert!(calls.is_empty());
    }

    #[test]
    fn parse_inline_call() {
        let calls = MiniMaxFormatter
            .parse_tool_calls("action=get_weather({\"location\": \"Tokyo\"})")
            .unwrap();
        assert_eq!(calls.len(), 1);
        assert_eq!(calls[0].name, "get_weather");
        assert_eq!(calls[0].args["location"], "Tokyo");
    }

    #[test]
    fn parse_inline_call_with_quotes_and_colon() {
        let calls = MiniMaxFormatter
            .parse_tool_calls("function_call: 'get_weather'({\"location\": \"Tokyo\"})")
            .unwrap();
        assert_eq!(calls.len(), 1);
        assert_eq!(calls[0].name, "get_weather");
    }

    #[test]
    fn parse_inline_call_spans_lines_dot_all() {
        // Go's minimaxToolCallRegex carries (?s): multi-line arguments parse.
        let response = "action=get_weather({\n  \"location\": \"Tokyo\"\n})";
        let calls = MiniMaxFormatter.parse_tool_calls(response).unwrap();
        assert_eq!(calls.len(), 1);
        assert_eq!(calls[0].args["location"], "Tokyo");
    }

    #[test]
    fn parse_multiple_inline_calls() {
        let response = "action=a({\"x\":1}) action=b({\"y\":2})";
        let calls = MiniMaxFormatter.parse_tool_calls(response).unwrap();
        assert_eq!(calls.len(), 2);
        assert_eq!(calls[0].name, "a");
        assert_eq!(calls[1].name, "b");
    }

    #[test]
    fn parse_inline_malformed_arguments_is_an_error() {
        assert!(
            MiniMaxFormatter
                .parse_tool_calls("action=get_weather(bad-json)")
                .is_err()
        );
        assert!(
            MiniMaxFormatter
                .parse_tool_calls("action=get_weather()")
                .is_err()
        );
    }

    #[test]
    fn parse_json_call_array() {
        let response =
            r#"[{"id":"chatcmpl-1","name":"get_weather","arguments":{"location":"Tokyo"}}]"#;
        let calls = MiniMaxFormatter.parse_tool_calls(response).unwrap();
        assert_eq!(calls.len(), 1);
        assert_eq!(calls[0].id, "chatcmpl-1");
        assert_eq!(calls[0].name, "get_weather");
        assert_eq!(calls[0].args["location"], "Tokyo");
        assert_eq!(calls[0].raw, response);
    }

    #[test]
    fn parse_json_array_skips_calls_without_object_arguments() {
        let response = r#"[{"id":"1","name":"a","arguments":null},{"id":"2","name":"b","arguments":"{}"},{"id":"3","name":"c","arguments":{"x":1}}]"#;
        let calls = MiniMaxFormatter.parse_tool_calls(response).unwrap();
        assert_eq!(calls.len(), 1);
        assert_eq!(calls[0].name, "c");
    }

    #[test]
    fn parse_json_array_tolerates_null_id_and_keeps_empty_arguments() {
        let response = r#"[{"id":null,"name":"get_weather","arguments":{}}]"#;
        let calls = MiniMaxFormatter.parse_tool_calls(response).unwrap();
        assert_eq!(calls.len(), 1);
        assert!(calls[0].id.is_empty());
        assert!(calls[0].args.is_empty());
    }

    #[test]
    fn parse_json_non_arrays_and_mixed_elements_yield_no_calls() {
        assert_eq!(
            MiniMaxFormatter.parse_tool_calls("{\"a\":1}").unwrap(),
            Vec::new()
        );
        assert_eq!(MiniMaxFormatter.parse_tool_calls("[]").unwrap(), Vec::new());
        assert_eq!(
            MiniMaxFormatter.parse_tool_calls("[1]").unwrap(),
            Vec::new()
        );
    }

    #[test]
    fn format_result_success_wraps_in_tool_response() {
        let result = ToolResult {
            success: true,
            output: "Sunny, 22C".to_owned(),
            error: String::new(),
        };
        assert_eq!(
            MiniMaxFormatter.format_tool_result("get_weather", &result),
            " result<tool_name>get_weather</tool_name><result>Sunny, 22C</result> result"
        );
    }

    #[test]
    fn format_result_error_wraps_in_tool_response() {
        let result = ToolResult {
            success: false,
            output: String::new(),
            error: "API unavailable".to_owned(),
        };
        assert_eq!(
            MiniMaxFormatter.format_tool_result("get_weather", &result),
            " result<tool_name>get_weather</tool_name><error>API unavailable</error> result"
        );
    }

    #[test]
    fn model_family_is_minimax() {
        assert_eq!(MiniMaxFormatter.model_family(), "minimax");
    }

    #[test]
    fn validate_format_accepts_valid_and_empty() {
        assert!(MiniMaxFormatter
            .validate_format(
                "<tool_call><tool_name>get_weather</tool_name><parameters>{\"location\":\"Tokyo\"}</parameters></tool_call>",
            )
            .is_ok());
        assert!(
            MiniMaxFormatter
                .validate_format("action=get_weather({\"location\": \"Tokyo\"})")
                .is_ok()
        );
        assert!(MiniMaxFormatter.validate_format("").is_ok());
    }

    #[test]
    fn validate_format_rejects_missing_names_or_arguments() {
        // Empty arguments object: parse keeps the call, validate rejects it.
        assert!(MiniMaxFormatter
            .validate_format(
                "<tool_call><tool_name>get_weather</tool_name><parameters>{}</parameters></tool_call>",
            )
            .is_err());
        assert!(
            MiniMaxFormatter
                .validate_format(r#"[{"name":"","arguments":{}}]"#)
                .is_err()
        );
        assert!(MiniMaxFormatter
            .validate_format(
                "<tool_call><tool_name>get_weather</tool_name><parameters>bad-json</parameters></tool_call>",
            )
            .is_err());
    }

    #[test]
    fn validate_format_allows_calls_skipped_for_non_object_arguments() {
        // Go skips these at parse time, so validation sees nothing to reject.
        assert!(
            MiniMaxFormatter
                .validate_format(r#"[{"id":"1","name":"a","arguments":null}]"#)
                .is_ok()
        );
    }

    #[test]
    fn selector_does_not_route_minimax() {
        // go/toolformat/selector.go has no minimax arm; minimax models fall
        // through to the generic formatter there, and for_model mirrors it.
        assert_eq!(for_model("minimax-m1").model_family(), "generic");
    }
}
