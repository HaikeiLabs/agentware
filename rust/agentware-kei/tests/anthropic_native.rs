use haikeilabs_agentware::llm::{
    Message, Role, ToolDefinition, format_anthropic_messages, format_anthropic_tools,
    parse_anthropic_response,
};
use serde_json::{Value, json};

#[test]
fn anthropic_native_fixtures() {
    let cases: Value = serde_json::from_str(include_str!(
        "../../../fixtures/llm/anthropic-native/cases.json"
    ))
    .unwrap();
    for case in cases.as_array().unwrap() {
        let response = parse_anthropic_response(&case["response"]).unwrap();
        if case["name"] == "no thinking" {
            assert!(response.tool_calls[0].metadata.is_none());
        } else {
            assert!(response.tool_calls[0].metadata.is_some());
            assert!(!format!("{:?}", response.tool_calls).contains("signed+opaque"));
            assert!(!format!("{:?}", response.tool_calls).contains("opaque+encrypted"));
        }
        let tools: Vec<ToolDefinition> = case["tools"]
            .as_array()
            .unwrap()
            .iter()
            .map(|tool| ToolDefinition {
                name: tool["name"].as_str().unwrap().into(),
                description: tool["description"].as_str().unwrap().into(),
                input_schema: tool.get("input_schema").and_then(Value::as_object).cloned(),
            })
            .collect();
        assert_eq!(
            format_anthropic_tools(&tools),
            case["expected_tools"],
            "{}",
            case["name"]
        );
        let mut messages = vec![Message {
            role: Role::Assistant,
            content: response.content,
            tool_calls: response.tool_calls,
            ..Default::default()
        }];
        for call in messages[0].tool_calls.clone() {
            messages.push(Message {
                role: Role::Tool,
                content: "done".into(),
                tool_call_id: call.id,
                ..Default::default()
            });
        }
        assert_eq!(
            json!(format_anthropic_messages(&messages).unwrap()),
            case["expected_messages"],
            "{}",
            case["name"]
        );
    }
}
