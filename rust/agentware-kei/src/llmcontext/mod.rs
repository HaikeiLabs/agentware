//! Durable file-backed conversation history and round compaction (port of
//! `go/llmcontext`).

pub mod compaction;
pub mod entry;
pub mod manager;

pub use compaction::{CompactionConfig, CompactionStats, Compactor, ToolCall};
pub use entry::{
    ENTRY_PROMPT, ENTRY_RESPONSE, ENTRY_TOOL_CALL, ENTRY_TOOL_RESULT, Entry, EntryType,
    FileContextManager, format_rfc3339_nanos, now_nanos, parse_rfc3339,
};
pub use manager::{ContextManager, ToolResultEntry};
