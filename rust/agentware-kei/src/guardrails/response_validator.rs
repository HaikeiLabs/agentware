//! Validation of model responses against the configured tool names (port of
//! `go/middleware/guardrails/response_validator.go`).
//!
//! The validator decides whether a model response is a usable tool call. When
//! rescue is enabled it attempts to recover a tool call embedded in free text
//! (think tags, python tags, code fences, rehearsal markup); otherwise it
//! returns a retry nudge. It never authorizes or denies execution.

use serde_json::{Map, Value};

use super::nudge::{Nudge, retry_nudge, unknown_tool_nudge};

/// One tool call the model produced (Go `ToolCall`).
#[derive(Debug, Clone, PartialEq)]
pub struct ToolCall {
    pub tool: String,
    pub args: Map<String, Value>,
}

/// The result of validating a model response (Go `ValidationResult`).
#[derive(Debug, Clone, PartialEq)]
pub struct ValidationResult {
    pub tool_calls: Vec<ToolCall>,
    pub nudge: Option<Nudge>,
    pub needs_retry: bool,
}

/// Validates model responses against a closed set of tool names (Go
/// `ResponseValidator`).
#[derive(Debug, Clone)]
pub struct ResponseValidator {
    pub tool_names: Vec<String>,
    pub rescue_enabled: bool,
    /// Builds the retry nudge for a non-tool-call response; defaults to
    /// [`retry_nudge`] (Go `RetryNudgeFn`).
    pub retry_nudge_fn: fn(&str, &[String]) -> Nudge,
}

impl ResponseValidator {
    /// Creates a validator (Go `NewResponseValidator`).
    pub fn new(tool_names: Vec<String>, rescue_enabled: bool) -> Self {
        Self {
            tool_names,
            rescue_enabled,
            retry_nudge_fn: retry_nudge,
        }
    }

    /// Validates a free-text response (Go `ValidateTextResponse`). With
    /// rescue enabled, a tool call embedded in the text is recovered;
    /// otherwise (or when nothing is recoverable) the response needs a retry.
    pub fn validate_text_response(&self, response: &str) -> ValidationResult {
        if self.rescue_enabled {
            let rescued = self.rescue_tool_call(response);
            if !rescued.is_empty() {
                return ValidationResult {
                    tool_calls: rescued,
                    nudge: None,
                    needs_retry: false,
                };
            }
        }
        ValidationResult {
            tool_calls: Vec::new(),
            nudge: Some((self.retry_nudge_fn)(response, &self.tool_names)),
            needs_retry: true,
        }
    }

    /// Validates structured tool calls (Go `ValidateToolCalls`). Any unknown
    /// tool name fails the whole batch with an unknown-tool nudge.
    pub fn validate_tool_calls(&self, tool_calls: &[ToolCall]) -> ValidationResult {
        let mut unknown: Vec<String> = Vec::new();
        let mut valid_calls: Vec<ToolCall> = Vec::new();

        for tc in tool_calls {
            if self.is_valid_tool(&tc.tool) {
                valid_calls.push(tc.clone());
            } else {
                unknown.push(tc.tool.clone());
            }
        }

        if !unknown.is_empty() {
            return ValidationResult {
                tool_calls: Vec::new(),
                nudge: Some(unknown_tool_nudge(&unknown[0], &self.tool_names)),
                needs_retry: true,
            };
        }

        ValidationResult {
            tool_calls: valid_calls,
            nudge: None,
            needs_retry: false,
        }
    }

    fn is_valid_tool(&self, name: &str) -> bool {
        self.tool_names.iter().any(|tn| tn == name)
    }

    /// Attempts to recover tool calls from a free-text response (Go
    /// `RescueToolCall`): strips think and python tags, then tries JSON,
    /// rehearsal, and Qwen-XML extraction in order.
    pub fn rescue_tool_call(&self, response: &str) -> Vec<ToolCall> {
        let cleaned = self.strip_python_tag(&self.strip_think_tags(response));
        let cleaned = cleaned.trim();

        if cleaned.is_empty() {
            return Vec::new();
        }

        let calls = self.extract_json_tool_calls(cleaned);
        if !calls.is_empty() {
            return calls;
        }

        let calls = self.extract_rehearsal_tool_calls(cleaned);
        if !calls.is_empty() {
            return calls;
        }

        self.extract_qwen_xml_tool_calls(cleaned)
    }

    fn strip_think_tags(&self, text: &str) -> String {
        static THINK: std::sync::LazyLock<regex::Regex> = std::sync::LazyLock::new(|| {
            regex::Regex::new(
                r"(?i)\[THINK\].*?\[/THINK\]|<think>.*?
</think>",
            )
            .expect("static regex")
        });
        THINK.replace_all(text, "").into_owned()
    }

    fn strip_python_tag(&self, text: &str) -> String {
        static PYTHON_TAG: std::sync::LazyLock<regex::Regex> = std::sync::LazyLock::new(|| {
            regex::Regex::new(r"(?i)<\|python_tag\|>").expect("static regex")
        });
        PYTHON_TAG.replace_all(text, "").into_owned()
    }

    fn extract_json_tool_calls(&self, text: &str) -> Vec<ToolCall> {
        static CODE_FENCE: std::sync::LazyLock<regex::Regex> = std::sync::LazyLock::new(|| {
            regex::Regex::new(r"```(?:json)?\s*\n?").expect("static regex")
        });
        let cleaned = CODE_FENCE.replace_all(text, "");
        let cleaned = cleaned.trim_matches([' ', '\n', '\t']);

        let mut found: Vec<ToolCall> = Vec::new();
        let chars: Vec<char> = cleaned.chars().collect();
        let mut i = 0;
        while i < chars.len() {
            if chars[i] == '{' {
                let mut depth = 0i32;
                let mut j = i;
                while j < chars.len() {
                    if chars[j] == '{' {
                        depth += 1;
                    } else if chars[j] == '}' {
                        depth -= 1;
                        if depth == 0 {
                            let candidate: String = chars[i..=j].iter().collect();
                            if let Some(call) = self.try_parse_tool_call(&candidate) {
                                found.push(call);
                            }
                            i = j + 1;
                            break;
                        }
                    }
                    j += 1;
                }
                if depth != 0 {
                    i += 1;
                }
            } else {
                i += 1;
            }
        }
        found
    }

    fn try_parse_tool_call(&self, json_str: &str) -> Option<ToolCall> {
        let data: Map<String, Value> = serde_json::from_str(json_str).ok()?;

        let tool_name = data
            .get("tool")
            .and_then(Value::as_str)
            .or_else(|| data.get("name").and_then(Value::as_str))?
            .to_owned();

        if !self.is_valid_tool(&tool_name) {
            return None;
        }

        let args = data
            .get("args")
            .and_then(Value::as_object)
            .or_else(|| data.get("arguments").and_then(Value::as_object))
            .cloned()
            .unwrap_or_default();

        Some(ToolCall {
            tool: tool_name,
            args,
        })
    }

    fn extract_rehearsal_tool_calls(&self, text: &str) -> Vec<ToolCall> {
        static REHEARSAL: std::sync::LazyLock<regex::Regex> = std::sync::LazyLock::new(|| {
            regex::Regex::new(r"(\w+)\[ARGS\](\{.*\})").expect("static regex")
        });
        let mut found: Vec<ToolCall> = Vec::new();

        for captures in REHEARSAL.captures_iter(text) {
            let tool_name = captures.get(1).expect("group 1").as_str().to_owned();
            let args_str = captures.get(2).expect("group 2").as_str();

            if !self.is_valid_tool(&tool_name) {
                continue;
            }

            // The rehearsal shape guarantees an object; a parse failure skips
            // the match. Go appends even an empty `{}` (its `args != nil`
            // check only excludes JSON null, which the shape cannot produce).
            let Ok(args) = serde_json::from_str::<Map<String, Value>>(args_str) else {
                continue;
            };
            found.push(ToolCall {
                tool: tool_name,
                args,
            });
        }

        found
    }

    fn extract_qwen_xml_tool_calls(&self, _text: &str) -> Vec<ToolCall> {
        // Ported as a stub, matching the Go implementation, which returns an
        // empty slice.
        Vec::new()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::guardrails::nudge::NudgeKind;

    fn validator(tools: &[&str], rescue: bool) -> ResponseValidator {
        ResponseValidator::new(tools.iter().map(|t| t.to_string()).collect(), rescue)
    }

    /// Mirrors go TestNewResponseValidator.
    #[test]
    fn new_response_validator() {
        let rv = validator(&["tool1", "tool2"], true);
        assert_eq!(rv.tool_names.len(), 2);
        assert!(rv.rescue_enabled);
    }

    /// Mirrors go TestValidateToolCalls_Valid.
    #[test]
    fn validate_tool_calls_valid() {
        let rv = validator(&["tool1", "tool2"], false);
        let calls = vec![ToolCall {
            tool: "tool1".to_owned(),
            args: Map::from_iter([("key".to_owned(), Value::String("value".to_owned()))]),
        }];

        let result = rv.validate_tool_calls(&calls);
        assert!(!result.needs_retry);
        assert!(result.nudge.is_none());
        assert_eq!(result.tool_calls.len(), 1);
    }

    /// Mirrors go TestValidateToolCalls_Invalid.
    #[test]
    fn validate_tool_calls_invalid() {
        let rv = validator(&["tool1", "tool2"], false);
        let calls = vec![ToolCall {
            tool: "unknown_tool".to_owned(),
            args: Map::new(),
        }];

        let result = rv.validate_tool_calls(&calls);
        assert!(result.needs_retry);
        assert_eq!(
            result.nudge.as_ref().map(|n| n.kind),
            Some(NudgeKind::UnknownTool)
        );
    }

    /// Mirrors go TestValidateTextResponse_WithRescue.
    #[test]
    fn validate_text_response_with_rescue() {
        let rv = validator(&["tool1", "tool2"], true);
        let response = r#"{"tool": "tool1", "args": {"key": "value"}}"#;

        let result = rv.validate_text_response(response);
        assert!(!result.needs_retry);
        assert_eq!(result.tool_calls.len(), 1);
    }

    /// Mirrors go TestValidateTextResponse_WithoutRescue.
    #[test]
    fn validate_text_response_without_rescue() {
        let rv = validator(&["tool1", "tool2"], false);
        let result = rv.validate_text_response("This is just text, not a tool call.");
        assert!(result.needs_retry);
        assert!(result.nudge.is_some());
    }

    /// Mirrors go TestValidateTextResponse_Empty.
    #[test]
    fn validate_text_response_empty() {
        let rv = validator(&["tool1", "tool2"], true);
        let result = rv.validate_text_response("");
        assert!(result.needs_retry);
    }

    /// Mirrors go TestRescueToolCall_StripsThinkTags.
    #[test]
    fn rescue_tool_call_strips_think_tags() {
        let rv = validator(&["tool1", "tool2"], true);
        let response = "[THINK] thinking [/THINK]\n{\"tool\": \"tool1\", \"args\": {}}";

        let calls = rv.rescue_tool_call(response);
        assert_eq!(calls.len(), 1);
    }

    /// Mirrors go TestRescueToolCall_StripsPythonTag.
    #[test]
    fn rescue_tool_call_strips_python_tag() {
        let rv = validator(&["tool1", "tool2"], true);
        let response = "<|python_tag|>{\"tool\": \"tool1\", \"args\": {}}";

        let calls = rv.rescue_tool_call(response);
        assert_eq!(calls.len(), 1);
    }

    /// Mirrors go TestExtractJSONToolCalls_WithCodeFence.
    #[test]
    fn extract_json_tool_calls_with_code_fence() {
        let rv = validator(&["tool1", "tool2"], true);
        let response = "```json\n{\"tool\": \"tool1\", \"args\": {\"key\": \"value\"}}\n```";

        let calls = rv.extract_json_tool_calls(response);
        assert_eq!(calls.len(), 1);
    }

    /// Mirrors go TestExtractRehearsalToolCalls.
    #[test]
    fn extract_rehearsal_tool_calls() {
        let rv = validator(&["tool1", "tool2"], true);
        let response = r#"tool1[ARGS]{"key": "value"}"#;

        let calls = rv.extract_rehearsal_tool_calls(response);
        assert_eq!(calls.len(), 1);
    }

    /// Mirrors go TestIsValidTool.
    #[test]
    fn is_valid_tool() {
        let rv = validator(&["tool1", "tool2"], false);
        assert!(rv.is_valid_tool("tool1"));
        assert!(!rv.is_valid_tool("tool3"));
    }

    /// Mirrors go TestTryParseToolCall_Valid.
    #[test]
    fn try_parse_tool_call_valid() {
        let rv = validator(&["tool1", "tool2"], true);
        let call = rv
            .try_parse_tool_call(r#"{"tool": "tool1", "args": {"key": "value"}}"#)
            .expect("expected non-nil call");
        assert_eq!(call.tool, "tool1");
    }

    /// Mirrors go TestTryParseToolCall_InvalidJSON.
    #[test]
    fn try_parse_tool_call_invalid_json() {
        let rv = validator(&["tool1", "tool2"], true);
        assert!(rv.try_parse_tool_call("not valid json").is_none());
    }

    /// Mirrors go TestTryParseToolCall_UnknownTool.
    #[test]
    fn try_parse_tool_call_unknown_tool() {
        let rv = validator(&["tool1"], true);
        assert!(
            rv.try_parse_tool_call(r#"{"tool": "unknown", "args": {}}"#)
                .is_none()
        );
    }

    /// Mirrors go TestExtractQwenXMLToolCalls.
    #[test]
    fn extract_qwen_xml_tool_calls() {
        let rv = validator(&["tool1", "tool2"], true);
        let calls = rv.extract_qwen_xml_tool_calls("<function=tool1>content</function>");
        assert_eq!(calls.len(), 0);
    }
}
