//! Adapter limits (port of `go/reasoning/options.go`).

use std::time::SystemTime;

/// Supplies timestamps for trees and nodes. Defaults to [`SystemTime::now`].
pub type Clock = fn() -> SystemTime;

/// Bounds the reasoning adapter's behavior. All limits fail closed: an input
/// that exceeds a bound is rejected with an error rather than being truncated
/// into a partial tree (summaries are the one bounded field by design; they
/// are truncated, never the parse aborted).
#[derive(Debug, Clone, Copy)]
pub struct Options {
    /// Caps the size in bytes of the extracted reasoning text. Input whose
    /// reasoning exceeds this bound is rejected. Zero means the default
    /// (64 KiB).
    pub max_reasoning_bytes: usize,
    /// Caps the number of nodes a single tree may contain. Zero means the
    /// default (64).
    pub max_nodes: usize,
    /// Caps the depth of the node chain. Zero means the default (32).
    pub max_depth: usize,
    /// Caps the length of every node summary. Zero means the default (512).
    pub max_summary_chars: usize,
    /// Supplies timestamps for tree and nodes. `None` uses [`SystemTime::now`].
    pub clock: Option<Clock>,
}

impl Default for Options {
    fn default() -> Self {
        Self {
            max_reasoning_bytes: 64 * 1024,
            max_nodes: 64,
            max_depth: 32,
            max_summary_chars: 512,
            clock: None,
        }
    }
}

impl Options {
    /// Replaces zero-valued limits with the defaults (mirrors Go's
    /// `Options.normalize`; a zero bound would reject every input).
    pub(crate) fn normalize(mut self) -> Self {
        let defaults = Self::default();
        if self.max_reasoning_bytes == 0 {
            self.max_reasoning_bytes = defaults.max_reasoning_bytes;
        }
        if self.max_nodes == 0 {
            self.max_nodes = defaults.max_nodes;
        }
        if self.max_depth == 0 {
            self.max_depth = defaults.max_depth;
        }
        if self.max_summary_chars == 0 {
            self.max_summary_chars = defaults.max_summary_chars;
        }
        self
    }

    pub(crate) fn clock(&self) -> SystemTime {
        self.clock
            .map(|clock| clock())
            .unwrap_or_else(SystemTime::now)
    }
}
