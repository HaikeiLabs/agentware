//! Reasoning context-tree contract types (port of `go/reasoning/types.go`).

use serde_json::{Map, Value, json};
use std::time::{SystemTime, UNIX_EPOCH};

/// Version of the context-tree contract this module emits. Bump it only when
/// the wire shape of [`ContextTree`]/[`Node`] changes.
pub const CONTRACT_VERSION: &str = "1";

/// Shape of the input that produced a [`ContextTree`].
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Format {
    /// An OpenAI-compatible response carrying a native reasoning field.
    Native,
    /// Text with embedded generic thinking tags.
    Thinking,
    /// A response carrying a model-specific reasoning field registered with
    /// [`Adapter::register_model_field`](crate::reasoning::Adapter::register_model_field).
    ModelSpecific,
    /// No reasoning was detected.
    None,
}

impl Format {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Native => "native",
            Self::Thinking => "thinking",
            Self::ModelSpecific => "model_specific",
            Self::None => "none",
        }
    }
}

/// Closed vocabulary of context-tree node kinds.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum NodeKind {
    Goal,
    Observation,
    Decision,
    ToolCall,
    ToolResult,
    Conclusion,
    Blocker,
}

impl NodeKind {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Goal => "goal",
            Self::Observation => "observation",
            Self::Decision => "decision",
            Self::ToolCall => "tool-call",
            Self::ToolResult => "tool-result",
            Self::Conclusion => "conclusion",
            Self::Blocker => "blocker",
        }
    }
}

/// Closed vocabulary of context-tree node statuses.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum NodeStatus {
    Active,
    Complete,
    Blocked,
    Superseded,
}

impl NodeStatus {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Active => "active",
            Self::Complete => "complete",
            Self::Blocked => "blocked",
            Self::Superseded => "superseded",
        }
    }
}

/// A single node in the reasoning context tree. Nodes carry bounded summaries
/// only; raw reasoning text never appears on a node.
#[derive(Debug, Clone, PartialEq)]
pub struct Node {
    pub id: String,
    pub parent_id: String,
    pub kind: NodeKind,
    pub status: NodeStatus,
    pub summary: String,
    pub evidence_refs: Vec<String>,
    pub tool_call_refs: Vec<String>,
    pub created_at: SystemTime,
    pub updated_at: SystemTime,
}

impl Node {
    /// JSON form mirroring Go's `Node` marshalling (`omitempty` on the ref
    /// lists, RFC 3339 timestamps).
    pub fn to_json(&self) -> Value {
        let mut object = Map::new();
        object.insert("id".into(), json!(self.id));
        object.insert("parent_id".into(), json!(self.parent_id));
        object.insert("kind".into(), json!(self.kind.as_str()));
        object.insert("status".into(), json!(self.status.as_str()));
        object.insert("summary".into(), json!(self.summary));
        if !self.evidence_refs.is_empty() {
            object.insert("evidence_refs".into(), json!(self.evidence_refs));
        }
        if !self.tool_call_refs.is_empty() {
            object.insert("tool_call_refs".into(), json!(self.tool_call_refs));
        }
        object.insert("created_at".into(), json!(format_rfc3339(self.created_at)));
        object.insert("updated_at".into(), json!(format_rfc3339(self.updated_at)));
        Value::Object(object)
    }
}

/// The versioned, normalized representation of a model's reasoning. `root_id`
/// points at the root node (the first node when the parser produced the
/// tree); it is empty when no reasoning was detected.
#[derive(Debug, Clone, PartialEq)]
pub struct ContextTree {
    pub version: String,
    pub format: Format,
    pub model: String,
    pub backend: String,
    pub created_at: SystemTime,
    pub root_id: String,
    pub nodes: Vec<Node>,
}

impl ContextTree {
    /// Reports whether the tree holds no reasoning nodes.
    pub fn is_empty(&self) -> bool {
        self.nodes.is_empty()
    }

    /// JSON form mirroring Go's `ContextTree` marshalling.
    pub fn to_json(&self) -> Value {
        let mut object = Map::new();
        object.insert("version".into(), json!(self.version));
        object.insert("format".into(), json!(self.format.as_str()));
        object.insert("model".into(), json!(self.model));
        object.insert("backend".into(), json!(self.backend));
        object.insert("created_at".into(), json!(format_rfc3339(self.created_at)));
        object.insert("root_id".into(), json!(self.root_id));
        let nodes: Vec<Value> = self.nodes.iter().map(Node::to_json).collect();
        object.insert("nodes".into(), json!(nodes));
        Value::Object(object)
    }
}

/// Formats a timestamp the way Go marshals `time.Time` (UTC RFC 3339 with
/// trailing fractional zeros trimmed).
pub(crate) fn format_rfc3339(time: SystemTime) -> String {
    let elapsed = time.duration_since(UNIX_EPOCH).unwrap_or_default();
    let secs = elapsed.as_secs() as i64;
    let nanos = elapsed.subsec_nanos();
    let (year, month, day) = civil_from_days(secs.div_euclid(86_400));
    let second_of_day = secs.rem_euclid(86_400);
    let (hour, minute, second) = (
        second_of_day / 3600,
        (second_of_day % 3600) / 60,
        second_of_day % 60,
    );
    let mut out = format!("{year:04}-{month:02}-{day:02}T{hour:02}:{minute:02}:{second:02}");
    if nanos > 0 {
        let fraction = format!("{nanos:09}").trim_end_matches('0').to_owned();
        if !fraction.is_empty() {
            out.push('.');
            out.push_str(&fraction);
        }
    }
    out.push('Z');
    out
}

/// Days since the Unix epoch to (year, month, day) — Howard Hinnant's
/// `civil_from_days` algorithm.
fn civil_from_days(days: i64) -> (i64, u32, u32) {
    let z = days + 719_468;
    let era = z.div_euclid(146_097);
    let day_of_era = z.rem_euclid(146_097);
    let year_of_era =
        (day_of_era - day_of_era / 1460 + day_of_era / 36_524 - day_of_era / 146_096) / 365;
    let year = year_of_era + era * 400;
    let day_of_year = day_of_era - (365 * year_of_era + year_of_era / 4 - year_of_era / 100);
    let mp = (5 * day_of_year + 2) / 153;
    let day = day_of_year - (153 * mp + 2) / 5 + 1;
    let month = if mp < 10 { mp + 3 } else { mp - 9 };
    (
        if month <= 2 { year + 1 } else { year },
        month as u32,
        day as u32,
    )
}
