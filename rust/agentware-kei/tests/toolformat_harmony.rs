//! GPT-OSS Harmony prompt renderer and completion parser.
//!
//! Replays the Harmony-only sections of `fixtures/toolformat/harmony-cases.json`:
//! `render_prompt` (whole conversations for raw `/v1/completions`) and
//! `parse_completion`. The four standard sections replay in
//! `toolformat_fixtures.rs`.

use haikeilabs_agentware::toolformat::{
    Formatter, HARMONY_STOP_TOKENS, HarmonyFormatter, HarmonyMessage, HarmonyPromptOptions,
    HarmonySystem, ToolDef, for_model, parse_harmony_completion, render_harmony_message,
    render_harmony_prompt,
};
use serde_json::Value;
use std::path::PathBuf;

fn fixture() -> Value {
    let path = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("../../fixtures/toolformat/harmony-cases.json");
    let text = std::fs::read_to_string(&path)
        .unwrap_or_else(|error| panic!("read {}: {error}", path.display()));
    serde_json::from_str(&text).expect("harmony-cases.json parses")
}

fn text(v: &Value, key: &str) -> String {
    v[key].as_str().unwrap_or_default().to_owned()
}

fn message(v: &Value) -> HarmonyMessage {
    HarmonyMessage {
        role: text(v, "role"),
        content: text(v, "content"),
        channel: text(v, "channel"),
        recipient: text(v, "recipient"),
        content_type: text(v, "content_type"),
        name: text(v, "name"),
    }
}

/// A missing "system" key is the default system message and null leaves it out.
fn system(case: &Value) -> Option<HarmonySystem> {
    let Some(spec) = case.get("system") else {
        return Some(HarmonySystem::default());
    };
    if spec.is_null() {
        return None;
    }
    let mut s = HarmonySystem::default();
    if let Some(v) = spec["model_identity"].as_str() {
        s.model_identity = v.to_owned();
    }
    if let Some(v) = spec["knowledge_cutoff"].as_str() {
        s.knowledge_cutoff = v.to_owned();
    }
    if let Some(v) = spec["current_date"].as_str() {
        s.current_date = v.to_owned();
    }
    if let Some(v) = spec["reasoning_effort"].as_str() {
        s.reasoning_effort = v.to_owned();
    }
    if let Some(v) = spec["valid_channels"].as_array() {
        s.valid_channels = v
            .iter()
            .map(|c| c.as_str().expect("channel").to_owned())
            .collect();
    }
    Some(s)
}

#[test]
fn render_prompt_cases() {
    let fx = fixture();
    for case in fx["render_prompt"].as_array().expect("render_prompt") {
        let name = text(case, "name");
        let tools = case["tools"]
            .as_array()
            .map(|tools| {
                tools
                    .iter()
                    .map(|t| ToolDef {
                        name: text(t, "name"),
                        description: text(t, "description"),
                        input_schema: t["input_schema"].as_object().cloned(),
                    })
                    .collect()
            })
            .unwrap_or_default();
        let opts = HarmonyPromptOptions {
            system: system(case),
            instructions: text(case, "instructions"),
            tools,
            auto_drop_analysis: case["auto_drop_analysis"].as_bool().unwrap_or(true),
            next_role: match case.get("next_role").and_then(Value::as_str) {
                Some("") => None,
                Some(role) => Some(role.to_owned()),
                None => Some("assistant".to_owned()),
            },
        };
        let messages: Vec<HarmonyMessage> = case["messages"]
            .as_array()
            .expect("messages")
            .iter()
            .map(message)
            .collect();
        let got = render_harmony_prompt(&messages, &opts)
            .unwrap_or_else(|error| panic!("{name}: {error}"));
        assert_eq!(got, text(case, "expected"), "render_prompt case {name:?}");
    }
}

#[test]
fn parse_completion_cases() {
    let fx = fixture();
    for case in fx["parse_completion"].as_array().expect("parse_completion") {
        let name = text(case, "name");
        let want = &case["expected"];
        let got = parse_harmony_completion(&text(case, "input"));
        assert_eq!(got.reasoning, text(want, "reasoning"), "{name}: reasoning");
        assert_eq!(got.content, text(want, "content"), "{name}: content");
        let calls: Vec<Value> = got
            .tool_calls
            .iter()
            .map(|c| serde_json::json!({"name": c.name, "args": c.args}))
            .collect();
        assert_eq!(
            &Value::Array(calls),
            &want["tool_calls"],
            "{name}: tool_calls"
        );
        let messages: Vec<HarmonyMessage> = want["messages"]
            .as_array()
            .expect("messages")
            .iter()
            .map(message)
            .collect();
        assert_eq!(got.messages, messages, "{name}: messages");
    }
}

#[test]
fn parsed_messages_render_back_into_history() {
    let parsed = parse_harmony_completion(
        r#"<|channel|>analysis<|message|>Need weather for Tokyo.<|end|><|start|>assistant<|channel|>commentary to=functions.get_weather <|constrain|>json<|message|>{"city":"Tokyo"}<|call|>"#,
    );
    let rendered: Vec<String> = parsed
        .messages
        .iter()
        .map(|m| render_harmony_message(m).expect("renders"))
        .collect();
    assert_eq!(
        rendered,
        [
            "<|start|>assistant<|channel|>analysis<|message|>Need weather for Tokyo.<|end|>",
            r#"<|start|>assistant to=functions.get_weather<|channel|>commentary <|constrain|>json<|message|>{"city":"Tokyo"}<|call|>"#,
        ]
    );
    let done = parse_harmony_completion("<|channel|>final<|message|>done<|return|>");
    assert_eq!(
        render_harmony_message(&done.messages[0]).expect("renders"),
        "<|start|>assistant<|channel|>final<|message|>done<|end|>"
    );
}

#[test]
fn stop_tokens() {
    assert_eq!(HARMONY_STOP_TOKENS, ["<|return|>", "<|call|>"]);
}

#[test]
fn rejects_invalid_messages() {
    let robot = HarmonyMessage::new("robot", "x");
    assert!(render_harmony_message(&robot).is_err());
    let nameless_tool = HarmonyMessage::new("tool", "x");
    assert!(render_harmony_message(&nameless_tool).is_err());
    let opts = HarmonyPromptOptions {
        system: Some(HarmonySystem {
            reasoning_effort: "max".to_owned(),
            ..HarmonySystem::default()
        }),
        ..HarmonyPromptOptions::default()
    };
    assert!(render_harmony_prompt(&[], &opts).is_err());
}

#[test]
fn for_model_routes_gpt_oss() {
    for model in [
        "openai/gpt-oss-20b",
        "gpt-oss-120b",
        "GPT-OSS-20B",
        "gpt_oss",
        "ggml-org/gpt-oss-20b-GGUF",
    ] {
        assert_eq!(for_model(model).model_family(), "gpt-oss", "{model}");
    }
    assert_eq!(for_model("gpt-4o").model_family(), "openai");
}

#[test]
fn no_tools_renders_nothing() {
    assert_eq!(HarmonyFormatter.format_tools(&[]), "");
}
