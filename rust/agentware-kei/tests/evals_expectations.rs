//! Replays the shared eval-expectations fixture (`fixtures/evals/expectations`)
//! through `EvalRunner` with a scripted backend. Go, Python and TypeScript
//! replay the same cases.

use std::sync::Mutex;

use haikeilabs_agentware::evals::{EvalCase, EvalRunner, report_row};
use haikeilabs_agentware::llm::{Backend, Request, Response, ToolCall};
use serde_json::{Map, Value, json};

struct ScriptedBackend {
    turns: Vec<Value>,
    consumed: Mutex<usize>,
}

impl ScriptedBackend {
    fn new(turns: Vec<Value>) -> Self {
        Self {
            turns,
            consumed: Mutex::new(0),
        }
    }

    fn consumed(&self) -> usize {
        *self.consumed.lock().unwrap()
    }
}

impl Backend for ScriptedBackend {
    fn complete(&self, _req: &Request) -> Result<Response, String> {
        let mut consumed = self.consumed.lock().unwrap();
        let turn = self
            .turns
            .get(*consumed)
            .ok_or_else(|| "script exhausted".to_string())?;
        *consumed += 1;
        let tool_calls = turn
            .get("tool_calls")
            .and_then(Value::as_array)
            .map(|calls| {
                calls
                    .iter()
                    .enumerate()
                    .map(|(i, tc)| ToolCall {
                        id: format!("call-{consumed}-{i}"),
                        name: tc["name"].as_str().unwrap().to_string(),
                        args: tc["arguments"].as_object().cloned().unwrap_or_default(),
                        metadata: None,
                    })
                    .collect()
            })
            .unwrap_or_default();
        Ok(Response {
            content: turn["content"].as_str().unwrap_or_default().to_string(),
            tool_calls,
            ..Response::default()
        })
    }

    fn supports_native_tool_calling(&self) -> bool {
        true
    }

    fn model_name(&self) -> String {
        "scripted".into()
    }

    fn context_window_size(&self) -> i64 {
        0
    }
}

fn fixture() -> Value {
    serde_json::from_str(include_str!(
        "../../../fixtures/evals/expectations/cases.json"
    ))
    .unwrap()
}

fn case_with_tools(case: &Value, tools: &Value) -> EvalCase {
    let mut obj = case.as_object().unwrap().clone();
    obj.insert("tools".into(), tools.clone());
    EvalCase::from_json(&Value::Object(obj)).unwrap()
}

fn ok_executor(_name: &str, _args: &Map<String, Value>) -> String {
    "ok".into()
}

#[test]
fn eval_expectations_fixture() {
    let data = fixture();
    let cases = data["cases"].as_array().unwrap();
    assert!(!cases.is_empty());
    for entry in cases {
        let case = case_with_tools(&entry["case"], &data["tools"]);
        let turns: Vec<Value> = entry["script"]
            .as_array()
            .unwrap()
            .iter()
            .flat_map(|repeat| repeat.as_array().unwrap().clone())
            .collect();
        let backend = ScriptedBackend::new(turns.clone());
        let result = EvalRunner::new(10).run_case(&case, &backend, &mut ok_executor);

        let want = &entry["expected"];
        let name = &case.name;
        assert_eq!(
            result.success,
            want["success"].as_bool().unwrap(),
            "{name}: {}",
            result.error
        );
        assert_eq!(
            result.passes as u64,
            want["passes"].as_u64().unwrap(),
            "{name}"
        );
        assert_eq!(
            result.repeats as u64,
            want["repeats"].as_u64().unwrap(),
            "{name}"
        );
        assert_eq!(
            result.turns as u64,
            want["turns"].as_u64().unwrap(),
            "{name}"
        );
        let names: Vec<&str> = result
            .tool_calls
            .iter()
            .map(|tc| tc.name.as_str())
            .collect();
        let want_names: Vec<&str> = want["toolCalls"]
            .as_array()
            .unwrap()
            .iter()
            .map(|v| v.as_str().unwrap())
            .collect();
        assert_eq!(names, want_names, "{name}");
        if let Some(error) = want.get("error").and_then(Value::as_str) {
            assert_eq!(result.error, error, "{name}");
        }
        if let Some(fragment) = want.get("errorContains").and_then(Value::as_str) {
            assert!(result.error.contains(fragment), "{name}: {}", result.error);
        }
        assert_eq!(backend.consumed(), turns.len(), "{name}");
    }
}

#[test]
fn contract_defaults() {
    let case = EvalCase::from_json(&json!({
        "name": "n",
        "description": "d",
        "systemPrompt": "s",
        "userMessage": "u",
        "tools": [],
        "expectedTool": "t",
        "maxTurns": 2
    }))
    .unwrap();
    assert_eq!(case.expect, "call");
    assert!(case.expected_args.is_none());
    assert_eq!(case.repeats, 1);
    assert_eq!(case.min_pass_rate, 1.0);
    assert_eq!(case.max_turns, 2);
}

#[test]
fn report_row_keeps_single_run_shape() {
    let data = fixture();
    let case = case_with_tools(
        &json!({"name": "legacy", "userMessage": "hi", "expectedTool": "get_weather", "maxTurns": 1}),
        &data["tools"],
    );
    let backend = ScriptedBackend::new(vec![json!({"content": "hi"})]);
    let result = EvalRunner::new(10).run_case(&case, &backend, &mut ok_executor);
    let row = report_row(&result);
    let keys: Vec<&str> = row
        .as_object()
        .unwrap()
        .keys()
        .map(String::as_str)
        .collect();
    assert_eq!(
        keys,
        [
            "case",
            "model",
            "success",
            "turns",
            "tool_calls",
            "error",
            "duration_ms"
        ]
    );
}

#[test]
fn report_row_adds_passes_and_repeats_when_repeated() {
    let data = fixture();
    let case = case_with_tools(
        &json!({"name": "repeated", "userMessage": "hi", "expectedTool": "get_weather",
                "expect": "no_call", "repeats": 2, "maxTurns": 1}),
        &data["tools"],
    );
    let backend = ScriptedBackend::new(vec![json!({"content": "a"}), json!({"content": "b"})]);
    let result = EvalRunner::new(10).run_case(&case, &backend, &mut ok_executor);
    let row = report_row(&result);
    assert_eq!(row["passes"], 2);
    assert_eq!(row["repeats"], 2);
}
