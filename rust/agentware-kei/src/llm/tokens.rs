//! Token estimation and model context-window table (port of `go/llm/tokens.go`).

use super::request::Message;
use serde_json::Value;

/// Estimates the number of tokens in a message. This is a rough
/// approximation; actual tokenizers vary by model. (Go `EstimateTokens`.)
pub fn estimate_tokens(text: &str) -> i64 {
    text.len() as i64 / 4
}

/// Estimates tokens for a slice of messages. (Go `EstimateMessagesTokens`.)
pub fn estimate_messages_tokens(messages: &[Message]) -> i64 {
    let mut total = 0i64;
    for message in messages {
        total += 4; // overhead per message
        total += estimate_tokens(&message.content);
        for tool_call in &message.tool_calls {
            total += estimate_tokens(&tool_call.name);
            for value in tool_call.args.values() {
                total += estimate_tokens(&format_go_value(value));
            }
        }
    }
    total
}

/// Returns the context window for known models, 0-mapped default of 4096 for
/// unknown names. (Go `GetModelContextWindow`.)
pub fn get_model_context_window(model_name: &str) -> i64 {
    // Go's `contains` helper is a plain case-sensitive substring search; the
    // table keeps Go's order (e.g. "gpt-4-turbo" before "gpt-4").
    let name = model_name;
    if name.contains("gpt-4o") || name.contains("gpt-4-turbo") {
        128_000
    } else if name.contains("gpt-4") {
        8_192
    } else if name.contains("gpt-3.5-turbo") {
        16_385
    } else if name.contains("claude-3-opus")
        || name.contains("claude-3-sonnet")
        || name.contains("claude-3-haiku")
    {
        200_000
    } else if name.contains("qwen") {
        32_768
    } else if name.contains("llama") {
        8_192
    } else if name.contains("mistral") {
        32_768
    } else {
        4_096
    }
}

/// Formats a JSON value the way Go's `fmt.Sprintf("%v", v)` prints the
/// equivalent dynamically-typed value, for token estimation parity with
/// `go/llm/tokens.go` (which formats each tool-call argument with `%v`).
///
/// Mapping: string → as-is, integer → decimal, float → shortest round-trip
/// form, bool → `true`/`false`, null → `<nil>`, array → `[a b c]`, object →
/// `map[k:v ...]` with keys sorted (Go prints maps sorted by key).
pub fn format_go_value(value: &Value) -> String {
    match value {
        Value::Null => "<nil>".to_owned(),
        Value::Bool(true) => "true".to_owned(),
        Value::Bool(false) => "false".to_owned(),
        Value::Number(number) => {
            if let Some(int) = number.as_i64() {
                int.to_string()
            } else if let Some(uint) = number.as_u64() {
                uint.to_string()
            } else {
                let float = number.as_f64().unwrap_or_default();
                format_float_go(float)
            }
        }
        Value::String(text) => text.clone(),
        Value::Array(items) => {
            let parts: Vec<String> = items.iter().map(format_go_value).collect();
            format!("[{}]", parts.join(" "))
        }
        Value::Object(entries) => {
            let mut keys: Vec<&String> = entries.keys().collect();
            keys.sort();
            let parts: Vec<String> = keys
                .iter()
                .map(|key| format!("{}:{}", key, format_go_value(&entries[*key])))
                .collect();
            format!("map[{}]", parts.join(" "))
        }
    }
}

/// Go `%v` of a float64 uses `strconv.FormatFloat(v, 'g', -1, 64)`: the
/// shortest representation that round-trips, switching to exponent form at
/// the same magnitudes. Rust's `Display` for `f64` is the shortest round-trip
/// form but never uses exponents; integral floats print without a decimal
/// point in both. This keeps the common cases identical.
fn format_float_go(value: f64) -> String {
    if value == value.trunc() && value.abs() < 1e21 {
        // Go %v prints integral floats without an exponent or decimal point.
        format!("{value:.0}")
    } else {
        format!("{value}")
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn estimate_tokens_matches_go() {
        let cases = [
            ("", 0),
            ("hello", 1),
            ("four chars", 2),
            ("exactly four", 3),
            ("five chars", 2),
            ("a b c d e f g h", 3),
        ];
        for (input, expected) in cases {
            assert_eq!(estimate_tokens(input), expected, "input {input:?}");
        }
    }

    #[test]
    fn estimate_messages_tokens_empty() {
        assert_eq!(estimate_messages_tokens(&[]), 0);
    }

    #[test]
    fn estimate_messages_tokens_single_message() {
        let messages = vec![Message {
            role: super::super::request::Role::User,
            content: "hello".into(),
            ..Default::default()
        }];
        assert!(estimate_messages_tokens(&messages) >= 4);
    }

    #[test]
    fn estimate_messages_tokens_with_tool_calls() {
        let mut args = serde_json::Map::new();
        args.insert("key".into(), json!(123));
        let messages = vec![Message {
            role: super::super::request::Role::Assistant,
            content: "using tool".into(),
            tool_calls: vec![super::super::response::ToolCall {
                metadata: None,
                name: "my_tool".into(),
                args,
                ..Default::default()
            }],
            ..Default::default()
        }];
        assert!(estimate_messages_tokens(&messages) >= 5);
    }

    #[test]
    fn get_model_context_window_matches_go() {
        let cases = [
            ("gpt-4o", 128_000),
            ("gpt-4o-mini", 128_000),
            ("gpt-4-turbo", 128_000),
            ("gpt-4", 8_192),
            ("gpt-3.5-turbo", 16_385),
            ("claude-3-opus", 200_000),
            ("claude-3-sonnet", 200_000),
            ("claude-3-haiku", 200_000),
            ("qwen2.5", 32_768),
            ("llama-3", 8_192),
            ("mistral-large", 32_768),
            ("unknown-model", 4_096),
            ("", 4_096),
        ];
        for (model, expected) in cases {
            assert_eq!(get_model_context_window(model), expected, "model {model:?}");
        }
    }

    #[test]
    fn format_go_value_matches_go_percent_v() {
        let cases = [
            (json!("plain"), "plain"),
            (json!(123), "123"),
            (json!(1.5), "1.5"),
            (json!(true), "true"),
            (json!(false), "false"),
            (json!(null), "<nil>"),
            (json!([1, "a"]), "[1 a]"),
            (json!({"b": 2, "a": 1}), "map[a:1 b:2]"),
        ];
        for (value, expected) in cases {
            assert_eq!(format_go_value(&value), expected, "value {value}");
        }
    }
}
