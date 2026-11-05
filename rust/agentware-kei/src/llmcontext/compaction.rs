//! File-history compaction (port of `go/llmcontext/compaction.go`).

use serde_json::Value;
use std::path::{Path, PathBuf};

/// Compaction tuning (Go `CompactionConfig`).
///
/// Go json-tags these fields (`threshold`, `keep_recent`,
/// `context_limit`, `summarize_via_llm`) for harness config files; the ported
/// surface does not serialize them, so the struct is plain.
#[derive(Debug, Clone, PartialEq)]
pub struct CompactionConfig {
    /// Trigger at this fraction of the context limit (default 0.75).
    pub threshold: f64,
    /// Number of recent rounds to keep (default 3).
    pub keep_recent: i64,
    /// Total context window size (default 100000).
    pub context_limit: i64,
    /// Use LLM for summarization (not implemented in Go either).
    pub summarize_via_llm: bool,
}

impl Default for CompactionConfig {
    /// Go `DefaultCompactionConfig`.
    fn default() -> Self {
        Self {
            threshold: 0.75,
            keep_recent: 3,
            context_limit: 100_000,
            summarize_via_llm: false,
        }
    }
}

/// Round counts from a compaction pass (Go `CompactionStats`; Go json-tags
/// these fields for harness reporting).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub struct CompactionStats {
    pub total_rounds: i64,
    pub recent_rounds: i64,
    pub compacted_rounds: i64,
    pub threshold_tokens: i64,
    pub estimated_tokens: i64,
    pub compaction_time_ms: i64,
}

/// A tool call recorded in `NNN-tool-calls.json` (Go `ToolCall` with
/// `json:"name"`/`json:"args"`; Go's decoder also accepts the untagged
/// `Name`/`Args` spelling case-insensitively).
#[derive(Debug, Clone, PartialEq, Default)]
pub struct ToolCall {
    pub name: String,
    pub args: Value,
}

/// Summarizes old rounds of a job's file history (Go `Compactor`).
pub struct Compactor {
    config: CompactionConfig,
    job_dir: PathBuf,
    /// Go carries `debugMode` (never read); kept for shape parity.
    #[allow(dead_code)]
    debug_mode: bool,
}

impl Compactor {
    /// Go `NewCompactor`: a zero threshold replaces the whole config with
    /// [`CompactionConfig::default`]; the other zero fields are not
    /// defaulted.
    pub fn new(job_dir: impl AsRef<Path>, config: CompactionConfig) -> Self {
        let config = if config.threshold == 0.0 {
            CompactionConfig::default()
        } else {
            config
        };
        Self {
            config,
            job_dir: job_dir.as_ref().to_path_buf(),
            debug_mode: false,
        }
    }

    /// Whether the estimated token count is at or over the compaction
    /// threshold (Go `ShouldCompact`). A directory read error counts as no.
    pub fn should_compact(&self) -> bool {
        let Ok(files) = self.get_prompt_files() else {
            return false;
        };
        if files.len() as i64 <= self.config.keep_recent {
            return false;
        }

        let threshold = (self.config.context_limit as f64 * self.config.threshold) as i64;
        let estimated = self.estimate_token_count();

        estimated >= threshold
    }

    /// Builds the summary of old rounds, keeping the most recent
    /// `keep_recent` prompt files as context (Go `Compact`). Empty output
    /// means nothing to compact.
    pub fn compact(&self) -> Result<String, String> {
        let files = self
            .get_prompt_files()
            .map_err(|error| format!("get prompt files: {error}"))?;

        if files.len() as i64 <= self.config.keep_recent {
            return Ok(String::new());
        }

        let keep = self.config.keep_recent as usize;
        let recent_files = &files[files.len() - keep..];
        let old_files = &files[..files.len() - keep];

        self.summarize_history(old_files, recent_files)
    }

    /// Round stats for the job directory (Go `GetStats`).
    pub fn get_stats(&self) -> CompactionStats {
        let total_rounds = self.get_prompt_files().unwrap_or_default().len() as i64;

        let mut recent_rounds = self.config.keep_recent;
        if recent_rounds > total_rounds {
            recent_rounds = total_rounds;
        }

        let mut compacted_rounds = total_rounds - recent_rounds;
        if compacted_rounds < 0 {
            compacted_rounds = 0;
        }

        let threshold_tokens = (self.config.context_limit as f64 * self.config.threshold) as i64;
        let estimated_tokens = self.estimate_token_count();

        CompactionStats {
            total_rounds,
            recent_rounds,
            compacted_rounds,
            threshold_tokens,
            estimated_tokens,
            compaction_time_ms: 0,
        }
    }

    /// Prompt file names (`NNN-prompt.txt`), sorted lexically (Go
    /// `getPromptFiles`).
    pub fn get_prompt_files(&self) -> Result<Vec<String>, String> {
        let entries = std::fs::read_dir(&self.job_dir).map_err(|error| error.to_string())?;

        let mut prompts = Vec::new();
        for entry in entries {
            let entry = entry.map_err(|error| error.to_string())?;
            if entry.path().is_dir() {
                continue;
            }
            let name = entry.file_name().to_string_lossy().to_string();
            if name.ends_with("-prompt.txt") {
                prompts.push(name);
            }
        }

        prompts.sort();
        Ok(prompts)
    }

    /// Builds the summary text (Go `summarizeHistory`), byte-for-byte:
    /// tool-call files are probed by OFFSET (`001`..`005`-tool-calls.json),
    /// not by round number, mirroring the Go implementation.
    fn summarize_history(
        &self,
        old_files: &[String],
        recent_files: &[String],
    ) -> Result<String, String> {
        let mut summary = String::new();

        if !old_files.is_empty() {
            summary.push_str(&format!(
                "Previous Work Summary ({} earlier rounds):\n\n",
                old_files.len()
            ));
        }

        for file in old_files {
            let round = file.strip_suffix("-prompt.txt").unwrap_or(file);

            let mut found_tool_calls = false;

            for offset in 1..=5i64 {
                let tool_calls_file = self.job_dir.join(format!("{offset:03}-tool-calls.json"));

                if let Ok(data) = std::fs::read(&tool_calls_file) {
                    if let Ok(calls) = serde_json::from_slice::<Value>(&data) {
                        let calls = parse_tool_calls(&calls);
                        if !calls.is_empty() {
                            found_tool_calls = true;
                            summary.push_str(&format!(
                                "- Round {round}: {} tool call(s)\n",
                                calls.len()
                            ));

                            for call in &calls {
                                summary.push_str(&format!("  - {}", call.name));

                                if let Some(args) = call.args.as_object() {
                                    if let Some(file) = args.get("file").and_then(Value::as_str) {
                                        summary.push_str(&format!(" (file: {file})"));
                                    }
                                    if let Some(pattern) =
                                        args.get("pattern").and_then(Value::as_str)
                                    {
                                        summary.push_str(&format!(" (pattern: {pattern})"));
                                    }
                                    if let Some(path) = args.get("path").and_then(Value::as_str) {
                                        summary.push_str(&format!(" (path: {path})"));
                                    }
                                    if let Some(command) =
                                        args.get("command").and_then(Value::as_str)
                                    {
                                        // Truncate long commands (byte length,
                                        // like Go's slice; UTF-8 boundary-safe).
                                        let mut command = command.to_owned();
                                        if command.len() > 50 {
                                            command =
                                                format!("{}...", truncate_bytes(&command, 50));
                                        }
                                        summary.push_str(&format!(" (cmd: {command})"));
                                    }
                                }
                                summary.push('\n');
                            }

                            // Check for modified files in results.
                            for result_offset in offset..=offset + 2 {
                                let tool_results_file = self
                                    .job_dir
                                    .join(format!("{result_offset:03}-tool-results.json"));
                                if let Ok(result_data) = std::fs::read(&tool_results_file) {
                                    if let Ok(results_value) =
                                        serde_json::from_slice::<Value>(&result_data)
                                    {
                                        for result in parse_tool_result_entries(&results_value) {
                                            if !result.output.is_empty()
                                                && result.output.contains("Modified:")
                                            {
                                                for line in result.output.split('\n') {
                                                    if line.starts_with("Modified:") {
                                                        summary.push_str(&format!(
                                                            "  {}\n",
                                                            line.trim()
                                                        ));
                                                    }
                                                }
                                            }
                                        }
                                    }
                                    break;
                                }
                            }
                            break;
                        }
                    }
                }
            }

            if !found_tool_calls {
                let response_file = self.job_dir.join(format!("{round}-response.txt"));
                if let Ok(data) = std::fs::read(&response_file) {
                    let mut response = String::from_utf8_lossy(&data).to_string();
                    if response.len() > 100 {
                        response = format!("{}...", truncate_bytes(&response, 100));
                    }
                    summary.push_str(&format!("- Round {round}: {response}\n"));
                }
            }
        }

        summary.push_str("\nRecent Context:\n");
        for file in recent_files {
            let round = file.strip_suffix("-prompt.txt").unwrap_or(file);
            summary.push_str(&format!("- {round}: (kept for context)\n"));
        }

        Ok(summary)
    }

    /// Rough token estimate over prompt and response files (Go
    /// `estimateTokenCount`): ~4 chars per token, read errors ignored.
    fn estimate_token_count(&self) -> i64 {
        let Ok(files) = self.get_prompt_files() else {
            return 0;
        };

        let mut total = 0i64;
        for file in &files {
            let path = self.job_dir.join(file);
            if let Ok(data) = std::fs::read(&path) {
                total += data.len() as i64 / 4;
            }

            let round = file.strip_suffix("-prompt.txt").unwrap_or(file);
            let response_path = self.job_dir.join(format!("{round}-response.txt"));
            if let Ok(data) = std::fs::read(&response_path) {
                total += data.len() as i64 / 4;
            }
        }

        total
    }
}

/// Byte truncation like Go's `s[:n]`, landing on the largest UTF-8 character
/// boundary at or before `n` bytes.
fn truncate_bytes(text: &str, max_bytes: usize) -> &str {
    if text.len() <= max_bytes {
        return text;
    }
    let mut end = max_bytes;
    while end > 0 && !text.is_char_boundary(end) {
        end -= 1;
    }
    &text[..end]
}

/// Parses a `tool-calls.json` array, accepting both the tagged
/// `name`/`args` keys and Go's case-insensitive `Name`/`Args` fallback.
fn parse_tool_calls(value: &Value) -> Vec<ToolCall> {
    let Some(items) = value.as_array() else {
        return Vec::new();
    };
    items
        .iter()
        .map(|item| ToolCall {
            name: string_field_any_case(item, &["name", "Name"]),
            args: item
                .get("args")
                .or_else(|| item.get("Args"))
                .cloned()
                .unwrap_or(Value::Null),
        })
        .collect()
}

/// Parses a `tool-results.json` array (Go `[]ToolResultEntry`, untagged, so
/// Go's decoder matches case-insensitively).
fn parse_tool_result_entries(value: &Value) -> Vec<super::manager::ToolResultEntry> {
    let Some(items) = value.as_array() else {
        return Vec::new();
    };
    items
        .iter()
        .map(|item| super::manager::ToolResultEntry {
            call_id: string_field_any_case(item, &["CallID", "call_id"]),
            tool_name: string_field_any_case(item, &["ToolName", "tool_name"]),
            args: item
                .get("Args")
                .or_else(|| item.get("args"))
                .and_then(Value::as_object)
                .cloned()
                .unwrap_or_default(),
            output: string_field_any_case(item, &["Output", "output"]),
            success: item
                .get("Success")
                .or_else(|| item.get("success"))
                .and_then(Value::as_bool)
                .unwrap_or(false),
        })
        .collect()
}

fn string_field_any_case(value: &Value, keys: &[&str]) -> String {
    for key in keys {
        if let Some(text) = value.get(*key).and_then(Value::as_str) {
            return text.to_owned();
        }
    }
    String::new()
}

#[cfg(test)]
mod tests {
    use super::*;

    fn temp_dir(label: &str) -> PathBuf {
        std::env::temp_dir().join(format!(
            "aw-llmcontext-compact-{label}-{}-{}",
            std::process::id(),
            crate::llmcontext::entry::now_nanos()
        ))
    }

    fn write(dir: &Path, name: &str, content: &str) {
        std::fs::write(dir.join(name), content).unwrap();
    }

    #[test]
    fn default_config() {
        let config = CompactionConfig::default();
        assert_eq!(config.threshold, 0.75);
        assert_eq!(config.keep_recent, 3);
        assert_eq!(config.context_limit, 100_000);
    }

    #[test]
    fn new_compactor_keeps_custom_config() {
        let dir = temp_dir("custom");
        let config = CompactionConfig {
            threshold: 0.5,
            keep_recent: 2,
            context_limit: 50_000,
            summarize_via_llm: false,
        };
        let compactor = Compactor::new(&dir, config);
        assert_eq!(compactor.config.threshold, 0.5);
        assert_eq!(compactor.config.keep_recent, 2);
    }

    #[test]
    fn new_compactor_defaults_on_zero_threshold() {
        let dir = temp_dir("zero");
        let compactor = Compactor::new(
            &dir,
            CompactionConfig {
                threshold: 0.0,
                keep_recent: 0,
                context_limit: 0,
                summarize_via_llm: false,
            },
        );
        assert_eq!(compactor.config.threshold, 0.75);
    }

    #[test]
    fn should_compact_empty() {
        let dir = temp_dir("sc-empty");
        std::fs::create_dir_all(&dir).unwrap();
        let compactor = Compactor::new(&dir, CompactionConfig::default());
        assert!(!compactor.should_compact(), "no files");
        std::fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn should_compact_under_threshold() {
        let dir = temp_dir("sc-under");
        std::fs::create_dir_all(&dir).unwrap();
        write(&dir, "001-prompt.txt", "test prompt");
        let compactor = Compactor::new(
            &dir,
            CompactionConfig {
                threshold: 0.75,
                keep_recent: 3,
                context_limit: 100_000,
                summarize_via_llm: false,
            },
        );
        assert!(!compactor.should_compact());
        std::fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn should_compact_over_threshold() {
        let dir = temp_dir("sc-over");
        std::fs::create_dir_all(&dir).unwrap();
        for i in 1..=10 {
            write(
                &dir,
                &format!("{i:03}-prompt.txt"),
                "test prompt with lots of content to exceed token threshold",
            );
            write(
                &dir,
                &format!("{i:03}-response.txt"),
                "test response with lots of content to exceed token threshold",
            );
        }
        let compactor = Compactor::new(
            &dir,
            CompactionConfig {
                threshold: 0.1,
                keep_recent: 3,
                context_limit: 1000,
                summarize_via_llm: false,
            },
        );
        assert!(compactor.should_compact());
        std::fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn compact_empty() {
        let dir = temp_dir("c-empty");
        std::fs::create_dir_all(&dir).unwrap();
        let compactor = Compactor::new(&dir, CompactionConfig::default());
        let result = compactor.compact().unwrap();
        assert_eq!(result, "");
        std::fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn compact_under_keep_recent() {
        let dir = temp_dir("c-under");
        std::fs::create_dir_all(&dir).unwrap();
        write(&dir, "001-prompt.txt", "test prompt");
        write(&dir, "002-prompt.txt", "test prompt 2");
        let compactor = Compactor::new(
            &dir,
            CompactionConfig {
                threshold: 0.0,
                keep_recent: 3,
                context_limit: 0,
                summarize_via_llm: false,
            },
        );
        // Threshold 0 means the config is replaced by defaults (Go quirk),
        // so keep_recent is 3 and there is nothing to compact.
        let result = compactor.compact().unwrap();
        assert_eq!(result, "");
        std::fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn compact_over_keep_recent() {
        let dir = temp_dir("c-over");
        std::fs::create_dir_all(&dir).unwrap();
        for i in 1..=5 {
            write(&dir, &format!("{i:03}-prompt.txt"), "test prompt");
            write(
                &dir,
                &format!("{i:03}-tool-calls.json"),
                r#"[{"Name": "bash", "Args": {"command": "ls"}}]"#,
            );
        }
        let compactor = Compactor::new(
            &dir,
            CompactionConfig {
                threshold: 0.5,
                keep_recent: 2,
                context_limit: 100_000,
                summarize_via_llm: false,
            },
        );
        let result = compactor.compact().unwrap();
        assert!(!result.is_empty(), "expected summary");
        // The summary names old rounds with their tool calls.
        assert!(result.contains("Previous Work Summary (3 earlier rounds):"));
        assert!(result.contains("- Round 001: 1 tool call(s)"));
        assert!(result.contains("  - bash"));
        assert!(result.contains("(cmd: ls)"));
        assert!(
            result.contains(
                "Recent Context:\n- 004: (kept for context)\n- 005: (kept for context)\n"
            )
        );
        std::fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn compact_uses_response_fallback_without_tool_calls() {
        let dir = temp_dir("c-fallback");
        std::fs::create_dir_all(&dir).unwrap();
        for i in 1..=4 {
            write(&dir, &format!("{i:03}-prompt.txt"), "test prompt");
        }
        write(&dir, "001-response.txt", "short response");
        let long_response = "x".repeat(150);
        write(&dir, "002-response.txt", &long_response);

        let compactor = Compactor::new(
            &dir,
            CompactionConfig {
                threshold: 0.5,
                keep_recent: 2,
                context_limit: 100_000,
                summarize_via_llm: false,
            },
        );
        let result = compactor.compact().unwrap();
        assert!(result.contains("- Round 001: short response\n"));
        // Responses over 100 chars are truncated with an ellipsis.
        let expected = format!("- Round 002: {}...\n", "x".repeat(100));
        assert!(
            result.contains(&expected),
            "long response truncated: {result}"
        );
        std::fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn compact_lists_modified_files() {
        let dir = temp_dir("c-modified");
        std::fs::create_dir_all(&dir).unwrap();
        for i in 1..=4 {
            write(&dir, &format!("{i:03}-prompt.txt"), "test prompt");
        }
        write(
            &dir,
            "001-tool-calls.json",
            r#"[{"name": "edit", "args": {"path": "a/b.go"}}]"#,
        );
        write(
            &dir,
            "001-tool-results.json",
            r#"[{"Output": "ok\nModified: a/b.go\n", "Success": true}]"#,
        );

        let compactor = Compactor::new(
            &dir,
            CompactionConfig {
                threshold: 0.5,
                keep_recent: 2,
                context_limit: 100_000,
                summarize_via_llm: false,
            },
        );
        let result = compactor.compact().unwrap();
        assert!(result.contains("  Modified: a/b.go\n"), "{result}");
        std::fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn get_stats_counts_rounds() {
        let dir = temp_dir("stats");
        std::fs::create_dir_all(&dir).unwrap();
        for i in 1..=5 {
            write(&dir, &format!("{i:03}-prompt.txt"), "test");
        }
        let compactor = Compactor::new(&dir, CompactionConfig::default());
        let stats = compactor.get_stats();
        assert_eq!(stats.total_rounds, 5);
        assert_eq!(stats.recent_rounds, 3);
        assert_eq!(stats.compacted_rounds, 2);
        std::fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn estimate_token_counts_prompts_and_responses() {
        let dir = temp_dir("estimate");
        std::fs::create_dir_all(&dir).unwrap();
        let content = "this is a test prompt with exactly twenty four characters";
        write(&dir, "001-prompt.txt", content);
        write(&dir, "001-response.txt", content);

        let compactor = Compactor::new(&dir, CompactionConfig::default());
        let estimated = compactor.estimate_token_count();
        assert_eq!(estimated, (content.len() / 4 * 2) as i64);
        std::fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn get_prompt_files_filters_and_sorts() {
        let dir = temp_dir("files");
        std::fs::create_dir_all(&dir).unwrap();
        write(&dir, "001-prompt.txt", "test");
        write(&dir, "002-prompt.txt", "test");
        write(&dir, "003-other.txt", "test");
        std::fs::create_dir_all(dir.join("004-prompt.txt.dir")).unwrap();

        let compactor = Compactor::new(&dir, CompactionConfig::default());
        let files = compactor.get_prompt_files().unwrap();
        assert_eq!(files, vec!["001-prompt.txt", "002-prompt.txt"]);
        std::fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn tool_calls_accept_both_key_spellings() {
        let dir = temp_dir("keys");
        std::fs::create_dir_all(&dir).unwrap();
        write(
            &dir,
            "001-tool-calls.json",
            r#"[{"Name": "bash", "Args": {"command": "git status --short"}}, {"name": "lower", "args": {"file": "x"}}]"#,
        );
        for i in 1..=4 {
            write(&dir, &format!("{i:03}-prompt.txt"), "test prompt");
        }
        let compactor = Compactor::new(
            &dir,
            CompactionConfig {
                threshold: 0.5,
                keep_recent: 2,
                context_limit: 100_000,
                summarize_via_llm: false,
            },
        );
        let result = compactor.compact().unwrap();
        assert!(
            result.contains("  - bash (cmd: git status --short)"),
            "{result}"
        );
        assert!(result.contains("  - lower (file: x)"), "{result}");
        std::fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn long_commands_are_truncated() {
        let dir = temp_dir("cmd");
        std::fs::create_dir_all(&dir).unwrap();
        let long_command = "c".repeat(80);
        write(
            &dir,
            "001-tool-calls.json",
            &format!(r#"[{{"name": "bash", "Args": {{"command": "{long_command}"}}}}]"#),
        );
        for i in 1..=4 {
            write(&dir, &format!("{i:03}-prompt.txt"), "test prompt");
        }
        let compactor = Compactor::new(
            &dir,
            CompactionConfig {
                threshold: 0.5,
                keep_recent: 2,
                context_limit: 100_000,
                summarize_via_llm: false,
            },
        );
        let result = compactor.compact().unwrap();
        let expected = format!("  - bash (cmd: {}...)\n", "c".repeat(50));
        assert!(result.contains(&expected), "{result}");
        std::fs::remove_dir_all(&dir).ok();
    }
}
