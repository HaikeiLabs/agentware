//! Model-agnostic reasoning adapter (port of `go/reasoning`).
//!
//! The adapter recognizes native reasoning fields (DeepSeek/Qwen/Kimi/GLM
//! JSON), generic reasoning tags (every pair in [`REASONING_TAG_PAIRS`]:
//! think, thinking and bracket THINK; see [`tags`]), and model-specific reasoning fields registered per
//! model name. It splits a raw model output into clean content (for ordinary
//! tool-call parsing) and normalized reasoning (a versioned context tree
//! carrying bounded summaries only).
//!
//! The adapter fails closed: malformed input (a reasoning field that is not a
//! string, unbalanced thinking tags, malformed JSON) and unbounded input
//! (reasoning over the byte cap, too many nodes, too deep a chain) return
//! [`ReasoningError`] instead of emitting partial reasoning.

pub mod adapter;
pub mod options;
pub mod parse;
pub mod tags;
pub mod types;

pub use adapter::{Adapter, Analysis, ReasoningError};
pub use options::{Clock, Options};
pub use tags::{
    BRACKET_THINK_CLOSE, BRACKET_THINK_OPEN, REASONING_TAG_PAIRS, THINK_CLOSE, THINK_OPEN,
    THINKING_CLOSE, THINKING_OPEN, TagPair,
};
pub use types::{CONTRACT_VERSION, ContextTree, Format, Node, NodeKind, NodeStatus};
