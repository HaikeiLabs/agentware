//! Reasoning-text parsing into context-tree nodes (port of
//! `go/reasoning/parse.go`).

use crate::reasoning::ReasoningError;
use crate::reasoning::adapter::Adapter;
use crate::reasoning::types::{Node, NodeKind, NodeStatus};
use regex::Regex;
use sha2::{Digest, Sha256};
use std::fmt::Write;
use std::sync::LazyLock;
use std::time::SystemTime;

// Go's RE2 `\s`, `\S`, `\d` and `\b` are ASCII-only; Rust's are Unicode by
// default. The patterns below spell the ASCII classes out (and use the ASCII
// word boundary `(?-u:\b)`) so both ports classify identical inputs
// identically.

static PARAGRAPH_SEP: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"\n[ \t]*\n").expect("valid regex"));

static LIST_ORNAMENT: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r"^[\t\n\f\r ]*(?:[-*+•]|[0-9]+[.)])[\t\n\f\r ]+").expect("valid regex")
});

/// Matches an explicit kind label at the start of a paragraph.
static KIND_PREFIX: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(concat!(
        r"(?i)^[\t\n\f\r ]*(goal|observation|decision|tool-call|tool_call|",
        r"tool-result|tool_result|tool|conclusion|blocker)[\t\n\f\r ]*[:=][\t\n\f\r ]*",
    ))
    .expect("valid regex")
});

/// Matches a trailing status marker like `[BLOCKED]`.
static STATUS_MARKER: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r"(?i)[\t\n\f\r ]*\[(blocked|pending|active|superseded|complete)\][\t\n\f\r ]*$")
        .expect("valid regex")
});

static EVIDENCE_PATTERNS: LazyLock<Vec<Regex>> = LazyLock::new(|| {
    vec![
        Regex::new(r"\[E[0-9]+\]").expect("valid regex"),
        Regex::new(r"(?-u:\b)ref[\t\n\f\r ]*[=:][\t\n\f\r ]*([^\t\n\f\r ]+)").expect("valid regex"),
        Regex::new(r"(?-u:\b)evidence[\t\n\f\r ]*[=:][\t\n\f\r ]*([^\t\n\f\r ]+)")
            .expect("valid regex"),
    ]
});

static TOOL_REF_PATTERN: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r"(?-u:\b)(?:tool|call)[\t\n\f\r ]*[=:][\t\n\f\r ]*([a-zA-Z_][a-zA-Z0-9_.-]*)")
        .expect("valid regex")
});

/// Matches a leading `name:` / `name=` label in a tool node summary (e.g.
/// `refund_tool: {...}`).
static LEADING_IDENTIFIER: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r"^([a-zA-Z_][a-zA-Z0-9_.-]*)[\t\n\f\r ]*[:=]").expect("valid regex")
});

static BARE_IDENTIFIER: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"^[a-zA-Z_][a-zA-Z0-9_.-]*$").expect("valid regex"));

impl Adapter {
    /// Turns reasoning text into a chain of context-tree nodes. The parser is
    /// intentionally deterministic: paragraphs (blank-line separated) map to
    /// nodes, the first node is the root, and every subsequent node's parent
    /// is its predecessor. Nodes carry bounded summaries; raw reasoning never
    /// reaches the tree.
    ///
    /// Limits fail closed: too many nodes, too deep a chain, or reasoning
    /// larger than the configured cap abort the parse instead of emitting a
    /// partial tree.
    pub(crate) fn parse_nodes(
        &self,
        reasoning: &str,
        now: SystemTime,
    ) -> Result<Vec<Node>, ReasoningError> {
        if reasoning.len() > self.opts.max_reasoning_bytes {
            return Err(ReasoningError::ReasoningTooLarge);
        }

        let paragraphs = split_paragraphs(reasoning);
        if paragraphs.len() > self.opts.max_nodes {
            return Err(ReasoningError::TooManyNodes);
        }

        let mut nodes: Vec<Node> = Vec::with_capacity(paragraphs.len());
        let mut parent_id = String::new();
        let mut depth = 0usize;

        for para in paragraphs {
            depth += 1;
            if depth > self.opts.max_depth {
                return Err(ReasoningError::TooDeep);
            }

            let (kind, summary) = classify_paragraph(&para);
            let (status, summary) = status_from_summary(&summary, kind);
            let summary = truncate_runes(&summary, self.opts.max_summary_chars);
            if summary.is_empty() {
                // An empty node carries no reasoning; skip it rather than emit
                // a meaningless tree entry.
                depth -= 1;
                continue;
            }

            let id = node_id(kind, &parent_id, &summary);
            nodes.push(Node {
                id: id.clone(),
                parent_id: parent_id.clone(),
                kind,
                status,
                evidence_refs: extract_evidence(&summary),
                tool_call_refs: extract_tool_refs(kind, &summary),
                summary,
                created_at: now,
                updated_at: now,
            });
            parent_id = id;
        }
        Ok(nodes)
    }
}

/// Splits reasoning text on blank lines and drops empty paragraphs.
fn split_paragraphs(text: &str) -> Vec<String> {
    PARAGRAPH_SEP
        .split(text)
        .map(str::trim)
        .filter(|para| !para.is_empty())
        .map(str::to_owned)
        .collect()
}

/// Maps a matched prefix label to its node kind.
fn kind_from_label(label: &str) -> NodeKind {
    match label.to_lowercase().as_str() {
        "goal" => NodeKind::Goal,
        "observation" => NodeKind::Observation,
        "decision" => NodeKind::Decision,
        "tool-call" | "tool_call" | "tool" => NodeKind::ToolCall,
        "tool-result" | "tool_result" => NodeKind::ToolResult,
        "conclusion" => NodeKind::Conclusion,
        "blocker" => NodeKind::Blocker,
        _ => NodeKind::Observation,
    }
}

/// Strips list ornaments and an optional kind label, and returns the node
/// kind and the remaining summary text.
fn classify_paragraph(para: &str) -> (NodeKind, String) {
    let summary = LIST_ORNAMENT.replace_all(para, "");
    if let Some(captures) = KIND_PREFIX.captures(&summary) {
        let label = captures.get(1).expect("kind label group");
        let kind = kind_from_label(label.as_str());
        let rest = &summary[captures.get(0).expect("full match").end()..];
        return (kind, rest.trim().to_owned());
    }
    (NodeKind::Observation, summary.into_owned())
}

fn status_from_marker(label: &str) -> NodeStatus {
    match label.to_lowercase().as_str() {
        "blocked" => NodeStatus::Blocked,
        "pending" | "active" => NodeStatus::Active,
        "superseded" => NodeStatus::Superseded,
        "complete" => NodeStatus::Complete,
        _ => NodeStatus::Complete,
    }
}

/// Applies a trailing status marker and infers blocker status. The marker is
/// removed from the summary.
fn status_from_summary(summary: &str, kind: NodeKind) -> (NodeStatus, String) {
    if let Some(captures) = STATUS_MARKER.captures(summary) {
        let marker = captures.get(0).expect("full match");
        let label = captures.get(1).expect("status label group");
        return (
            status_from_marker(label.as_str()),
            summary[..marker.start()].trim().to_owned(),
        );
    }
    if kind == NodeKind::Blocker {
        return (NodeStatus::Blocked, summary.to_owned());
    }
    (NodeStatus::Complete, summary.to_owned())
}

/// Collects evidence references from a summary, in order, with duplicates
/// removed. Bracketed `[E1]`-style refs are stored without the brackets.
fn extract_evidence(summary: &str) -> Vec<String> {
    let mut out: Vec<String> = Vec::new();
    let mut seen = std::collections::HashSet::new();
    for pattern in EVIDENCE_PATTERNS.iter() {
        for captures in pattern.captures_iter(summary) {
            let full = captures.get(0).expect("full match");
            let reference = captures
                .get(1)
                .map_or(full.as_str(), |group| group.as_str())
                .trim_matches(|c| c == '[' || c == ']');
            if !reference.is_empty() && seen.insert(reference.to_owned()) {
                out.push(reference.to_owned());
            }
        }
    }
    out
}

/// Collects tool identifiers referenced by a tool-call or tool-result node:
/// first a leading `name:` label, then an explicit `tool=`/`call=` reference,
/// then a bare identifier summary.
fn extract_tool_refs(kind: NodeKind, summary: &str) -> Vec<String> {
    if !matches!(kind, NodeKind::ToolCall | NodeKind::ToolResult) {
        return Vec::new();
    }
    if let Some(captures) = LEADING_IDENTIFIER.captures(summary) {
        return vec![
            captures
                .get(1)
                .expect("identifier group")
                .as_str()
                .to_owned(),
        ];
    }
    if let Some(captures) = TOOL_REF_PATTERN.captures(summary) {
        return vec![
            captures
                .get(1)
                .expect("identifier group")
                .as_str()
                .to_owned(),
        ];
    }
    if BARE_IDENTIFIER.is_match(summary) {
        return vec![summary.to_owned()];
    }
    Vec::new()
}

/// Truncates `text` to at most `max` Unicode code points. It never splits a
/// code point.
fn truncate_runes(text: &str, max: usize) -> String {
    if max == 0 {
        return String::new();
    }
    text.chars().take(max).collect()
}

/// Derives a stable, deterministic node ID from a node's content and position
/// so identical reasoning produces identical trees across ports and runs.
fn node_id(kind: NodeKind, parent_id: &str, summary: &str) -> String {
    let mut hasher = Sha256::new();
    hasher.update(kind.as_str());
    hasher.update([0u8]);
    hasher.update(parent_id);
    hasher.update([0u8]);
    hasher.update(summary);
    let digest = hasher.finalize();
    let mut hex = String::with_capacity(digest.len() * 2);
    for byte in digest.iter() {
        let _ = write!(hex, "{byte:02x}");
    }
    hex[..16].to_owned()
}
