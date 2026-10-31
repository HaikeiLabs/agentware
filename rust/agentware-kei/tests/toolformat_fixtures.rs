//! Shared cross-language toolformat fixtures (`fixtures/toolformat/*-cases.json`).
//!
//! Every case asserts the same render/parse output the other language SDKs
//! produce against the same fixture files.

use agentware_kei::toolformat::{Formatter, ToolDef, ToolResult, for_model};
use serde_json::{Map, Value};
use std::path::{Path, PathBuf};

/// Appends one JSONL record for the equal-coverage gate
/// (`scripts/conformance/gate.py`); a no-op unless
/// AGENTWARE_CONFORMANCE_LOG is set. Called after a case's assertions, so
/// only passing cases are recorded.
fn record(fixture: &str, section: &str, case: &str) {
    let Some(log) = std::env::var_os("AGENTWARE_CONFORMANCE_LOG") else {
        return;
    };
    let line = serde_json::json!({"language": "rust", "fixture": fixture, "section": section, "case": case});
    let mut file = std::fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(&log)
        .unwrap_or_else(|error| panic!("open conformance log: {error}"));
    use std::io::Write;
    // One write call per record: the fixture tests run in parallel threads
    // against the same log, and writeln!'s fragmented writes would interleave.
    let mut row = line.to_string();
    row.push('\n');
    file.write_all(row.as_bytes())
        .unwrap_or_else(|error| panic!("write conformance log: {error}"));
}

fn fixture_dir() -> PathBuf {
    let manifest_dir = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
    manifest_dir
        .parent()
        .and_then(Path::parent)
        .expect("crate lives two levels below the repository root")
        .join("fixtures/toolformat")
}

fn load_fixture(name: &str) -> Value {
    let path = fixture_dir().join(name);
    let text = std::fs::read_to_string(&path)
        .unwrap_or_else(|error| panic!("read {}: {error}", path.display()));
    serde_json::from_str(&text).unwrap_or_else(|error| panic!("parse {}: {error}", path.display()))
}

/// The tool definitions shared by every family (`fixtures/toolformat/tools.json`).
fn shared_tools() -> Vec<ToolDef> {
    let v = load_fixture("tools.json");
    v["tools"]
        .as_array()
        .expect("tools.json has a tools array")
        .iter()
        .map(|t| ToolDef {
            name: t["name"].as_str().expect("tool name").to_owned(),
            description: t["description"]
                .as_str()
                .expect("tool description")
                .to_owned(),
            input_schema: t["input_schema"].as_object().cloned(),
        })
        .collect()
}

/// Select the formatter for a fixture family through the public selector,
/// using model ids the way real callers do.
fn family_formatter(family: &str) -> Box<dyn Formatter> {
    let model = family_model(family)
        .unwrap_or_else(|| panic!("no model id wired for fixture family {family}"));
    for_model(model)
}

/// The model id that selects a fixture family's formatter, or None for a
/// family with no Rust formatter yet.
fn family_model(family: &str) -> Option<&'static str> {
    match family {
        "openai" => Some("gpt-4"),
        "anthropic" => Some("claude-3"),
        "deepseek" => Some("deepseek-chat"),
        "glm" => Some("nvidia/GLM-5.3-Flash-FP4-Local"),
        "qwen" => Some("qwen2.5"),
        _ => None,
    }
}

fn assert_format_definitions(fixture: &str, family: &str, cases: &Value) {
    let formatter = family_formatter(family);
    let tools = shared_tools();
    for case in cases.as_array().expect("format_definitions array") {
        let name = case["name"].as_str().expect("case name");
        let got = formatter.format_tools(&tools);
        assert_eq!(
            got,
            case["expected"].as_str().expect("expected string"),
            "family {family}: format_definitions case {name:?}"
        );
        record(fixture, "format_definitions", name);
    }
}

fn assert_parse_calls(fixture: &str, family: &str, cases: &Value) {
    let formatter = family_formatter(family);
    for case in cases.as_array().expect("parse_calls array") {
        let name = case["name"].as_str().expect("case name");
        let got = formatter.parse_tool_calls(case["input"].as_str().expect("input"));
        let calls = got.unwrap_or_else(|error| {
            panic!("family {family}: parse_calls case {name:?} errored: {error}")
        });
        let expected = case["expected"].as_array().expect("expected array");
        assert_eq!(
            calls.len(),
            expected.len(),
            "family {family}: parse_calls case {name:?} call count"
        );
        for (call, want) in calls.iter().zip(expected) {
            let empty_args = Map::new();
            assert_eq!(
                call.name,
                want["name"].as_str().unwrap_or_default(),
                "family {family}: parse_calls case {name:?} name"
            );
            assert_eq!(
                call.id,
                want["id"].as_str().unwrap_or_default(),
                "family {family}: parse_calls case {name:?} id"
            );
            assert_eq!(
                &call.args,
                want["args"].as_object().unwrap_or(&empty_args),
                "family {family}: parse_calls case {name:?} args"
            );
        }
        record(fixture, "parse_calls", name);
    }
}

fn assert_format_result(fixture: &str, family: &str, cases: &Value) {
    let formatter = family_formatter(family);
    for case in cases.as_array().expect("format_result array") {
        let name = case["name"].as_str().expect("case name");
        let result = &case["result"];
        let tool_result = ToolResult {
            success: result["success"].as_bool().expect("result.success"),
            output: result["output"].as_str().unwrap_or_default().to_owned(),
            error: result["error"].as_str().unwrap_or_default().to_owned(),
        };
        let got = formatter
            .format_tool_result(case["tool_name"].as_str().expect("tool_name"), &tool_result);
        assert_eq!(
            got,
            case["expected"].as_str().expect("expected string"),
            "family {family}: format_result case {name:?}"
        );
        record(fixture, "format_result", name);
    }
}

fn assert_validate_format(fixture: &str, family: &str, cases: &Value) {
    let formatter = family_formatter(family);
    for case in cases.as_array().expect("validate_format array") {
        let name = case["name"].as_str().expect("case name");
        let got = formatter.validate_format(case["input"].as_str().expect("input"));
        assert_eq!(
            got.is_ok(),
            case["valid"].as_bool().expect("valid flag"),
            "family {family}: validate_format case {name:?} -> {got:?}"
        );
        record(fixture, "validate_format", name);
    }
}

fn assert_fixture_file(name: &str) {
    let v = load_fixture(name);
    let family = v["family"].as_str().expect("family");
    assert_eq!(
        family_formatter(family).model_family(),
        family,
        "selector family for {family}"
    );
    let fixture = format!("toolformat/{name}");
    assert_format_definitions(&fixture, family, &v["format_definitions"]);
    assert_parse_calls(&fixture, family, &v["parse_calls"]);
    assert_format_result(&fixture, family, &v["format_result"]);
    assert_validate_format(&fixture, family, &v["validate_format"]);
}

#[test]
fn openai_cases() {
    assert_fixture_file("openai-cases.json");
}

#[test]
fn anthropic_cases() {
    assert_fixture_file("anthropic-cases.json");
}

#[test]
fn deepseek_cases() {
    assert_fixture_file("deepseek-cases.json");
}

#[test]
fn glm_cases() {
    assert_fixture_file("glm-cases.json");
}

/// Candidate fixtures from live models (`live-*-cases.json`), staged here only
/// while scripts/live-captures compares the languages (docs/testing.md), so
/// this test has nothing to do in a normal run.
#[test]
fn live_capture_cases() {
    let mut names: Vec<String> = std::fs::read_dir(fixture_dir())
        .expect("fixtures/toolformat directory")
        .filter_map(|entry| entry.ok()?.file_name().into_string().ok())
        .filter(|name| name.starts_with("live-") && name.ends_with("-cases.json"))
        .collect();
    names.sort();
    for name in names {
        assert_fixture_file(&name);
    }
}

#[test]
fn qwen_cases() {
    assert_fixture_file("qwen-cases.json");
}
