//! Conservative, caller-controlled compaction for OpenAI-style message history.
use serde_json::Value;

/// Truncate old tool-result text without dropping messages or changing call IDs.
///
/// This does not guarantee a hard token limit. The caller must enforce its own
/// context budget and decide what to do if preserving paired calls exceeds it.
pub fn compact_tool_results(
    messages: &[Value],
    keep_recent: usize,
    max_chars: usize,
) -> Result<(Vec<Value>, bool), &'static str> {
    if max_chars == 0 {
        return Err("max_chars must be positive");
    }
    let mut result = messages.to_vec();
    let eligible_end = result.len().saturating_sub(keep_recent);
    let mut changed = false;
    for message in result.iter_mut().take(eligible_end).skip(2) {
        if message.get("role").and_then(Value::as_str) != Some("tool") {
            continue;
        }
        let Some(content) = message.get("content").and_then(Value::as_str) else {
            continue;
        };
        if content.chars().count() <= max_chars || content.contains("[older tool result compacted]")
        {
            continue;
        }
        let prefix: String = content.chars().take(max_chars).collect();
        let compacted = format!("{prefix}\n[older tool result compacted]");
        let object = message.as_object_mut().ok_or("invalid tool message")?;
        object.insert("content".into(), Value::String(compacted));
        changed = true;
    }
    Ok((result, changed))
}
