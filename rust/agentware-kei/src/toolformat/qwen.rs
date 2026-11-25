//! Qwen Hermes tool formatting with deprecated legacy XML parsing.
use super::{Formatter, ParsedToolCall, ToolDef, ToolResult};
use regex::Regex;
use serde_json::{Map, Value, json};
use std::sync::LazyLock;

static SPAN: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"(?s)<tool_call>(.*?)</tool_call>").unwrap());
static LEGACY: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r#"(?s)^<tool name="([^"]+)">\s*(.*?)\s*</tool>$"#).unwrap());
/// Qwen Hermes tool formatter.
pub struct QwenFormatter;
fn decode(body: &str) -> Option<(String, Map<String, Value>)> {
    let value: Value = serde_json::from_str(body).ok()?;
    let name = value.get("name")?.as_str()?.to_owned();
    if name.is_empty() {
        return None;
    }
    let args = value.get("arguments")?;
    let args = if let Some(s) = args.as_str() {
        serde_json::from_str(s).ok()?
    } else {
        args.clone()
    };
    Some((name, args.as_object()?.clone()))
}
fn legacy(body: &str) -> Option<(String, Map<String, Value>)> {
    let c = LEGACY.captures(body.trim())?;
    let args = if c[2].is_empty() {
        json!({})
    } else {
        serde_json::from_str(&c[2]).ok()?
    };
    Some((c[1].to_owned(), args.as_object()?.clone()))
}
fn sorted(value: Value) -> Value {
    match value {
        Value::Object(m) => {
            let mut entries: Vec<_> = m.into_iter().collect();
            entries.sort_by(|a, b| a.0.cmp(&b.0));
            Value::Object(entries.into_iter().map(|(k, v)| (k, sorted(v))).collect())
        }
        Value::Array(a) => Value::Array(a.into_iter().map(sorted).collect()),
        other => other,
    }
}
impl Formatter for QwenFormatter {
    fn format_tools(&self, tools: &[ToolDef]) -> String {
        if tools.is_empty() {
            return String::new();
        }
        let lines: Vec<String> = tools
            .iter()
            .map(|t| {
                let mut f = Map::new();
                f.insert("name".into(), json!(t.name));
                f.insert("description".into(), json!(t.description));
                if let Some(s) = &t.input_schema {
                    f.insert("parameters".into(), json!(s));
                }
                sorted(json!({"type":"function", "function":f})).to_string()
            })
            .collect();
        format!(
            "# Tools\n\nYou may call one or more functions to assist with the user query.\n\nYou are provided with function signatures within <tools></tools> XML tags:\n<tools>\n{}\n</tools>\n\nFor each function call, return a json object with function name and arguments within <tool_call></tool_call> XML tags:\n<tool_call>\n{{\"name\": <function-name>, \"arguments\": <args-json-object>}}\n</tool_call>",
            lines.join("\n")
        )
    }
    fn parse_tool_calls(&self, response: &str) -> Result<Vec<ParsedToolCall>, String> {
        let spans: Vec<_> = SPAN.captures_iter(response).collect();
        let hermes = spans
            .iter()
            .any(|c| !c[1].trim_start().starts_with("<tool"));
        Ok(spans
            .iter()
            .filter_map(|c| {
                let body = c[1].trim();
                let decoded = if hermes {
                    if body.starts_with("<tool") {
                        None
                    } else {
                        decode(body)
                    }
                } else {
                    legacy(body)
                };
                decoded.map(|(name, args)| ParsedToolCall {
                    id: String::new(),
                    name,
                    args,
                    raw: c[0].to_owned(),
                })
            })
            .collect())
    }
    fn format_tool_result(&self, _name: &str, result: &ToolResult) -> String {
        let value = if result.success {
            json!(result.output)
        } else {
            json!({"error":result.error})
        };
        format!("<tool_response>\n{value}\n</tool_response>")
    }
    fn model_family(&self) -> &'static str {
        "qwen"
    }
    fn validate_format(&self, response: &str) -> Result<(), String> {
        let spans: Vec<_> = SPAN.captures_iter(response).collect();
        let hermes = spans
            .iter()
            .any(|c| !c[1].trim_start().starts_with("<tool"));
        for c in spans {
            let body = c[1].trim();
            if hermes && body.starts_with("<tool") {
                continue;
            }
            let valid = if hermes {
                decode(body).is_some()
            } else {
                legacy(body).is_some() && LEGACY.captures(body).is_some_and(|m| !m[2].is_empty())
            };
            if !valid {
                return Err("invalid tool call or arguments".into());
            }
        }
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn skips_bad_json_and_keeps_valid_sibling() {
        let text = r#"<tool_call>{bad}</tool_call><tool_call>{"name":"good","arguments":{"x":1}}</tool_call>"#;
        let calls = QwenFormatter.parse_tool_calls(text).unwrap();
        assert_eq!(calls.len(), 1);
        assert_eq!(calls[0].name, "good");
        assert_eq!(calls[0].args["x"], 1);
    }

    #[test]
    fn parses_string_encoded_arguments() {
        let text = r#"<tool_call>{"name":"good","arguments":"{\"x\":1}"}</tool_call>"#;
        let calls = QwenFormatter.parse_tool_calls(text).unwrap();
        assert_eq!(calls.len(), 1);
        assert_eq!(calls[0].args["x"], 1);
    }

    #[test]
    fn legacy_is_only_used_without_hermes() {
        let legacy = r#"<tool_call><tool name="legacy">{}</tool></tool_call>"#;
        assert_eq!(
            QwenFormatter.parse_tool_calls(legacy).unwrap()[0].name,
            "legacy"
        );
        let mixed = format!(r#"{legacy}<tool_call>{{"name":"new","arguments":{{}}}}</tool_call>"#);
        let calls = QwenFormatter.parse_tool_calls(&mixed).unwrap();
        assert_eq!(calls.len(), 1);
        assert_eq!(calls[0].name, "new");
        let malformed = format!("{legacy}<tool_call>{{bad}}</tool_call>");
        assert!(
            QwenFormatter
                .parse_tool_calls(&malformed)
                .unwrap()
                .is_empty()
        );
    }

    #[test]
    fn validate_rejects_bad_json() {
        assert!(
            QwenFormatter
                .validate_format("<tool_call>{bad}</tool_call>")
                .is_err()
        );
    }
}
