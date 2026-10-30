//! Shared ToolSpec contract cases (`fixtures/toolspec/cases.v1.json`):
//! replays every case against `agentware_kei::toolspec`, mirroring
//! go/toolspec/cases_test.go and python/tests/toolspec_test.py, and records
//! passing cases for the equal-coverage gate (`scripts/conformance/gate.py`,
//! see `docs/testing.md`).

use agentware_kei::toolformat::{
    AnthropicFormatter, DeepSeekFormatter, Formatter, GLMFormatter, GenericFormatter,
    OpenAIFormatter, ToolDef,
};
use agentware_kei::toolspec::{self, CODE_MISSING_FIELD, ToolSpec};
use serde_json::{Value, json};
use std::path::{Path, PathBuf};

/// Fixture path under `fixtures/`, as the conformance gate names it.
const FIXTURE: &str = "toolspec/cases.v1.json";

/// Root of the shared fixtures at the repository root.
fn fixtures_root() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR"))
        .parent()
        .and_then(Path::parent)
        .expect("repository root from CARGO_MANIFEST_DIR")
        .join("fixtures")
}

fn read_json(path: &Path) -> Value {
    let data =
        std::fs::read(path).unwrap_or_else(|error| panic!("read {}: {error}", path.display()));
    serde_json::from_slice(&data)
        .unwrap_or_else(|error| panic!("parse {}: {error}", path.display()))
}

/// Appends one JSONL record for the equal-coverage gate
/// (`scripts/conformance/gate.py`); a no-op unless
/// AGENTWARE_CONFORMANCE_LOG is set. Called after a case's assertions, so
/// only passing cases are recorded.
fn record(section: &str, case: &str) {
    let Some(log) = std::env::var_os("AGENTWARE_CONFORMANCE_LOG") else {
        return;
    };
    let line = json!({"language": "rust", "fixture": FIXTURE, "section": section, "case": case});
    let mut file = std::fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(&log)
        .unwrap_or_else(|error| panic!("open conformance log: {error}"));
    use std::io::Write;
    writeln!(file, "{line}").unwrap_or_else(|error| panic!("write conformance log: {error}"));
}

/// Decode one fixture `spec` into a typed ToolSpec through the manifest
/// loader — the Rust path from wire JSON to a spec (Go unmarshals into
/// `toolspec.ToolSpec` directly). Every wire case carries a route, which the
/// typed spec requires.
fn decode_spec(spec: &Value) -> Result<ToolSpec, String> {
    let document = json!({"schema": toolspec::MANIFEST_SCHEMA_V3, "tools": [spec]});
    let data = serde_json::to_vec(&document).map_err(|error| error.to_string())?;
    let mut specs = toolspec::parse_manifest_v3(&data).map_err(|error| error.to_string())?;
    Ok(specs.swap_remove(0))
}

/// The manifest `tools` array of `fixtures/kei/tool-manifest.v3.json`, which
/// the wire cases must reproduce exactly.
fn manifest_tools(root: &Path) -> Vec<Value> {
    let manifest = read_json(&root.join("kei/tool-manifest.v3.json"));
    manifest["tools"]
        .as_array()
        .expect("tool-manifest.v3.json has a tools array")
        .clone()
}

/// Mirrors go/toolspec TestCasesV1ReplayManifestWire: every expected_wire
/// must equal one tool of tool-manifest.v3.json, and the cases must cover
/// both manifest routes.
#[test]
fn cases_v1_replay_manifest_wire() {
    let root = fixtures_root();
    let fixture = read_json(&root.join("toolspec/cases.v1.json"));
    let cases = fixture["cases"].as_array().expect("cases array");
    assert!(
        cases.len() >= 4,
        "got {} cases, want at least 4",
        cases.len()
    );

    let manifest = manifest_tools(&root);
    let mut matched_manifest_tools = 0usize;
    let mut found_routes = std::collections::HashSet::new();
    let mut failures = Vec::new();
    for case in cases {
        let Some(expected_wire) = case.get("expected_wire") else {
            continue;
        };
        let name = case["name"].as_str().expect("case name");
        if manifest.iter().any(|tool| tool == expected_wire) {
            matched_manifest_tools += 1;
        } else {
            failures.push(format!(
                "{name}: expected wire does not equal a tool in tool-manifest.v3.json"
            ));
        }
        if let Some(route) = expected_wire["route"].as_object() {
            if route.contains_key("harness_executor") {
                found_routes.insert("harness_executor");
            }
            if route.contains_key("connector_binding") {
                found_routes.insert("connector_binding");
            }
        }
    }
    if matched_manifest_tools != 2
        || !found_routes.contains("harness_executor")
        || !found_routes.contains("connector_binding")
    {
        failures.push(format!(
            "cases must include both manifest routes; matched={matched_manifest_tools} routes={found_routes:?}"
        ));
    }
    assert!(
        failures.is_empty(),
        "fixture failures:\n{}",
        failures.join("\n")
    );
}

/// Mirrors go/toolspec TestMarshalManifestV3UsesContractCases: each case runs
/// through the Rust toolspec and must produce the shared expected result —
/// the manifest wire object for wire cases, a rejection for error cases.
#[test]
fn marshal_manifest_v3_uses_contract_cases() {
    let root = fixtures_root();
    let fixture = read_json(&root.join("toolspec/cases.v1.json"));
    let cases = fixture["cases"].as_array().expect("cases array");
    let mut failures = Vec::new();
    for case in cases {
        let name = case["name"].as_str().expect("case name");
        match run_marshal_case(case) {
            Ok(()) => record("cases", name),
            Err(message) => failures.push(format!("{name}: {message}")),
        }
    }
    assert!(
        failures.is_empty(),
        "fixture failures:\n{}",
        failures.join("\n")
    );
}

fn run_marshal_case(case: &Value) -> Result<(), String> {
    if let Some(expected_error) = case["expected_error"].as_str() {
        return run_error_case(&case["spec"], expected_error);
    }
    let Some(expected_wire) = case.get("expected_wire") else {
        return Ok(()); // toolformat-only case; covered by the formatter test
    };
    let spec = decode_spec(&case["spec"])?;
    let out = toolspec::marshal_tool_v3(&spec)
        .map_err(|error| format!("marshal {}: {error}", spec.name))?;
    let got: Value =
        serde_json::from_slice(&out).map_err(|error| format!("marshal output: {error}"))?;
    if got != *expected_wire {
        return Err(format!(
            "wire for {} differs\ngot:  {got}\nwant: {expected_wire}",
            spec.name
        ));
    }
    Ok(())
}

/// Go rejects a routeless spec at marshal time with "route is required"
/// (go/toolspec `toWire`). Rust's typed ToolSpec cannot represent a routeless
/// tool, so the equivalent fail-closed check is the manifest decode's
/// missing_field rejection at /tools/0/route.
fn run_error_case(spec: &Value, expected_error: &str) -> Result<(), String> {
    if expected_error != "route is required" {
        return Err(format!(
            "no Rust mapping for expected error {expected_error:?}"
        ));
    }
    let document = json!({"schema": toolspec::MANIFEST_SCHEMA_V3, "tools": [spec]});
    let data = serde_json::to_vec(&document).map_err(|error| error.to_string())?;
    match toolspec::parse_manifest_v3(&data) {
        Ok(_) => Err("routeless spec was accepted".to_owned()),
        Err(error) if error.code() == Some(CODE_MISSING_FIELD) => {
            if error.path() == Some("/tools/0/route") {
                Ok(())
            } else {
                Err(format!(
                    "missing_field at {}, want /tools/0/route",
                    error.path().unwrap_or_default()
                ))
            }
        }
        Err(error) => Err(format!("expected missing_field, got {error}")),
    }
}

/// The formatter each fixture flavor maps to; an unknown flavor fails the
/// same way Go's missing-formatter fatal does.
fn fixture_formatter(flavor: &str) -> Box<dyn Formatter> {
    match flavor {
        "generic" => Box::new(GenericFormatter),
        "openai" => Box::new(OpenAIFormatter),
        "deepseek" => Box::new(DeepSeekFormatter),
        "glm" => Box::new(GLMFormatter),
        "anthropic" => Box::new(AnthropicFormatter),
        other => panic!("no formatter registered for fixture flavor {other:?}"),
    }
}

/// The model-facing fields the formatters consume (go/toolformat
/// `adaptToolSpecs`: name, description, parameters). Rust's formatters take a
/// ToolDef, which carries exactly those fields.
fn model_definition(spec: &Value) -> ToolDef {
    ToolDef {
        name: spec["name"].as_str().expect("spec name").to_owned(),
        description: spec["description"]
            .as_str()
            .expect("spec description")
            .to_owned(),
        input_schema: spec["parameters"].as_object().cloned(),
    }
}

/// Mirrors go/toolspec TestGenericToolSpecFormatterUsesModelFieldsOnly: every
/// expected_toolformat entry is replayed through the matching Rust formatter
/// and compared semantically as JSON or exactly as text, per the fixture's
/// `toolformat_compare` mode.
#[test]
fn generic_tool_spec_formatter_uses_model_fields_only() {
    let root = fixtures_root();
    let fixture = read_json(&root.join("toolspec/cases.v1.json"));
    let compare = &fixture["toolformat_compare"];
    let cases = fixture["cases"].as_array().expect("cases array");
    let mut failures = Vec::new();
    for case in cases {
        let Some(expected_toolformat) = case["expected_toolformat"].as_object() else {
            continue;
        };
        let name = case["name"].as_str().expect("case name");
        let definitions = [model_definition(&case["spec"])];
        for (flavor, expected) in expected_toolformat {
            let expected = expected.as_str().expect("expected output string");
            let formatter = fixture_formatter(flavor);
            let got = formatter.format_tools(&definitions);
            match compare[flavor.as_str()].as_str() {
                Some("json") => {
                    let got_json: Value = match serde_json::from_str(&got) {
                        Ok(value) => value,
                        Err(error) => {
                            failures.push(format!("{name}/{flavor} output is not JSON: {error}"));
                            continue;
                        }
                    };
                    let expected_json: Value =
                        serde_json::from_str(expected).unwrap_or_else(|error| {
                            panic!("{name}/{flavor} fixture is not JSON: {error}")
                        });
                    if got_json != expected_json {
                        failures.push(format!(
                            "{name}/{flavor} JSON output differs semantically: got {got}, want {expected}"
                        ));
                    }
                }
                Some("text") => {
                    if got != expected {
                        failures.push(format!(
                            "{name}/{flavor} text output differs: got {got:?}, want {expected:?}"
                        ));
                    }
                }
                other => panic!("missing or invalid comparison mode for {flavor:?}: {other:?}"),
            }
        }
    }
    assert!(
        failures.is_empty(),
        "fixture failures:\n{}",
        failures.join("\n")
    );
}
