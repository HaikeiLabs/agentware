//! MiniMax checks the shared runner (`toolformat_fixtures.rs`) does not
//! cover: it replays `minimax-cases.json` through the M2 formatter, so this
//! file checks the M3 variant against the fixture's `m3_format_definitions`.

use haikeilabs_agentware::toolformat::{ToolDef, ToolResult, for_model};
use serde_json::Value;
use std::path::PathBuf;

fn fixture(name: &str) -> Value {
    let path = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("../../fixtures/toolformat")
        .join(name);
    let text = std::fs::read_to_string(&path)
        .unwrap_or_else(|error| panic!("read {}: {error}", path.display()));
    serde_json::from_str(&text).expect("fixture JSON")
}

fn shared_tools() -> Vec<ToolDef> {
    fixture("tools.json")["tools"]
        .as_array()
        .expect("tools array")
        .iter()
        .map(|t| ToolDef {
            name: t["name"].as_str().expect("name").to_owned(),
            description: t["description"].as_str().expect("description").to_owned(),
            input_schema: t["input_schema"].as_object().cloned(),
        })
        .collect()
}

#[test]
fn m3_format_definitions() {
    let cases = fixture("minimax-cases.json");
    let cases = cases["m3_format_definitions"]
        .as_array()
        .expect("m3_format_definitions");
    assert!(!cases.is_empty());
    let formatter = for_model("MiniMaxAI/MiniMax-M3");
    for case in cases {
        assert_eq!(
            formatter.format_tools(&shared_tools()),
            case["expected"].as_str().expect("expected"),
            "{}",
            case["name"]
        );
    }
}

#[test]
fn m3_variant_parses_validates_and_formats_like_m2() {
    let cases = fixture("minimax-cases.json");
    let (m2, m3) = (for_model("MiniMax-M2"), for_model("MiniMax-M3"));
    for case in cases["parse_calls"].as_array().expect("parse_calls") {
        let input = case["input"].as_str().expect("input");
        assert_eq!(
            m2.parse_tool_calls(input),
            m3.parse_tool_calls(input),
            "{}",
            case["name"]
        );
    }
    for case in cases["validate_format"]
        .as_array()
        .expect("validate_format")
    {
        let input = case["input"].as_str().expect("input");
        assert_eq!(
            m3.validate_format(input).is_ok(),
            case["valid"].as_bool().expect("valid"),
            "{}",
            case["name"]
        );
    }
    let result = ToolResult {
        success: true,
        output: "Sunny".into(),
        error: String::new(),
    };
    assert_eq!(
        m3.format_tool_result("get_weather", &result),
        "<response>Sunny</response>"
    );
}
