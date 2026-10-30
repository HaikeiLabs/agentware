//! OpenAI-compatible JSON function-calling engine.
//!
//! Shared by the [`GenericFormatter`] and the families that inherit it
//! (openai, deepseek, glm, nemotron), mirroring Go's `GenericFormatter`.

use serde_json::{Map, Value};

use super::{Formatter, ParsedToolCall, ToolDef, ToolResult};

/// Renders tool definitions as a JSON array of
/// `{"type":"function","function":{name,description,parameters?}}` objects.
pub(super) fn format_function_definitions(tools: &[ToolDef]) -> String {
    if tools.is_empty() {
        return String::new();
    }
    let mut schemas = Vec::with_capacity(tools.len());
    for tool in tools {
        let mut function = Map::new();
        function.insert("name".to_owned(), Value::String(tool.name.clone()));
        function.insert(
            "description".to_owned(),
            Value::String(tool.description.clone()),
        );
        if let Some(schema) = &tool.input_schema {
            function.insert("parameters".to_owned(), Value::Object(schema.clone()));
        }
        let mut schema = Map::new();
        schema.insert("type".to_owned(), Value::String("function".to_owned()));
        schema.insert("function".to_owned(), Value::Object(function));
        schemas.push(Value::Object(schema));
    }
    serde_json::to_string(&Value::Array(schemas)).expect("Value serialization cannot fail")
}

/// Renders a tool result plainly: the output on success, `Error: <error>`
/// otherwise (mirrors Go's generic `FormatToolResult`).
pub(super) fn format_plain_result(result: &ToolResult) -> String {
    if result.success {
        result.output.clone()
    } else {
        format!("Error: {}", result.error)
    }
}

/// Decodes tool-call arguments that may be a JSON object or a JSON string
/// containing an object (GLM-5.3 and some OpenAI-compatible backends emit the
/// string form). Anything else is malformed and returns an error — a call is
/// never silently dropped.
pub(super) fn decode_arguments(arguments: Option<&Value>) -> Result<Map<String, Value>, String> {
    match arguments {
        Some(Value::Object(map)) => Ok(map.clone()),
        Some(Value::String(text)) => match serde_json::from_str::<Value>(text) {
            Ok(Value::Object(map)) => Ok(map),
            Ok(_) => Err(format!(
                "tool call arguments must be a JSON object, got: {text}"
            )),
            Err(error) => Err(format!("malformed tool call arguments: {error}")),
        },
        Some(Value::Null) | None => Err("tool call is missing 'arguments'".to_owned()),
        Some(other) => Err(format!(
            "tool call arguments must be a JSON object or string, got: {other}"
        )),
    }
}

fn expect_object<'a>(value: &'a Value, what: &str) -> Result<&'a Map<String, Value>, String> {
    value
        .as_object()
        .ok_or_else(|| format!("failed to parse tool calls: {what} must be a JSON object"))
}

fn call_name(obj: &Map<String, Value>) -> Result<String, String> {
    match obj.get("name") {
        None | Some(Value::Null) => Ok(String::new()),
        Some(Value::String(name)) => Ok(name.clone()),
        Some(other) => Err(format!(
            "failed to parse tool calls: tool call 'name' must be a string, got {other}"
        )),
    }
}

fn call_id(obj: &Map<String, Value>) -> Result<String, String> {
    match obj.get("id") {
        None | Some(Value::Null) => Ok(String::new()),
        Some(Value::String(id)) => Ok(id.clone()),
        Some(other) => Err(format!(
            "failed to parse tool calls: tool call 'id' must be a string, got {other}"
        )),
    }
}

/// Parses a JSON array of `{id?, name, arguments}` tool calls. The arguments
/// may be a JSON object or a JSON string containing an object; malformed
/// arguments are an error. A top-level JSON `null` yields no calls (Go's
/// unmarshal-into-slice semantics).
pub(super) fn parse_json_tool_calls(response: &str) -> Result<Vec<ParsedToolCall>, String> {
    if response.is_empty() {
        return Ok(Vec::new());
    }
    let value: Value = serde_json::from_str(response)
        .map_err(|error| format!("failed to parse tool calls: {error}"))?;
    let items = match value {
        Value::Null => return Ok(Vec::new()),
        Value::Array(items) => items,
        _ => {
            return Err(
                "failed to parse tool calls: expected a JSON array of tool calls".to_owned(),
            );
        }
    };
    let mut result = Vec::with_capacity(items.len());
    for item in &items {
        let obj = expect_object(item, "tool call")?;
        let name = call_name(obj)?;
        let id = call_id(obj)?;
        let args = decode_arguments(obj.get("arguments"))?;
        result.push(ParsedToolCall {
            id,
            name,
            args,
            raw: response.to_owned(),
        });
    }
    Ok(result)
}

/// Validates a JSON tool-call response: well-formed array, every call has a
/// name and decodable arguments (object or JSON-string object).
pub(super) fn validate_json_tool_calls(response: &str) -> Result<(), String> {
    if response.is_empty() {
        return Ok(());
    }
    let value: Value =
        serde_json::from_str(response).map_err(|error| format!("invalid JSON format: {error}"))?;
    let items = match value {
        Value::Null => return Ok(()),
        Value::Array(items) => items,
        _ => return Err("invalid JSON format: expected a JSON array of tool calls".to_owned()),
    };
    for item in &items {
        let obj = expect_object(item, "tool call")
            .map_err(|error| format!("invalid JSON format: {error}"))?;
        let name = match obj.get("name") {
            Some(Value::String(name)) => name.clone(),
            _ => return Err("tool call missing 'name' field".to_owned()),
        };
        let arguments = match obj.get("arguments") {
            None | Some(Value::Null) => {
                return Err(format!("tool call {name:?} missing 'arguments'"));
            }
            Some(arguments) => arguments,
        };
        if decode_arguments(Some(arguments)).is_err() {
            return Err(format!("tool call {name:?} has invalid 'arguments'"));
        }
    }
    Ok(())
}

/// Family-neutral fallback formatter: OpenAI-compatible JSON function calling.
pub struct GenericFormatter;

impl Formatter for GenericFormatter {
    fn format_tools(&self, tools: &[ToolDef]) -> String {
        format_function_definitions(tools)
    }

    fn parse_tool_calls(&self, response: &str) -> Result<Vec<ParsedToolCall>, String> {
        parse_json_tool_calls(response)
    }

    fn format_tool_result(&self, _name: &str, result: &ToolResult) -> String {
        format_plain_result(result)
    }

    fn model_family(&self) -> &'static str {
        "generic"
    }

    fn validate_format(&self, response: &str) -> Result<(), String> {
        validate_json_tool_calls(response)
    }
}
