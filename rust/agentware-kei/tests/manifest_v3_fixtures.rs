//! Shared-fixture test for the kei.tool-manifest/v3 loader: walks
//! `fixtures/toolspec/manifest-v3/index.json` per that directory's README
//! (the loader contract) and records passing cases for the equal-coverage
//! gate, see `docs/testing.md`.

use agentware_kei::toolspec::{self, Route, ToolSpec};
use serde_json::{Value, json};
use std::path::{Path, PathBuf};

/// Fixture path under `fixtures/`, as the conformance gate names it.
const FIXTURE: &str = "toolspec/manifest-v3/index.json";

/// Root of the shared manifest-v3 fixtures at the repository root.
fn fixtures_root() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR"))
        .parent()
        .and_then(Path::parent)
        .expect("repository root from CARGO_MANIFEST_DIR")
        .join("fixtures/toolspec/manifest-v3")
}

fn read_json(root: &Path, relative: &str) -> Value {
    let data = std::fs::read(root.join(relative))
        .unwrap_or_else(|error| panic!("read {relative}: {error}"));
    serde_json::from_slice(&data).unwrap_or_else(|error| panic!("parse {relative}: {error}"))
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

#[test]
fn manifest_v3_loader_fixtures() {
    let root = fixtures_root();
    let index = read_json(&root, "index.json");
    let valid = index["valid"].as_array().expect("valid cases in index");
    let invalid = index["invalid"].as_array().expect("invalid cases in index");
    assert!(
        !valid.is_empty() && !invalid.is_empty(),
        "index must list valid and invalid cases: valid={} invalid={}",
        valid.len(),
        invalid.len()
    );

    let mut failures = Vec::new();
    for case in valid {
        let name = case["name"].as_str().expect("case name");
        match run_valid_case(&root, case) {
            Ok(()) => record("valid", name),
            Err(message) => failures.push(format!("valid/{name}: {message}")),
        }
    }
    for case in invalid {
        let name = case["name"].as_str().expect("case name");
        match run_invalid_case(&root, case) {
            Ok(()) => record("invalid", name),
            Err(message) => failures.push(format!("invalid/{name}: {message}")),
        }
    }
    assert!(
        failures.is_empty(),
        "fixture failures:\n{}",
        failures.join("\n")
    );
}

/// A valid case: the loader accepts the input file, the typed specs match the
/// normalized expected form, and marshalling round-trips to the canonical
/// document.
fn run_valid_case(root: &Path, case: &Value) -> Result<(), String> {
    let input = case["input"].as_str().expect("input path");
    let specs = toolspec::load_manifest_v3(root.join(input))
        .map_err(|error| format!("load_manifest_v3: {error}"))?;
    let data = std::fs::read(root.join(input)).map_err(|error| error.to_string())?;
    let parsed = toolspec::parse_manifest_v3(&data)
        .map_err(|error| format!("parse_manifest_v3: {error}"))?;
    if parsed != specs {
        return Err("load_manifest_v3 and parse_manifest_v3 disagree".to_owned());
    }

    let got = normalized(&specs);
    let want = read_json(root, case["expected"].as_str().expect("expected path"));
    if got != want {
        return Err(format!(
            "loaded specs differ from expected\n got:  {}\n want: {}",
            serde_json::to_string(&got).unwrap_or_default(),
            serde_json::to_string(&want).unwrap_or_default()
        ));
    }

    let out = toolspec::marshal_manifest_v3(&specs)
        .map_err(|error| format!("marshal_manifest_v3: {error}"))?;
    let round_tripped: Value = serde_json::from_slice(&out).map_err(|error| error.to_string())?;
    let canonical = read_json(root, case["canonical"].as_str().expect("canonical path"));
    if round_tripped != canonical {
        return Err(format!(
            "round trip differs from canonical\n got:  {}\n want: {}",
            String::from_utf8_lossy(&out),
            serde_json::to_string(&canonical).unwrap_or_default()
        ));
    }
    Ok(())
}

/// An invalid case: the loader rejects the input with a manifest error whose
/// code is the case's expected code.
fn run_invalid_case(root: &Path, case: &Value) -> Result<(), String> {
    let data = std::fs::read(root.join(case["input"].as_str().expect("input path")))
        .map_err(|error| error.to_string())?;
    let want = case["error"].as_str().expect("error code");
    match toolspec::parse_manifest_v3(&data) {
        Ok(_) => Err("parse_manifest_v3 accepted an invalid manifest".to_owned()),
        Err(error) => match error.code() {
            Some(code) if code == want => Ok(()),
            Some(code) => Err(format!(
                "error code = {code:?}, want {want:?} (error: {error})"
            )),
            None => Err(format!("error {error} carries no manifest code")),
        },
    }
}

/// Renders loaded specs in the README's normalized expected form,
/// `{"tools": [...]}`, so absent versus empty does not differ.
fn normalized(specs: &[ToolSpec]) -> Value {
    let tools = specs
        .iter()
        .map(|spec| {
            let route = match &spec.route {
                Route::HarnessExecutor(he) => json!({
                    "harness_executor": {
                        "executor": he.executor,
                        "registration": he.registration,
                    }
                }),
                Route::ConnectorBinding(cb) => json!({
                    "connector_binding": {
                        "agent_id": cb.agent_id,
                        "connector_id": cb.connector_id,
                    }
                }),
            };
            let resource_types: Vec<Value> = spec
                .resource_types
                .iter()
                .map(|rt| match &rt.parent_type {
                    Some(parent) => json!({"type": rt.r#type, "parent_type": parent}),
                    None => json!({"type": rt.r#type}),
                })
                .collect();
            json!({
                "name": spec.name,
                "description": spec.description,
                "parameters": spec.parameters.clone().unwrap_or_default(),
                "service": spec.service,
                "source": spec.source,
                "operation_class": spec.operation_class,
                "route": route,
                "required_capabilities": spec.required_capabilities,
                "resource_types": resource_types,
                "enabled": spec.enabled.unwrap_or(true),
            })
        })
        .collect::<Vec<_>>();
    json!({"tools": tools})
}
