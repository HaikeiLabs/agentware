//! Shared Guardrails facade cases (`fixtures/guardrails/cases.v1.json`):
//! replays every case against `agentware_kei::guardrails`, mirroring
//! go/middleware/guardrails facade_replay_test.go and contract_test.go.
//!
//! The guardrails family is not in the conformance gate's fixture families
//! (scripts/conformance/gate.py) and the Go replay does not record to the
//! conformance log, so this test does not record either.

use agentware_kei::guardrails::{ErrorCategory, ToolCall, load_config, new_guardrails};
use serde_json::{Map, Value};
use std::path::{Path, PathBuf};

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

/// Decodes one fixture tool call; args default to an empty object.
fn decode_tool_call(raw: &Value) -> ToolCall {
    let mut args = Map::new();
    if let Some(obj) = raw["args"].as_object() {
        args = obj.clone();
    }
    ToolCall {
        tool: raw["tool"].as_str().expect("tool call name").to_owned(),
        args,
    }
}

/// Mirrors go TestGuardrailsReplaySharedCases: every shared case loads its
/// config, seeds the session state, runs the recorded or checked input, and
/// must match the expected outcome, nudge kind, tool calls, and missing
/// steps or prerequisites.
#[test]
fn guardrails_replay_shared_cases() {
    let fixture = read_json(&fixtures_root().join("guardrails/cases.v1.json"));
    assert_eq!(
        fixture["version"].as_i64(),
        Some(1),
        "contract version = {}, want 1",
        fixture["version"]
    );
    let cases = fixture["cases"].as_array().expect("cases array");

    let mut failures = Vec::new();
    for case in cases {
        let name = case["name"].as_str().expect("case name");
        if let Err(message) = run_case(case) {
            failures.push(format!("{name}: {message}"));
        }
    }
    assert!(
        failures.is_empty(),
        "fixture failures:\n{}",
        failures.join("\n")
    );
}

fn run_case(case: &Value) -> Result<(), String> {
    let config_bytes = serde_json::to_vec(&case["config"]).map_err(|error| error.to_string())?;
    let config = load_config(&config_bytes).map_err(|error| format!("load config: {error}"))?;
    let mut guardrails =
        new_guardrails(config).map_err(|error| format!("create guardrails: {error}"))?;
    const SESSION: &str = "fixture-session";

    for completed in case["input"]["state"]["completed_steps"]
        .as_array()
        .into_iter()
        .flatten()
    {
        guardrails.record(
            SESSION,
            &decode_tool_call(completed),
            None,
            ErrorCategory::Unknown,
        );
    }
    let seeded_violations = case["input"]["state"]["prereq_violations"]
        .as_i64()
        .unwrap_or(0);
    if seeded_violations > 0 {
        let rule = guardrails_prerequisite_rule(case)
            .ok_or("fixture seeds prerequisite violations without a prerequisite rule")?;
        for _ in 0..seeded_violations {
            guardrails.check(
                SESSION,
                &[ToolCall {
                    tool: rule.clone(),
                    args: Map::new(),
                }],
            );
        }
    }

    let result = if case["input"]["record"].is_object() {
        let record = &case["input"]["record"];
        guardrails.record(
            SESSION,
            &ToolCall {
                tool: record["tool"].as_str().expect("record tool").to_owned(),
                args: Map::new(),
            },
            Some(record["error"].as_str().expect("record error")),
            ErrorCategory::parse(record["category"].as_str().expect("record category")),
        )
    } else {
        let calls: Vec<ToolCall> = case["input"]["response"]["tool_calls"]
            .as_array()
            .expect("response tool_calls")
            .iter()
            .map(decode_tool_call)
            .collect();
        guardrails.check(SESSION, &calls)
    };

    let expected = &case["expected"];
    if result.outcome.as_str() != expected["outcome"].as_str().expect("expected outcome") {
        return Err(format!(
            "outcome = {}, want {}",
            result.outcome.as_str(),
            expected["outcome"]
        ));
    }
    if let Some(kind) = expected["nudge_kind"].as_str() {
        let nudge = result.nudge.as_ref().ok_or("expected a nudge")?;
        if nudge.kind.as_str() != kind {
            return Err(format!("nudge kind = {}, want {kind}", nudge.kind.as_str()));
        }
    }
    if expected.get("tool_calls").is_some() {
        let want: Vec<ToolCall> = expected["tool_calls"]
            .as_array()
            .expect("expected tool_calls")
            .iter()
            .map(decode_tool_call)
            .collect();
        if result.tool_calls != want {
            return Err(format!(
                "tool calls = {:?}, want {:?}",
                result.tool_calls, want
            ));
        }
    }
    if expected.get("missing_steps").is_some() {
        let want: Vec<String> = expected["missing_steps"]
            .as_array()
            .expect("expected missing_steps")
            .iter()
            .map(|v| v.as_str().expect("string").to_owned())
            .collect();
        if result.missing_steps != want {
            return Err(format!(
                "missing steps = {:?}, want {want:?}",
                result.missing_steps
            ));
        }
    }
    if expected.get("missing_prerequisites").is_some() {
        let want: Vec<String> = expected["missing_prerequisites"]
            .as_array()
            .expect("expected missing_prerequisites")
            .iter()
            .map(|v| v.as_str().expect("string").to_owned())
            .collect();
        if result.missing_prerequisites != want {
            return Err(format!(
                "missing prerequisites = {:?}, want {want:?}",
                result.missing_prerequisites
            ));
        }
    }
    if expected["prereq_violations"].as_i64().unwrap_or(0) != 0
        && result.prereq_violations != expected["prereq_violations"].as_i64().unwrap_or(0)
    {
        return Err(format!(
            "prerequisite violations = {}, want {}",
            result.prereq_violations, expected["prereq_violations"]
        ));
    }
    Ok(())
}

/// The first prerequisite rule's tool, which the Go replay uses to seed
/// prerequisite violations.
fn guardrails_prerequisite_rule(case: &Value) -> Option<String> {
    // Re-decode the config to reach the typed prerequisites, the same way the
    // Go replay reads config.Prerequisites[0].Tool.
    let config_bytes = serde_json::to_vec(&case["config"]).ok()?;
    let config = load_config(&config_bytes).ok()?;
    config.prerequisites.first().map(|rule| rule.tool.clone())
}

/// Mirrors go TestSharedContractCasesReplayable: the shared v1 cases stay
/// consumable — version 1, at least one case, unique non-empty names, valid
/// JSON config and input, and only the five facade outcomes.
#[test]
fn shared_contract_cases_replayable() {
    let fixture = read_json(&fixtures_root().join("guardrails/cases.v1.json"));
    assert_eq!(
        fixture["version"].as_i64(),
        Some(1),
        "contract version = {}, want 1",
        fixture["version"]
    );
    let cases = fixture["cases"].as_array().expect("cases array");
    assert!(!cases.is_empty(), "shared contract has no cases");

    let allowed_outcomes = ["execute", "retry", "step_blocked", "fatal", "recorded"];
    let mut seen: Vec<&str> = Vec::new();
    for case in cases {
        let name = case["name"].as_str().expect("case name");
        assert!(!name.is_empty(), "case name is empty");
        assert!(!seen.contains(&name), "case name is duplicated: {name}");
        seen.push(name);
        // Go checks json.Valid on the raw config and input; once the fixture
        // parses, the equivalent is that both fields are present.
        assert!(case.get("config").is_some(), "config is missing");
        assert!(case.get("input").is_some(), "input is missing");
        let outcome = case["expected"]["outcome"]
            .as_str()
            .expect("expected outcome");
        assert!(
            allowed_outcomes.contains(&outcome),
            "unsupported facade outcome {outcome:?}"
        );
    }
}
