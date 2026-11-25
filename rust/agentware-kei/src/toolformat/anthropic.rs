//! Anthropic Claude tool use rendered as text (family `"anthropic"`).
//!
//! Definitions are Anthropic tool definitions: `{"name", "description",
//! "input_schema"}`. Calls are Anthropic `tool_use` blocks, `{"type":
//! "tool_use", "id", "name", "input"}`, given as one block or as a content
//! array (its other blocks, such as text, are ignored); each call keeps the
//! block's id. When the response holds no `tool_use` block, the legacy
//! `<function_calls><invoke tool="name">{json}</invoke></function_calls>` form
//! is parsed instead, and those calls have no id. Results are `tool_result`
//! blocks, `{"type": "tool_result", "tool_use_id", "content", "is_error"}`.
//! Without a call id, `tool_name` takes the place of `tool_use_id`; that block
//! is a text rendering only, not a valid Messages API block.
//!
//! See <https://platform.claude.com/docs/en/agents-and-tools/tool-use/overview>.
//! Mirrors Go's `AnthropicFormatter`.

use std::collections::HashSet;
use std::sync::LazyLock;

use regex::Regex;
use serde_json::{Map, Value, json};

use super::{Formatter, ParsedToolCall, ToolDef, ToolResult};

static CALLS_OUTER: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r"<function_calls>\s*(.*?)\s*</function_calls>").expect("valid regex")
});
static INVOKE: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r#"<invoke tool="([^"]+)">\s*(.*?)\s*</invoke>"#).expect("valid regex")
});
static ANY_INVOKE: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"<invoke\b[^>]*>").expect("valid regex"));

/// Renders tool definitions as a JSON array of Anthropic tool definitions.
fn format_tool_definitions(tools: &[ToolDef]) -> String {
    if tools.is_empty() {
        return String::new();
    }
    let definitions: Vec<Value> = tools
        .iter()
        .map(|tool| {
            let mut definition = Map::new();
            definition.insert("name".to_owned(), Value::String(tool.name.clone()));
            definition.insert(
                "description".to_owned(),
                Value::String(tool.description.clone()),
            );
            // The Messages API requires input_schema; a tool without
            // parameters takes an empty object schema.
            let schema = tool.input_schema.clone().map_or_else(
                || json!({"type": "object", "properties": {}}),
                Value::Object,
            );
            definition.insert("input_schema".to_owned(), schema);
            Value::Object(definition)
        })
        .collect();
    serde_json::to_string(&definitions).expect("Value serialization cannot fail")
}

/// The `tool_use` blocks of a response that is a JSON block or a JSON content
/// array. Any other response yields none.
fn tool_use_blocks(response: &str) -> Vec<Map<String, Value>> {
    let text = response.trim();
    if !(text.starts_with('{') || text.starts_with('[')) {
        return Vec::new();
    }
    let items = match serde_json::from_str::<Value>(text) {
        Ok(Value::Array(items)) => items,
        Ok(value) => vec![value],
        Err(_) => return Vec::new(),
    };
    items
        .into_iter()
        .filter_map(|item| match item {
            Value::Object(block)
                if block.get("type").and_then(Value::as_str) == Some("tool_use") =>
            {
                Some(block)
            }
            _ => None,
        })
        .collect()
}

/// A valid `tool_use` block as `(id, name, input)`, or why it is malformed.
fn tool_use_parts(block: &Map<String, Value>) -> Result<(&str, &str, &Map<String, Value>), String> {
    let name = block
        .get("name")
        .and_then(Value::as_str)
        .filter(|name| !name.is_empty())
        .ok_or_else(|| "tool_use block missing name".to_owned())?;
    let id = block
        .get("id")
        .and_then(Value::as_str)
        .filter(|id| !id.is_empty())
        .ok_or_else(|| format!("tool_use block {name:?} missing id"))?;
    let input = block
        .get("input")
        .and_then(Value::as_object)
        .ok_or_else(|| format!("tool_use block {name:?} input must be a JSON object"))?;
    Ok((id, name, input))
}

fn parse_tool_calls(response: &str) -> Result<Vec<ParsedToolCall>, String> {
    if response.is_empty() {
        return Ok(Vec::new());
    }
    let blocks = tool_use_blocks(response);
    if blocks.is_empty() {
        return parse_invoke(response);
    }
    // Fail closed: a malformed tool_use block is never returned as a call
    // (validate_format names the problem).
    Ok(blocks
        .iter()
        .filter_map(|block| tool_use_parts(block).ok())
        .map(|(id, name, input)| ParsedToolCall {
            id: id.to_owned(),
            name: name.to_owned(),
            args: input.clone(),
            raw: response.to_owned(),
        })
        .collect())
}

/// Parses the legacy `<function_calls>` form. Malformed or missing arguments
/// are an error — Go kept them as a `_raw` fallback; this port fails closed
/// instead. These calls carry no id.
fn parse_invoke(response: &str) -> Result<Vec<ParsedToolCall>, String> {
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

/// Renders a `tool_result` block. A non-empty `call_id` becomes
/// `tool_use_id`; without one, `tool_name` carries the tool name.
fn format_tool_result(name: &str, result: &ToolResult, call_id: Option<&str>) -> String {
    let mut block = Map::new();
    block.insert("type".to_owned(), Value::String("tool_result".to_owned()));
    match call_id.filter(|id| !id.is_empty()) {
        Some(id) => block.insert("tool_use_id".to_owned(), Value::String(id.to_owned())),
        None => block.insert("tool_name".to_owned(), Value::String(name.to_owned())),
    };
    if result.success {
        block.insert("content".to_owned(), Value::String(result.output.clone()));
    } else {
        block.insert("content".to_owned(), Value::String(result.error.clone()));
        block.insert("is_error".to_owned(), Value::Bool(true));
    }
    serde_json::to_string(&block).expect("Value serialization cannot fail")
}

fn validate_format(response: &str) -> Result<(), String> {
    if response.is_empty() {
        return Ok(());
    }
    let blocks = tool_use_blocks(response);
    if blocks.is_empty() {
        return validate_invoke(response);
    }
    let mut seen = HashSet::new();
    for block in &blocks {
        let (id, _, _) = tool_use_parts(block)?;
        if !seen.insert(id) {
            return Err(format!("duplicate tool_use id {id:?}"));
        }
    }
    Ok(())
}

/// Validates the legacy `<function_calls><invoke>` form.
fn validate_invoke(response: &str) -> Result<(), String> {
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
        format_tool_result(name, result, None)
    }

    fn format_tool_result_with_call_id(
        &self,
        name: &str,
        result: &ToolResult,
        call_id: Option<&str>,
    ) -> String {
        format_tool_result(name, result, call_id)
    }

    fn model_family(&self) -> &'static str {
        "anthropic"
    }

    fn validate_format(&self, response: &str) -> Result<(), String> {
        validate_format(response)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn tool_without_schema_takes_an_empty_object_schema() {
        let tools = [ToolDef {
            name: "ping".to_owned(),
            description: "Ping".to_owned(),
            input_schema: None,
        }];
        assert_eq!(
            AnthropicFormatter.format_tools(&tools),
            r#"[{"name":"ping","description":"Ping","input_schema":{"type":"object","properties":{}}}]"#
        );
    }

    #[test]
    fn empty_call_id_falls_back_to_tool_name() {
        let result = ToolResult {
            success: true,
            output: "ok".to_owned(),
            error: String::new(),
        };
        assert_eq!(
            AnthropicFormatter.format_tool_result_with_call_id("t", &result, Some("")),
            r#"{"type":"tool_result","tool_name":"t","content":"ok"}"#
        );
        assert_eq!(
            AnthropicFormatter.format_tool_result("t", &result),
            r#"{"type":"tool_result","tool_name":"t","content":"ok"}"#
        );
    }

    #[test]
    fn other_formatters_ignore_the_call_id() {
        let result = ToolResult {
            success: true,
            output: "ok".to_owned(),
            error: String::new(),
        };
        for name in ["qwen", "llama", "mistral", "minimax", "nemotron", "gpt-4o"] {
            let formatter = super::super::for_model(name);
            assert_eq!(
                formatter.format_tool_result_with_call_id("t", &result, Some("call_1")),
                formatter.format_tool_result("t", &result),
                "{name}"
            );
        }
    }
}
