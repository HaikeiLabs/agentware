//! Shared reasoning-tag constants (`haikeilabs_agentware::reasoning::tags`)
//! and the shared response-validator fixture
//! (`fixtures/reasoning/validator-rescue.v1.json`), mirroring
//! go/reasoning/tags_test.go and
//! go/middleware/guardrails/response_validator_fixtures_test.go.
//!
//! The expected tag names are spelled without their delimiters on purpose:
//! the tags themselves are never typed literally (see `reasoning/tags.rs`).

use haikeilabs_agentware::guardrails::{ResponseValidator, ToolCall};
use haikeilabs_agentware::reasoning::{
    Adapter, BRACKET_THINK_CLOSE, BRACKET_THINK_OPEN, REASONING_TAG_PAIRS, THINK_CLOSE, THINK_OPEN,
    THINKING_CLOSE, THINKING_OPEN, TagPair,
};
use serde_json::{Map, Value};
use std::path::Path;

#[test]
fn tag_pairs_cover_think_thinking_and_bracket_think() {
    let want = [
        ("<", "think", ">"),
        ("<", "thinking", ">"),
        ("[", "THINK", "]"),
    ];
    assert_eq!(REASONING_TAG_PAIRS.len(), 3);
    for (pair, (lead, name, trail)) in REASONING_TAG_PAIRS.iter().zip(want) {
        assert_eq!(pair.open, format!("{lead}{name}{trail}"));
        assert_eq!(pair.close, format!("{lead}/{name}{trail}"));
    }
}

#[test]
fn tag_constants_match_pairs() {
    assert_eq!(
        REASONING_TAG_PAIRS,
        [
            TagPair {
                open: THINK_OPEN,
                close: THINK_CLOSE
            },
            TagPair {
                open: THINKING_OPEN,
                close: THINKING_CLOSE
            },
            TagPair {
                open: BRACKET_THINK_OPEN,
                close: BRACKET_THINK_CLOSE
            },
        ]
    );
}

#[test]
fn adapter_strips_every_tag_pair() {
    let adapter = Adapter::default();
    for pair in REASONING_TAG_PAIRS {
        for upper in [false, true] {
            let (open, close) = if upper {
                (pair.open.to_uppercase(), pair.close.to_uppercase())
            } else {
                (pair.open.to_owned(), pair.close.to_owned())
            };
            let raw = Value::String(format!("{open}\nplan the call\n{close}answer"));
            let clean = adapter
                .strip(&raw)
                .unwrap_or_else(|e| panic!("{open}: {e}"));
            assert_eq!(clean, "answer", "{open}");
        }
    }
}

#[test]
fn adapter_fails_closed_on_unclosed_think() {
    let raw = Value::String(format!("{THINK_OPEN}never closed"));
    assert!(Adapter::default().strip(&raw).is_err());
}

#[test]
fn response_validator_shared_reasoning_fixture() {
    let path = Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("../../fixtures/reasoning/validator-rescue.v1.json");
    let data = std::fs::read(&path).unwrap_or_else(|e| panic!("read {}: {e}", path.display()));
    let fx: Value = serde_json::from_slice(&data).expect("parse fixture");
    let tool_names: Vec<String> = fx["tool_names"]
        .as_array()
        .expect("tool_names")
        .iter()
        .map(|v| v.as_str().expect("tool name").to_owned())
        .collect();
    let cases = fx["cases"].as_array().expect("cases");
    assert!(!cases.is_empty());
    for case in cases {
        let name = case["name"].as_str().expect("name");
        let want: Vec<ToolCall> = case["expected_tool_calls"]
            .as_array()
            .expect("expected_tool_calls")
            .iter()
            .map(|c| ToolCall {
                tool: c["tool"].as_str().expect("tool").to_owned(),
                args: c["args"].as_object().cloned().unwrap_or_else(Map::new),
            })
            .collect();
        let rv = ResponseValidator::new(tool_names.clone(), true);
        let got = rv.validate_text_response(case["response"].as_str().expect("response"));
        if want.is_empty() {
            assert!(got.needs_retry, "{name}: want retry");
            assert!(got.tool_calls.is_empty(), "{name}: want no calls");
        } else {
            assert!(!got.needs_retry, "{name}: want rescued calls");
            assert_eq!(got.tool_calls, want, "{name}");
        }
    }
}
