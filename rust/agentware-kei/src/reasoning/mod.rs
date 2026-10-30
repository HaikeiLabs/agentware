//! Model-agnostic reasoning adapter (port of `go/reasoning`).
//!
//! The adapter recognizes native reasoning fields (DeepSeek/Qwen/Kimi/GLM
//! JSON), generic thinking tags (`<thinking>...</thinking>`,
//! `[THINK]...[/THINK]`), and model-specific reasoning fields registered per
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
pub mod types;

pub use adapter::{Adapter, Analysis, ReasoningError};
pub use options::{Clock, Options};
pub use types::{CONTRACT_VERSION, ContextTree, Format, Node, NodeKind, NodeStatus};
