//! Reasoning-tag constants shared by the reasoning adapter and the response
//! validator (`guardrails`), so both recognize the same tags.
//!
//! Every tag is built from parts and is never typed as one literal. The tags
//! are a blind spot for reasoning-model workers: a model that emits these tags
//! itself cannot reliably read or type them (it writes a different tag, or the
//! tag comes out garbled), so a literal tag in source is an edit waiting to be
//! corrupted. Keep it that way: never write a reasoning tag literally in
//! source, tests or docs; build it from parts or use these constants.
//!
//! Mirrors `go/reasoning/tags.go`.

/// Opens the plain think block that Qwen3, QwQ, DeepSeek-R1 and GLM emit
/// when served without a reasoning parser.
pub const THINK_OPEN: &str = concat!("<", "think", ">");
/// Closes a [`THINK_OPEN`] block.
pub const THINK_CLOSE: &str = concat!("<", "/", "think", ">");

/// Opens the mm:think block that MiniMax M3 emits inline in content.
pub const MMTHINK_OPEN: &str = concat!("<", "mm:think", ">");
/// Closes a [`MMTHINK_OPEN`] block.
pub const MMTHINK_CLOSE: &str = concat!("<", "/", "mm:think", ">");

/// Opens the generic thinking block.
pub const THINKING_OPEN: &str = concat!("<", "think", "ing", ">");
/// Closes a [`THINKING_OPEN`] block.
pub const THINKING_CLOSE: &str = concat!("<", "/", "think", "ing", ">");

/// Opens the bracketed THINK block (Mistral/Magistral).
pub const BRACKET_THINK_OPEN: &str = concat!("[", "THINK", "]");
/// Closes a [`BRACKET_THINK_OPEN`] block.
pub const BRACKET_THINK_CLOSE: &str = concat!("[", "/", "THINK", "]");

/// One open/close reasoning-tag pair. Matching is case-insensitive.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct TagPair {
    pub open: &'static str,
    pub close: &'static str,
}

/// Every reasoning-tag pair the adapter and the validator recognize, in a
/// fixed order: think, mm:think, thinking, bracket THINK.
pub const REASONING_TAG_PAIRS: [TagPair; 4] = [
    TagPair {
        open: THINK_OPEN,
        close: THINK_CLOSE,
    },
    TagPair {
        open: MMTHINK_OPEN,
        close: MMTHINK_CLOSE,
    },
    TagPair {
        open: THINKING_OPEN,
        close: THINKING_CLOSE,
    },
    TagPair {
        open: BRACKET_THINK_OPEN,
        close: BRACKET_THINK_CLOSE,
    },
];
