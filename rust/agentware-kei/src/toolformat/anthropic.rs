//! Anthropic Claude XML tool-use formatter (family `"anthropic"`).
//!
//! Text-mode tool use: `<function_calls><invoke tool="name">{json}</invoke>
//! </function_calls>`. Mirrors Go's `AnthropicFormatter`.

use std::sync::LazyLock;

use regex::Regex;
use serde_json::Value;

use super::generic;
use super::{Formatter, ParsedToolCall, ToolDef, ToolResult};

static CALLS_OUTER: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r"<function_calls>\s*(.*?)\s*</function_calls>").expect("valid regex")
});
static INVOKE: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r#"<invoke tool="([^"]+)">\s*(.*?)\s*</invoke>"#).expect("valid regex")
});
static ANY_INVOKE: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"<invoke\b[^>]*>").expect("valid regex"));

/// Renders tool definitions as the OpenAI-compatible JSON array (Claude in
/// text mode receives the same definitions block as the generic engine).
fn format_tool_definitions(tools: &[ToolDef]) -> String {
    generic::format_function_definitions(tools)
}

/// Parses `<function_calls>` blocks. Malformed or missing arguments are an
/// error — Go kept them as a `_raw` fallback; this port fails closed instead.
fn parse_tool_calls(response: &str) -> Result<Vec<ParsedToolCall>, String> {
    if response.is_empty() {
        return Ok(Vec::new());
    }
    let Some(outer) = CALLS_OUTER.captures(response) else {
        return Ok(Vec::new());
    };
    let inner = outer.get(1).map(|m| m.as_str()).unwrap_or_default();
    let mut result = Vec::new();
    for m in INVOKE.captures_iter(inner) {
        let name = m.get(1).map(|c| c.as_str()).unwrap_or_default().to_owned();
        let args_raw = m.get(2).map(|c| c.as_str()).unwrap_or_default();
        let args = super::decode_tag_arguments(&name, args_raw)?;
        result.push(ParsedToolCall {
            id: String::new(),
            name,
            args,
            raw: m.get(0).map(|c| c.as_str()).unwrap_or_default().to_owned(),
        });
    }
    Ok(result)
}

fn format_tool_result(name: &str, result: &ToolResult) -> String {
    if result.success {
        format!(
            r#"<function_results><result tool="{name}">{}</result></function_results>"#,
            result.output
        )
    } else {
        format!(
            r#"<function_results><result tool="{name}">Error: {}</result></function_results>"#,
            result.error
        )
    }
}

fn validate_format(response: &str) -> Result<(), String> {
    if response.is_empty() {
        return Ok(());
    }
    let Some(outer) = CALLS_OUTER.captures(response) else {
        return Ok(());
    };
    let inner = outer.get(1).map(|m| m.as_str()).unwrap_or_default();
    if ANY_INVOKE.is_match(inner) && !INVOKE.is_match(inner) {
        return Err("invalid invoke tag: missing 'tool' attribute".to_owned());
    }
    for m in INVOKE.captures_iter(inner) {
        let name = m.get(1).map(|c| c.as_str()).unwrap_or_default();
        let args_raw = m.get(2).map(|c| c.as_str()).unwrap_or_default();
        if name.is_empty() {
            return Err("tool call missing function name".to_owned());
        }
        if args_raw.is_empty() {
            return Err(format!("tool call {name:?} missing arguments"));
        }
        if serde_json::from_str::<Value>(args_raw).is_err() {
            return Err(format!("tool call {name:?} has invalid JSON arguments"));
        }
    }
    Ok(())
}

/// Anthropic Claude models.
pub struct AnthropicFormatter;

impl Formatter for AnthropicFormatter {
    fn format_tools(&self, tools: &[ToolDef]) -> String {
        format_tool_definitions(tools)
    }

    fn parse_tool_calls(&self, response: &str) -> Result<Vec<ParsedToolCall>, String> {
        parse_tool_calls(response)
    }

    fn format_tool_result(&self, name: &str, result: &ToolResult) -> String {
        format_tool_result(name, result)
    }

    fn model_family(&self) -> &'static str {
        "anthropic"
    }

    fn validate_format(&self, response: &str) -> Result<(), String> {
        validate_format(response)
    }
}
