use agentware_kei::guardrails::shared::Guardrails;
use serde_json::Value;

#[test]
fn shared_guardrails_cases() {
    let data: Value =
        serde_json::from_str(include_str!("../../../fixtures/guardrails/cases.v1.json")).unwrap();
    for case in data["cases"].as_array().unwrap() {
        let mut guard = Guardrails::new(case["config"].clone()).unwrap();
        let input = &case["input"];
        let result = if let Some(record) = input.get("record") {
            guard.record(record, input.get("state")).unwrap()
        } else {
            guard.check(&input["response"], input.get("state")).unwrap()
        };
        for (key, value) in case["expected"].as_object().unwrap() {
            assert_eq!(
                result.get(key),
                Some(value),
                "case {} key {key}",
                case["name"]
            );
        }
    }
}

#[test]
fn safely_rescues_fenced_json_and_rejects_other_text() {
    let mut guard =
        Guardrails::new(serde_json::json!({"version":1,"tool_names":["read"]})).unwrap();
    let result = guard
        .check_text("```json\n{\"tool\":\"read\",\"args\":{\"path\":\"x\"}}\n```")
        .unwrap();
    assert_eq!(result["outcome"], "execute");
    assert_eq!(result["tool_calls"][0]["args"]["path"], "x");
    assert_eq!(
        guard.check_text("read(__import__('os'))").unwrap()["outcome"],
        "retry"
    );
    assert_eq!(
        guard.check_text("{\"tool\":\"unknown\"}").unwrap()["outcome"],
        "retry"
    );
}

#[test]
fn multi_call_terminal_and_missing_match_arguments_do_not_bypass_checks() {
    let mut guard = Guardrails::new(serde_json::json!({"version":1,"tool_names":["read","write","done"],"required_steps":["read"],"terminal_tool":"done","prerequisites":[{"tool":"write","requires":"read","match_arg":"path"}]})).unwrap();
    assert_eq!(
        guard
            .check(&serde_json::json!({"tool_calls":[]}), None)
            .unwrap()["outcome"],
        "retry"
    );
    assert_eq!(guard.check(&serde_json::json!({"tool_calls":[{"tool":"read","args":{}},{"tool":"done","args":{}}]}), None).unwrap()["outcome"], "step_blocked");
    guard
        .record(&serde_json::json!({"tool":"read","args":{}}), None)
        .unwrap();
    let result = guard
        .check(
            &serde_json::json!({"tool_calls":[{"tool":"write","args":{}}]}),
            None,
        )
        .unwrap();
    assert_eq!(result["outcome"], "retry");
    assert!(result["nudge_text"].as_str().unwrap().contains("read"));
}

#[test]
fn rescues_qwen_mistral_rehearsal_and_ignores_thinking() {
    let mut guard =
        Guardrails::new(serde_json::json!({"version":1,"tool_names":["read"]})).unwrap();
    let cases = [
        (
            "<function=read><parameter=path>file.txt</parameter></function>",
            "file.txt",
        ),
        ("[TOOL_CALLS]read{\"path\":\"a{b}.txt\"}", "a{b}.txt"),
        ("read[ARGS]{\"path\":\"c.txt\"}", "c.txt"),
        (
            "<think>{\"tool\":\"read\",\"args\":{\"path\":\"wrong\"}}</think>{\"tool\":\"read\",\"args\":{\"path\":\"right\"}}",
            "right",
        ),
    ];
    for (text, expected) in cases {
        let result = guard.check_text(text).unwrap();
        assert_eq!(result["outcome"], "execute", "{text}");
        assert_eq!(result["tool_calls"][0]["args"]["path"], expected);
    }
}

#[test]
fn tagged_tool_calls_cannot_be_redirected_by_nested_names_or_later_json() {
    let mut guard =
        Guardrails::new(serde_json::json!({"version":1,"tool_names":["read","write"]})).unwrap();
    let result = guard
        .check_text("[TOOL_CALLS]write{\"name\":\"read\"}")
        .unwrap();
    assert_eq!(result["tool_calls"][0]["tool"], "write");
    assert_eq!(
        guard
            .check_text("[TOOL_CALLS]write prose {\"path\":\"x\"}")
            .unwrap()["outcome"],
        "retry"
    );
}
