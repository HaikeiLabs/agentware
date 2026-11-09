//! Native Anthropic content-block formatting. Signed blocks are opaque.

use super::{Message, Response, Role, ToolCall, ToolDefinition};
use serde_json::{Value, json};

/// Parse native content blocks and retain blocks surrounding each tool use.
pub fn parse_anthropic_response(data: &Value) -> Result<Response, String> {
    let mut response = Response {
        finish_reason: data
            .get("stop_reason")
            .and_then(Value::as_str)
            .unwrap_or_default()
            .into(),
        ..Default::default()
    };
    let mut pending: Vec<Value> = Vec::new();
    if let Some(blocks) = data.get("content").and_then(Value::as_array) {
        for block in blocks {
            if block.get("type").and_then(Value::as_str) == Some("tool_use") {
                let id = block
                    .get("id")
                    .and_then(Value::as_str)
                    .filter(|s| !s.is_empty())
                    .ok_or("invalid Anthropic tool_use")?;
                let name = block
                    .get("name")
                    .and_then(Value::as_str)
                    .filter(|s| !s.is_empty())
                    .ok_or("invalid Anthropic tool_use")?;
                let args = block
                    .get("input")
                    .and_then(Value::as_object)
                    .ok_or("invalid Anthropic tool_use")?
                    .clone();
                let metadata = if pending.is_empty() {
                    None
                } else {
                    Some(json!({"anthropic": {"before": pending, "after": []}}))
                };
                response.tool_calls.push(ToolCall {
                    id: id.into(),
                    name: name.into(),
                    args,
                    metadata,
                });
                pending = Vec::new();
            } else {
                if block.get("type").and_then(Value::as_str) == Some("text") {
                    if let Some(text) = block.get("text").and_then(Value::as_str) {
                        response.content.push_str(text);
                    }
                }
                pending.push(block.clone());
            }
        }
    }
    if !pending.is_empty() {
        if let Some(last) = response.tool_calls.last_mut() {
            let metadata = last
                .metadata
                .get_or_insert_with(|| json!({"anthropic": {"before": [], "after": []}}));
            metadata["anthropic"]["after"] = Value::Array(pending);
        }
    }
    Ok(response)
}

/// Format Anthropic tool definitions.
pub fn format_anthropic_tools(tools: &[ToolDefinition]) -> Value {
    let definitions = tools
        .iter()
        .map(|tool| {
            let schema = tool
                .input_schema
                .as_ref()
                .filter(|s| !s.is_empty())
                .cloned()
                .unwrap_or_else(|| {
                    json!({"type":"object","properties":{}})
                        .as_object()
                        .unwrap()
                        .clone()
                });
            json!({"name": tool.name, "description": tool.description, "input_schema": schema})
        })
        .collect();
    Value::Array(definitions)
}

/// Format native assistant calls and group adjacent tool results in user turns.
/// System instructions belong in the top-level `system` parameter.
pub fn format_anthropic_messages(messages: &[Message]) -> Result<Vec<Value>, String> {
    let mut out: Vec<Value> = Vec::new();
    for message in messages {
        match message.role {
            Role::System => return Err("Anthropic system instructions must be top-level".into()),
            Role::Tool => {
                if message.tool_call_id.is_empty() {
                    return Err("tool_result requires tool_call_id".into());
                }
                let block = json!({"type":"tool_result","tool_use_id":message.tool_call_id,"content":message.content});
                let previous = out.last_mut().filter(|v| {
                    v.get("role").and_then(Value::as_str) == Some("user")
                        && v.get("content")
                            .and_then(Value::as_array)
                            .is_some_and(|items| {
                                items.iter().all(|b| {
                                    b.get("type").and_then(Value::as_str) == Some("tool_result")
                                })
                            })
                });
                if let Some(previous) = previous {
                    previous["content"].as_array_mut().unwrap().push(block);
                } else {
                    out.push(json!({"role":"user","content":[block]}));
                }
            }
            Role::Assistant if !message.tool_calls.is_empty() => {
                let mut blocks = Vec::new();
                let has_saved = message.tool_calls.iter().any(|call| {
                    call.metadata
                        .as_ref()
                        .and_then(|m| m.get("anthropic"))
                        .is_some()
                });
                if !has_saved && !message.content.is_empty() {
                    blocks.push(json!({"type":"text","text":message.content}));
                }
                for call in &message.tool_calls {
                    let saved = call.metadata.as_ref().and_then(|m| m.get("anthropic"));
                    if let Some(saved) = saved {
                        let before = saved
                            .get("before")
                            .and_then(Value::as_array)
                            .ok_or("invalid Anthropic metadata")?;
                        blocks.extend(before.iter().cloned());
                    }
                    blocks.push(
                        json!({"type":"tool_use","id":call.id,"name":call.name,"input":call.args}),
                    );
                    if let Some(saved) = saved {
                        let after = saved
                            .get("after")
                            .and_then(Value::as_array)
                            .ok_or("invalid Anthropic metadata")?;
                        blocks.extend(after.iter().cloned());
                    }
                }
                out.push(json!({"role":"assistant","content":blocks}));
            }
            _ => out.push(json!({"role":message.role.as_str(),"content":message.content})),
        }
    }
    Ok(out)
}
