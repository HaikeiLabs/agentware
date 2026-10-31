//! Nemotron tool-call format: an OpenAI-style JSON array of
//! `{id, name, arguments}` calls with a compact function-definitions
//! listing. Duplicates the generic JSON engine (as in Go) rather than
//! sharing it.

use serde_json::{Map, Value};

use super::{Formatter, ParsedToolCall, ToolDef, ToolResult};

/// Parses the response as a JSON array of call objects, mirroring Go's
/// typed unmarshal: every element must be an object whose `id`/`name` are
/// strings (or absent/null), else the whole parse fails with
/// `{error_prefix}: …`. Go validates the shape of the entire array before
/// any per-call processing, so a type error anywhere beats per-call checks.
fn decode_call_array(response: &str, error_prefix: &str) -> Result<Vec<Value>, String> {
    let entries = serde_json::from_str::<Vec<Value>>(response)
        .map_err(|error| format!("{error_prefix}: {error}"))?;
    for entry in &entries {
        let Some(object) = entry.as_object() else {
            return Err(format!("{error_prefix}: every element must be an object"));
        };
        for field in ["id", "name"] {
            if let Some(value) = object.get(field) {
                if !value.is_string() && !value.is_null() {
                    return Err(format!("{error_prefix}: {field} must be a string"));
                }
            }
        }
    }
    Ok(entries)
}

/// Decodes a tool call's `arguments` value, mirroring Go's
/// `decodeToolCallArgs`: a JSON object passes through; a JSON string
/// containing an object is decoded; JSON null is treated as the empty
/// string (which then fails to decode); anything else is an error so a
/// malformed call is never silently dropped.
fn decode_tool_call_args(raw: &Value) -> Result<Map<String, Value>, String> {
    if let Value::Object(map) = raw {
        return Ok(map.clone());
    }
    let encoded = match raw {
        Value::String(encoded) => encoded.clone(),
        Value::Null => String::new(),
        _ => return Err("arguments must be an object or a string containing one".to_owned()),
    };
    match serde_json::from_str::<Value>(&encoded) {
        Ok(Value::Object(map)) => Ok(map),
        _ => Err("arguments string is not a JSON object".to_owned()),
    }
}

/// Formats tools and parses tool calls for the Nemotron family
/// (Nemotron-70B, ...).
pub struct NemotronFormatter;

impl Formatter for NemotronFormatter {
    fn format_tools(&self, tools: &[ToolDef]) -> String {
        if tools.is_empty() {
            return String::new();
        }

        // Go marshals map[string]any with sorted keys: "function" before
        // "type", and "description"/"name"/"parameters" inside. Insert in
        // that order so the compact JSON matches byte-for-byte; the tool's
        // own schema passes through untouched, like every other family.
        let schemas: Vec<Value> = tools
            .iter()
            .map(|tool| {
                let mut function = Map::new();
                function.insert(
                    "description".to_owned(),
                    Value::String(tool.description.clone()),
                );
                function.insert("name".to_owned(), Value::String(tool.name.clone()));
                if let Some(schema) = &tool.input_schema {
                    function.insert("parameters".to_owned(), Value::Object(schema.clone()));
                }
                let mut schema = Map::new();
                schema.insert("function".to_owned(), Value::Object(function));
                schema.insert("type".to_owned(), Value::String("function".to_owned()));
                Value::Object(schema)
            })
            .collect();
        Value::Array(schemas).to_string()
    }

    fn parse_tool_calls(&self, response: &str) -> Result<Vec<ParsedToolCall>, String> {
        if response.is_empty() {
            return Ok(Vec::new());
        }

        let entries = decode_call_array(response, "failed to parse tool calls")?;

        let mut calls = Vec::with_capacity(entries.len());
        for entry in &entries {
            let object = entry.as_object().expect("shape validated above");
            let id = match object.get("id") {
                None | Some(Value::Null) => String::new(),
                Some(Value::String(id)) => id.clone(),
                Some(_) => unreachable!("shape validated above"),
            };
            let name = match object.get("name") {
                None | Some(Value::Null) => String::new(),
                Some(Value::String(name)) => name.clone(),
                Some(_) => unreachable!("shape validated above"),
            };
            if name.is_empty() {
                return Err("tool call missing 'name' field".to_owned());
            }
            // Go distinguishes an ABSENT arguments field (nil RawMessage,
            // "must be an object or a string containing one") from an
            // explicit null (decoded as the empty string, "not a JSON
            // object"); both are errors.
            let Some(raw_args) = object.get("arguments") else {
                return Err(format!(
                    "tool call {name:?}: arguments must be an object or a string containing one"
                ));
            };
            let args = decode_tool_call_args(raw_args)
                .map_err(|error| format!("tool call {name:?}: {error}"))?;
            calls.push(ParsedToolCall {
                id,
                name,
                args,
                raw: response.to_owned(),
            });
        }
        Ok(calls)
    }

    fn format_tool_result(&self, _name: &str, result: &ToolResult) -> String {
        if result.success {
            return result.output.clone();
        }
        format!("Error: {}", result.error)
    }

    fn model_family(&self) -> &'static str {
        "nemotron"
    }

    fn validate_format(&self, response: &str) -> Result<(), String> {
        if response.is_empty() {
            return Ok(());
        }

        let entries = decode_call_array(response, "invalid JSON format")?;

        for entry in &entries {
            let object = entry.as_object().expect("shape validated above");
            let name = match object.get("name") {
                None | Some(Value::Null) => String::new(),
                Some(Value::String(name)) => name.clone(),
                Some(_) => unreachable!("shape validated above"),
            };
            if name.is_empty() {
                return Err("tool call missing 'name' field".to_owned());
            }
            // Go checks len(c.Args) == 0: only an ABSENT arguments field
            // trips this; an explicit null is non-empty raw bytes there,
            // so it passes validation even though parsing would reject it.
            if object.get("arguments").is_none() {
                return Err(format!("tool call {name:?} missing 'arguments'"));
            }
        }
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::toolformat::for_model;

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
        assert_eq!(NemotronFormatter.format_tools(&[]), "");
    }

    #[test]
    fn format_definitions_single_function_object() {
        let defs = NemotronFormatter.format_tools(&[ToolDef {
            name: "get_weather".to_owned(),
            description: "Get current weather".to_owned(),
            input_schema: None,
        }]);
        assert_eq!(
            defs,
            r#"[{"function":{"description":"Get current weather","name":"get_weather"},"type":"function"}]"#
        );
    }

    #[test]
    fn format_definitions_with_parameters() {
        let defs = NemotronFormatter.format_tools(&[weather_tool()]);
        assert_eq!(
            defs,
            r#"[{"function":{"description":"Get current weather","name":"get_weather","parameters":{"type":"object","properties":{"location":{"type":"string"}}}},"type":"function"}]"#
        );
    }

    #[test]
    fn parse_empty_response_has_no_calls() {
        assert_eq!(NemotronFormatter.parse_tool_calls("").unwrap(), Vec::new());
    }

    #[test]
    fn parse_fuzz_seed_call() {
        // Ported from go/toolformat/fuzz_test.go (nemotron-70b seed); the
        // raw span is the whole response.
        let response = r#"[{"id":"1","name":"get_weather","arguments":{"location":"Tokyo"}}]"#;
        let calls = NemotronFormatter.parse_tool_calls(response).unwrap();
        assert_eq!(calls.len(), 1);
        assert_eq!(calls[0].id, "1");
        assert_eq!(calls[0].name, "get_weather");
        assert_eq!(calls[0].args["location"], "Tokyo");
        assert_eq!(calls[0].raw, response);
    }

    #[test]
    fn parse_decodes_string_arguments() {
        // Ported from go TestParseToolCallsDecodesStringArguments, which
        // includes NemotronFormatter among the JSON-call formatters.
        let response = r#"[{"id":"chatcmpl-tool-b740f94d8f903c3d","name":"get_weather","arguments":"{\"location\": \"Tokyo\"}"}]"#;
        let calls = NemotronFormatter.parse_tool_calls(response).unwrap();
        assert_eq!(calls.len(), 1);
        assert_eq!(calls[0].id, "chatcmpl-tool-b740f94d8f903c3d");
        assert_eq!(calls[0].args["location"], "Tokyo");
    }

    #[test]
    fn parse_rejects_empty_name() {
        // Ported from go TestParseToolCallsFuzzRegressions ("nemotron
        // rejects empty name"; #278). "Arguments" is not the "arguments"
        // field, so the name is missing entirely.
        assert!(
            NemotronFormatter
                .parse_tool_calls(r#"[{"Arguments":{}}]"#)
                .is_err()
        );
    }

    #[test]
    fn parse_rejects_null_and_missing_arguments() {
        // #278: null arguments and an absent arguments field both fail.
        assert!(
            NemotronFormatter
                .parse_tool_calls(r#"[{"name":"my_tool","arguments":null}]"#)
                .is_err()
        );
        assert!(
            NemotronFormatter
                .parse_tool_calls(r#"[{"name":"my_tool"}]"#)
                .is_err()
        );
    }

    #[test]
    fn parse_rejects_non_arrays_and_bad_shapes() {
        assert!(NemotronFormatter.parse_tool_calls("{\"a\":1}").is_err());
        assert!(NemotronFormatter.parse_tool_calls("[1]").is_err());
        assert!(
            NemotronFormatter
                .parse_tool_calls(r#"[{"id":5,"name":"a","arguments":{}}]"#)
                .is_err()
        );
        assert!(
            NemotronFormatter
                .parse_tool_calls(r#"[{"name":"a","arguments":{}},{"id":5}]"#)
                .is_err()
        );
    }

    #[test]
    fn format_result_success_is_bare_output() {
        let result = ToolResult {
            success: true,
            output: "Sunny, 22C".to_owned(),
            error: String::new(),
        };
        assert_eq!(
            NemotronFormatter.format_tool_result("get_weather", &result),
            "Sunny, 22C"
        );
    }

    #[test]
    fn format_result_error_is_error_prefixed() {
        let result = ToolResult {
            success: false,
            output: String::new(),
            error: "API unavailable".to_owned(),
        };
        assert_eq!(
            NemotronFormatter.format_tool_result("get_weather", &result),
            "Error: API unavailable"
        );
    }

    #[test]
    fn model_family_is_nemotron() {
        assert_eq!(NemotronFormatter.model_family(), "nemotron");
    }

    #[test]
    fn validate_format_accepts_valid_and_empty() {
        assert!(
            NemotronFormatter
                .validate_format(
                    r#"[{"id":"1","name":"get_weather","arguments":{"location":"Tokyo"}}]"#
                )
                .is_ok()
        );
        assert!(NemotronFormatter.validate_format("").is_ok());
    }

    #[test]
    fn validate_format_rejects_missing_name_or_arguments() {
        assert!(
            NemotronFormatter
                .validate_format(r#"[{"Arguments":{}}]"#)
                .is_err()
        );
        assert!(
            NemotronFormatter
                .validate_format(r#"[{"name":"my_tool"}]"#)
                .is_err()
        );
        assert!(NemotronFormatter.validate_format("{\"a\":1}").is_err());
    }

    #[test]
    fn validate_format_allows_explicit_null_arguments() {
        // Go's validate only trips on an ABSENT arguments field; explicit
        // null is non-empty raw bytes, so it passes validation even though
        // parsing rejects it. Mirror that asymmetry.
        assert!(
            NemotronFormatter
                .validate_format(r#"[{"name":"my_tool","arguments":null}]"#)
                .is_ok()
        );
    }

    #[test]
    fn selector_maps_nemotron_models() {
        // go/toolformat/selector.go checks nemotron first (before qwen).
        assert_eq!(for_model("nemotron-70b").model_family(), "nemotron");
    }
}
