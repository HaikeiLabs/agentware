//! Context entries and the file-backed manager (port of
//! `go/llmcontext/entry.go`).
//!
//! Storage format: `<base_dir>/<job_id>/entries.json` holding a JSON array of
//! entries with Go's untagged field names (`JobID`, `Type`, `Content`,
//! `Metadata`, `Time`), `Type` as the iota integer and `Time` as RFC3339. Go
//! writes the local-offset form; this port writes UTC (`Z`) and parses both.

use super::manager::{ContextManager, ToolResultEntry};
use crate::llm::{Message, Role};
use crate::toolformat::ParsedToolCall;
use serde_json::{Map, Value, json};
use std::path::{Path, PathBuf};

/// The type of a context entry (Go `EntryType`).
///
/// Go declares it as `type EntryType int` with iota constants; the Rust
/// newtype keeps the same round-trip behavior for out-of-range values (they
/// deserialize, persist, and are skipped by `get_history`, exactly like Go's
/// untagged switch).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Default)]
pub struct EntryType(pub i64);

/// Go `EntryPrompt` (iota 0): an outbound prompt message.
pub const ENTRY_PROMPT: EntryType = EntryType(0);
/// Go `EntryResponse` (iota 1): an inbound LLM response.
pub const ENTRY_RESPONSE: EntryType = EntryType(1);
/// Go `EntryToolCall` (iota 2): a tool call made during execution.
pub const ENTRY_TOOL_CALL: EntryType = EntryType(2);
/// Go `EntryToolResult` (iota 3): a result from a tool execution.
pub const ENTRY_TOOL_RESULT: EntryType = EntryType(3);

/// A single entry in the context history (Go `Entry`).
///
/// `metadata` is Go's nilable `map[string]any`: `None` serializes as JSON
/// `null`. The Go zero value has `Type` iota 0 (`EntryPrompt`) and the zero
/// time; `Entry::default()` mirrors it.
#[derive(Debug, Clone, PartialEq, Default)]
pub struct Entry {
    pub job_id: String,
    pub entry_type: EntryType,
    pub content: String,
    pub metadata: Option<Map<String, Value>>,
    /// Nanoseconds since the Unix epoch (Go `time.Time`'s internal form;
    /// zero maps to Go's zero time in intent, not calendar value).
    pub time_nanos: i64,
}

/// File-based implementation of [`ContextManager`] (Go `FileContextManager`).
pub struct FileContextManager {
    base_dir: PathBuf,
}

impl FileContextManager {
    /// Go `NewFileContextManager` creates the directory eagerly; where Go
    /// panics on failure, the Rust constructor returns the error.
    pub fn new(base_dir: impl AsRef<Path>) -> Result<Self, String> {
        let base_dir = base_dir.as_ref().to_path_buf();
        std::fs::create_dir_all(&base_dir)
            .map_err(|error| format!("failed to create context directory: {error}"))?;
        Ok(Self { base_dir })
    }

    fn job_dir(&self, job_id: &str) -> PathBuf {
        self.base_dir.join(job_id)
    }

    fn entries_file(&self, job_id: &str) -> PathBuf {
        self.job_dir(job_id).join("entries.json")
    }

    fn append_entry(&self, job_id: &str, mut entry: Entry) -> Result<(), String> {
        std::fs::create_dir_all(self.job_dir(job_id))
            .map_err(|error| format!("failed to create job directory: {error}"))?;

        let mut entries = self.read_entries(job_id)?;
        entry.job_id = job_id.to_owned();
        entries.push(entry);

        let data = serialize_entries(&entries);
        write_file(&self.entries_file(job_id), &data)
    }

    fn read_entries(&self, job_id: &str) -> Result<Vec<Entry>, String> {
        let file = self.entries_file(job_id);
        let data = match std::fs::read(&file) {
            Ok(data) => data,
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
                return Ok(Vec::new());
            }
            Err(error) => return Err(format!("failed to read entries: {error}")),
        };

        let value: Value = serde_json::from_slice(&data)
            .map_err(|error| format!("failed to unmarshal entries: {error}"))?;
        deserialize_entries(&value)
    }
}

/// Writes a file with Go's 0644 permissions.
fn write_file(path: &Path, data: &[u8]) -> Result<(), String> {
    use std::io::Write;
    use std::os::unix::fs::OpenOptionsExt;
    let mut file = std::fs::OpenOptions::new()
        .write(true)
        .create(true)
        .truncate(true)
        .mode(0o644)
        .open(path)
        .map_err(|error| format!("write {}: {error}", path.display()))?;
    file.write_all(data)
        .map_err(|error| format!("write {}: {error}", path.display()))
}

/// Marshals entries with Go's untagged field names.
fn serialize_entries(entries: &[Entry]) -> Vec<u8> {
    let value = Value::Array(
        entries
            .iter()
            .map(|entry| {
                json!({
                    "JobID": entry.job_id,
                    "Type": entry.entry_type.0,
                    "Content": entry.content,
                    "Metadata": entry
                        .metadata
                        .as_ref()
                        .map(|metadata| Value::Object(metadata.clone()))
                        .unwrap_or(Value::Null),
                    "Time": format_rfc3339_nanos(entry.time_nanos),
                })
            })
            .collect(),
    );
    serde_json::to_vec(&value).expect("entry JSON serialization cannot fail")
}

/// Unmarshals entries. Like Go's decode into `EntryType int`, any integer
/// type value is accepted; unknown values are skipped by `get_history`'s
/// type switch but preserved on rewrite.
fn deserialize_entries(value: &Value) -> Result<Vec<Entry>, String> {
    let Some(items) = value.as_array() else {
        return Err("failed to unmarshal entries: expected a JSON array".into());
    };
    let mut entries = Vec::with_capacity(items.len());
    for item in items {
        let time = item
            .get("Time")
            .and_then(Value::as_str)
            .map(parse_rfc3339)
            .transpose()?
            .unwrap_or(0);
        entries.push(Entry {
            job_id: item
                .get("JobID")
                .and_then(Value::as_str)
                .unwrap_or_default()
                .to_owned(),
            entry_type: EntryType(item.get("Type").and_then(Value::as_i64).unwrap_or(0)),
            content: item
                .get("Content")
                .and_then(Value::as_str)
                .unwrap_or_default()
                .to_owned(),
            metadata: item.get("Metadata").and_then(Value::as_object).cloned(),
            time_nanos: time,
        });
    }
    Ok(entries)
}

impl ContextManager for FileContextManager {
    fn append_prompt(&self, job_id: &str, message: &Message) -> Result<(), String> {
        self.append_entry(
            job_id,
            Entry {
                entry_type: ENTRY_PROMPT,
                content: message.content.clone(),
                time_nanos: now_nanos(),
                ..Default::default()
            },
        )
    }

    fn append_response(&self, job_id: &str, message: &Message) -> Result<(), String> {
        self.append_entry(
            job_id,
            Entry {
                entry_type: ENTRY_RESPONSE,
                content: message.content.clone(),
                time_nanos: now_nanos(),
                ..Default::default()
            },
        )
    }

    fn append_tool_calls(&self, job_id: &str, calls: &[ParsedToolCall]) -> Result<(), String> {
        for call in calls {
            let mut metadata = Map::new();
            metadata.insert("args".into(), Value::Object(call.args.clone()));
            if !call.id.is_empty() {
                metadata.insert("id".into(), json!(call.id));
            }
            self.append_entry(
                job_id,
                Entry {
                    entry_type: ENTRY_TOOL_CALL,
                    content: call.name.clone(),
                    metadata: Some(metadata),
                    time_nanos: now_nanos(),
                    ..Default::default()
                },
            )?;
        }
        Ok(())
    }

    fn append_tool_results(&self, job_id: &str, results: &[ToolResultEntry]) -> Result<(), String> {
        for result in results {
            let mut metadata = Map::new();
            metadata.insert("tool_name".into(), json!(result.tool_name));
            metadata.insert("success".into(), json!(result.success));
            if !result.call_id.is_empty() {
                metadata.insert("call_id".into(), json!(result.call_id));
            }
            self.append_entry(
                job_id,
                Entry {
                    entry_type: ENTRY_TOOL_RESULT,
                    content: result.output.clone(),
                    metadata: Some(metadata),
                    time_nanos: now_nanos(),
                    ..Default::default()
                },
            )?;
        }
        Ok(())
    }

    /// Reconstructs the full message history: prompts map to `user`,
    /// responses to `assistant`, tool results to `tool`; tool-call entries
    /// (and unknown types) are skipped. Entries are sorted by time (Rust's
    /// sort is stable where Go's `sort.Slice` is not; equal timestamps keep
    /// write order).
    fn get_history(&self, job_id: &str) -> Result<Vec<Message>, String> {
        let mut entries = self.read_entries(job_id)?;
        entries.sort_by_key(|entry| entry.time_nanos);

        let mut messages = Vec::with_capacity(entries.len());
        for entry in entries {
            let role = if entry.entry_type == ENTRY_PROMPT {
                Role::User
            } else if entry.entry_type == ENTRY_RESPONSE {
                Role::Assistant
            } else if entry.entry_type == ENTRY_TOOL_RESULT {
                Role::Tool
            } else {
                continue;
            };
            messages.push(Message {
                role,
                content: entry.content,
                ..Default::default()
            });
        }
        Ok(messages)
    }

    /// Deletes all context files for a job.
    fn purge(&self, job_id: &str) -> Result<(), String> {
        let dir = self.job_dir(job_id);
        match std::fs::remove_dir_all(&dir) {
            Ok(()) => Ok(()),
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(()),
            Err(error) => Err(format!("remove {}: {error}", dir.display())),
        }
    }
}

/// Nanoseconds since the Unix epoch, now (Go `time.Now()`).
pub fn now_nanos() -> i64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|duration| duration.as_nanos() as i64)
        .unwrap_or(0)
}

/// Formats epoch nanoseconds as RFC3339 with nanosecond precision, trailing
/// zeros trimmed, UTC `Z` suffix (Go `time.Time.MarshalJSON` shape).
pub fn format_rfc3339_nanos(nanos: i64) -> String {
    let seconds = nanos.div_euclid(1_000_000_000);
    let subsec = nanos.rem_euclid(1_000_000_000);
    let days = seconds.div_euclid(86_400);
    let secs_of_day = seconds.rem_euclid(86_400);
    let (year, month, day) = civil_from_days(days);
    let hour = secs_of_day / 3_600;
    let minute = (secs_of_day % 3_600) / 60;
    let second = secs_of_day % 60;

    let mut out = format!("{year:04}-{month:02}-{day:02}T{hour:02}:{minute:02}:{second:02}");
    if subsec > 0 {
        let mut fraction = format!("{subsec:09}");
        while fraction.ends_with('0') {
            fraction.pop();
        }
        out.push('.');
        out.push_str(&fraction);
    }
    out.push('Z');
    out
}

/// Parses an RFC3339 timestamp (`Z` or numeric offset) to epoch nanoseconds.
pub fn parse_rfc3339(text: &str) -> Result<i64, String> {
    let bytes = text.as_bytes();
    if bytes.len() < 20 {
        return Err(format!("invalid RFC3339 timestamp: {text:?}"));
    }
    let year: i64 = text[0..4].parse().map_err(|_| bad(text))?;
    if bytes[4] != b'-' {
        return Err(bad(text));
    }
    let month: i64 = text[5..7].parse().map_err(|_| bad(text))?;
    if bytes[7] != b'-' {
        return Err(bad(text));
    }
    let day: i64 = text[8..10].parse().map_err(|_| bad(text))?;
    if bytes[10] != b'T' && bytes[10] != b't' {
        return Err(bad(text));
    }
    let hour: i64 = text[11..13].parse().map_err(|_| bad(text))?;
    if bytes[13] != b':' {
        return Err(bad(text));
    }
    let minute: i64 = text[14..16].parse().map_err(|_| bad(text))?;
    if bytes[16] != b':' {
        return Err(bad(text));
    }
    let second: i64 = text[17..19].parse().map_err(|_| bad(text))?;

    let mut rest = &text[19..];
    let mut subsec = 0i64;
    if let Some(fraction_text) = rest.strip_prefix('.') {
        let digits: String = fraction_text
            .chars()
            .take_while(|c| c.is_ascii_digit())
            .collect();
        if digits.is_empty() {
            return Err(bad(text));
        }
        rest = &rest[1 + digits.len()..];
        let mut scaled = digits.clone();
        if scaled.len() > 9 {
            scaled.truncate(9);
        }
        while scaled.len() < 9 {
            scaled.push('0');
        }
        subsec = scaled.parse().map_err(|_| bad(text))?;
    }

    let offset_seconds = match rest {
        "Z" | "z" => 0,
        _ => {
            let sign = match rest.as_bytes().first() {
                Some(b'+') => 1,
                Some(b'-') => -1,
                _ => return Err(bad(text)),
            };
            let offset = &rest[1..];
            if offset.len() != 5 || offset.as_bytes()[2] != b':' {
                return Err(bad(text));
            }
            let offset_hour: i64 = offset[0..2].parse().map_err(|_| bad(text))?;
            let offset_minute: i64 = offset[3..5].parse().map_err(|_| bad(text))?;
            sign * (offset_hour * 3_600 + offset_minute * 60)
        }
    };

    let days = days_from_civil(year, month, day);
    let seconds = days * 86_400 + hour * 3_600 + minute * 60 + second - offset_seconds;
    Ok(seconds * 1_000_000_000 + subsec)
}

fn bad(text: &str) -> String {
    format!("invalid RFC3339 timestamp: {text:?}")
}

/// Days since 1970-01-01 from a civil date (Howard Hinnant's algorithm).
fn days_from_civil(year: i64, month: i64, day: i64) -> i64 {
    let year = year - if month <= 2 { 1 } else { 0 };
    let era = if year >= 0 { year } else { year - 399 } / 400;
    let year_of_era = year - era * 400;
    let day_of_year = (153 * (month + if month > 2 { -3 } else { 9 }) + 2) / 5 + day - 1;
    let day_of_era = year_of_era * 365 + year_of_era / 4 - year_of_era / 100 + day_of_year;
    era * 146_097 + day_of_era - 719_468
}

/// Civil date from days since 1970-01-01 (Howard Hinnant's algorithm).
fn civil_from_days(days: i64) -> (i64, i64, i64) {
    let z = days + 719_468;
    let era = if z >= 0 { z } else { z - 146_096 } / 146_097;
    let day_of_era = z - era * 146_097;
    let year_of_era =
        (day_of_era - day_of_era / 1_460 + day_of_era / 36_524 - day_of_era / 146_096) / 365;
    let year = year_of_era + era * 400;
    let day_of_year = day_of_era - (365 * year_of_era + year_of_era / 4 - year_of_era / 100);
    let mp = (5 * day_of_year + 2) / 153;
    let day = day_of_year - (153 * mp + 2) / 5 + 1;
    let month = mp + if mp < 10 { 3 } else { -9 };
    (year + if month <= 2 { 1 } else { 0 }, month, day)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn round_trips_rfc3339() {
        let parsed = parse_rfc3339("2026-11-04T12:34:56.123456789Z").unwrap();
        assert_eq!(
            format_rfc3339_nanos(parsed),
            "2026-11-04T12:34:56.123456789Z"
        );

        let whole = parse_rfc3339("2026-11-04T12:34:56Z").unwrap();
        assert_eq!(format_rfc3339_nanos(whole), "2026-11-04T12:34:56Z");

        // Go writes local offsets; parse them to the same instant.
        let offset = parse_rfc3339("2026-11-04T13:34:56+01:00").unwrap();
        assert_eq!(offset, whole);

        let truncated = parse_rfc3339("2026-11-04T12:34:56.123Z").unwrap();
        assert_eq!(format_rfc3339_nanos(truncated), "2026-11-04T12:34:56.123Z");
    }

    #[test]
    fn rejects_bad_timestamps() {
        assert!(parse_rfc3339("not-a-time").is_err());
        assert!(parse_rfc3339("2026-11-04").is_err());
    }

    #[test]
    fn known_epoch_values() {
        // 1970-01-01T00:00:00Z is epoch 0.
        assert_eq!(parse_rfc3339("1970-01-01T00:00:00Z").unwrap(), 0);
        let days = days_from_civil(2026, 11, 4);
        assert_eq!(
            days * 86_400,
            parse_rfc3339("2026-11-04T00:00:00Z").unwrap() / 1_000_000_000
        );
    }

    #[test]
    fn file_context_manager_flow() {
        let temp_dir = temp_dir("flow");
        let manager = FileContextManager::new(&temp_dir).expect("manager");

        let job_id = "test-job-123";

        manager
            .append_prompt(
                job_id,
                &Message {
                    role: Role::User,
                    content: "Hello, world".into(),
                    ..Default::default()
                },
            )
            .expect("AppendPrompt");

        manager
            .append_response(
                job_id,
                &Message {
                    role: Role::Assistant,
                    content: "Hi there!".into(),
                    ..Default::default()
                },
            )
            .expect("AppendResponse");

        let mut args = Map::new();
        args.insert("arg1".into(), json!("value1"));
        manager
            .append_tool_calls(
                job_id,
                &[ParsedToolCall {
                    id: "call-1".into(),
                    name: "test_tool".into(),
                    args,
                    raw: String::new(),
                }],
            )
            .expect("AppendToolCalls");

        manager
            .append_tool_results(
                job_id,
                &[ToolResultEntry {
                    call_id: "call-1".into(),
                    tool_name: "test_tool".into(),
                    args: Map::new(),
                    output: "success".into(),
                    success: true,
                }],
            )
            .expect("AppendToolResults");

        let history = manager.get_history(job_id).expect("GetHistory");
        assert_eq!(history.len(), 3, "tool-call entries are skipped");
        assert_eq!(history[0].role, Role::User);
        assert_eq!(history[0].content, "Hello, world");

        // The persisted file uses Go's untagged field names and iota types.
        let raw = std::fs::read_to_string(temp_dir.join(job_id).join("entries.json"))
            .expect("entries file");
        assert!(raw.contains("\"Type\":2"), "tool call entry: {raw}");
        assert!(raw.contains("\"Metadata\":{\"args\":{\"arg1\":\"value1\"},\"id\":\"call-1\"}"));

        manager.purge(job_id).expect("Purge");
        assert!(!temp_dir.join(job_id).exists(), "job directory purged");

        std::fs::remove_dir_all(&temp_dir).ok();
    }

    #[test]
    fn get_history_empty_job() {
        let temp_dir = temp_dir("empty");
        let manager = FileContextManager::new(&temp_dir).expect("manager");
        let history = manager.get_history("nonexistent-job").expect("GetHistory");
        assert!(history.is_empty());
        std::fs::remove_dir_all(&temp_dir).ok();
    }

    #[test]
    fn history_sorts_by_time_not_write_order() {
        let temp_dir = temp_dir("sort");
        let manager = FileContextManager::new(&temp_dir).expect("manager");

        let job_id = "sort-job";
        manager
            .append_prompt(job_id, &Message::default())
            .expect("first");
        manager
            .append_response(job_id, &Message::default())
            .expect("second");

        // Rewrite with swapped times to prove the sort, not write order.
        let entries_path = temp_dir.join(job_id).join("entries.json");
        let raw = std::fs::read_to_string(&entries_path).unwrap();
        let mut value: Value = serde_json::from_str(&raw).unwrap();
        let items = value.as_array_mut().unwrap();
        let first_time = items[0]["Time"].as_str().unwrap().to_owned();
        items[0]["Time"] = items[1]["Time"].clone();
        items[1]["Time"] = json!(first_time);
        std::fs::write(&entries_path, serde_json::to_vec(&value).unwrap()).unwrap();

        let history = manager.get_history(job_id).unwrap();
        assert_eq!(history.len(), 2);
        assert_eq!(history[0].role, Role::Assistant);
        assert_eq!(history[1].role, Role::User);

        std::fs::remove_dir_all(&temp_dir).ok();
    }

    #[test]
    fn corrupt_entries_file_is_an_error() {
        let temp_dir = temp_dir("corrupt");
        let manager = FileContextManager::new(&temp_dir).expect("manager");
        std::fs::create_dir_all(temp_dir.join("job")).unwrap();
        std::fs::write(temp_dir.join("job").join("entries.json"), b"{not json").unwrap();
        assert!(manager.get_history("job").is_err());
        std::fs::remove_dir_all(&temp_dir).ok();
    }

    #[test]
    fn unknown_entry_type_round_trips_and_is_skipped() {
        let temp_dir = temp_dir("unknown");
        let manager = FileContextManager::new(&temp_dir).expect("manager");
        let job_id = "u";
        manager
            .append_prompt(job_id, &Message::default())
            .expect("prompt");
        // Corrupt the type to an out-of-range value, like Go's int type would
        // carry.
        let entries_path = temp_dir.join(job_id).join("entries.json");
        let raw = std::fs::read_to_string(&entries_path).unwrap();
        let patched = raw.replace("\"Type\":0", "\"Type\":9");
        std::fs::write(&entries_path, patched).unwrap();

        // Go: the entry survives a rewrite and is skipped in history.
        manager
            .append_response(job_id, &Message::default())
            .expect("response");
        let raw = std::fs::read_to_string(&entries_path).unwrap();
        assert!(raw.contains("\"Type\":9"), "unknown type preserved: {raw}");
        let history = manager.get_history(job_id).unwrap();
        assert_eq!(history.len(), 1, "unknown type skipped");
        assert_eq!(history[0].role, Role::Assistant);

        std::fs::remove_dir_all(&temp_dir).ok();
    }

    fn temp_dir(label: &str) -> PathBuf {
        std::env::temp_dir().join(format!(
            "aw-llmcontext-{label}-{}-{}",
            std::process::id(),
            now_nanos()
        ))
    }
}
