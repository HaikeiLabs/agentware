//! Shared cross-language reasoning fixtures (`fixtures/reasoning/*-cases.json`).
//!
//! Every case asserts the same analysis the other language SDKs produce
//! against the same fixture files: the extracted reasoning, the clean content
//! left after stripping, and fail-closed behavior. The analysis is the seam
//! shared by strip/extract/parse; parse only exposes the clean side and a
//! summarized tree, so the runner asserts on the analysis directly.

use agentware_kei::reasoning::Adapter;
use serde_json::Value;
use std::path::{Path, PathBuf};

/// Appends one JSONL record for the equal-coverage gate
/// (`scripts/conformance/gate.py`); a no-op unless
/// AGENTWARE_CONFORMANCE_LOG is set. Called after a case's assertions, so
/// only passing cases are recorded.
fn record(fixture: &str, case: &str) {
    let Some(log) = std::env::var_os("AGENTWARE_CONFORMANCE_LOG") else {
        return;
    };
    let line = serde_json::json!({"language": "rust", "fixture": fixture, "section": "examples", "case": case});
    let mut file = std::fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(&log)
        .unwrap_or_else(|error| panic!("open conformance log: {error}"));
    use std::io::Write;
    writeln!(file, "{line}").unwrap_or_else(|error| panic!("write conformance log: {error}"));
}

fn fixture_dir() -> PathBuf {
    let manifest_dir = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
    manifest_dir
        .parent()
        .and_then(Path::parent)
        .expect("crate lives two levels below the repository root")
        .join("fixtures/reasoning")
}

fn fixture_files() -> Vec<PathBuf> {
    let mut files: Vec<PathBuf> = std::fs::read_dir(fixture_dir())
        .expect("fixtures/reasoning directory")
        .filter_map(|entry| entry.ok().map(|entry| entry.path()))
        .filter(|path| {
            path.file_name()
                .and_then(|name| name.to_str())
                .is_some_and(|name| name.ends_with("-cases.json"))
        })
        .collect();
    files.sort();
    files
}

/// Runs one fixture case: a case whose expected clean content and reasoning
/// are both null is a fail-closed case — the adapter must return an error
/// rather than partial output; any other case asserts both expected values
/// (null meaning the empty string, as in the Go runner).
fn run_case(fixture: &str, case: &Value) -> Result<(), String> {
    let name = case["name"].as_str().expect("case name");
    let adapter = Adapter::new();
    let raw = &case["raw_response"];
    let want_clean = case["expected_clean_content"].as_str();
    let want_reasoning = case["expected_extracted_reasoning"].as_str();

    let result = adapter.analyze(raw, "");
    if want_clean.is_none() && want_reasoning.is_none() {
        if let Ok(analysis) = result {
            return Err(format!(
                "expected fail-closed error, got analysis clean={:?} reasoning={:?}",
                analysis.clean, analysis.reasoning
            ));
        }
    } else {
        let analysis = result.map_err(|error| format!("unexpected error: {error}"))?;
        let want_clean = want_clean.unwrap_or_default();
        let want_reasoning = want_reasoning.unwrap_or_default();
        if analysis.clean != want_clean {
            return Err(format!(
                "clean content = {:?}, want {:?}",
                analysis.clean, want_clean
            ));
        }
        if analysis.reasoning != want_reasoning {
            return Err(format!(
                "extracted reasoning = {:?}, want {:?}",
                analysis.reasoning, want_reasoning
            ));
        }
    }
    record(fixture, name);
    Ok(())
}

#[test]
fn reasoning_fixtures() {
    let files = fixture_files();
    assert!(!files.is_empty(), "no reasoning fixture files found");

    let mut failures = Vec::new();
    for file in &files {
        let text = std::fs::read_to_string(file)
            .unwrap_or_else(|error| panic!("read {}: {error}", file.display()));
        let fixture_value: Value = serde_json::from_str(&text)
            .unwrap_or_else(|error| panic!("parse {}: {error}", file.display()));
        let fixture = format!(
            "reasoning/{}",
            file.file_name().expect("file name").to_string_lossy()
        );

        for case in fixture_value["examples"]
            .as_array()
            .unwrap_or_else(|| panic!("{} has an examples array", file.display()))
        {
            let name = case["name"].as_str().expect("case name");
            if let Err(message) = run_case(&fixture, case) {
                failures.push(format!("{fixture}#examples/{name}: {message}"));
            }
        }
    }
    assert!(
        failures.is_empty(),
        "fixture failures:\n{}",
        failures.join("\n")
    );
}
