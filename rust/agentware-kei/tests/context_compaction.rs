use agentware_kei::context::compact_tool_results;
use serde_json::json;

#[test]
fn keeps_task_and_call_result_pairing() {
    let history = vec![
        json!({"role":"system","content":"system"}),
        json!({"role":"user","content":"task"}),
        json!({"role":"assistant","tool_calls":[{"id":"c1"}]}),
        json!({"role":"tool","tool_call_id":"c1","content":"a very long tool result"}),
        json!({"role":"assistant","content":"recent"}),
    ];
    let (compacted, changed) = compact_tool_results(&history, 1, 4).unwrap();
    assert!(changed);
    assert_eq!(compacted[0], history[0]);
    assert_eq!(compacted[1], history[1]);
    assert_eq!(compacted[2], history[2]);
    assert_eq!(compacted[3]["tool_call_id"], "c1");
    assert_eq!(compacted[4], history[4]);
    assert!(
        compacted[3]["content"]
            .as_str()
            .unwrap()
            .starts_with("a ve")
    );
    assert!(!compact_tool_results(&compacted, 1, 4).unwrap().1);
}
