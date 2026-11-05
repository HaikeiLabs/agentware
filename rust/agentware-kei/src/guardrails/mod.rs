//! Agent quality and progress guardrails (port of `go/middleware/guardrails`).
//!
//! The guardrails check generated responses and step progress: they validate
//! tool calls against a closed tool list, enforce step prerequisites per
//! session, track tool errors, and produce model-facing nudges. Outcomes
//! describe quality and progress only; they never grant or deny
//! authorization.

pub mod error_tracker;
pub mod facade;
pub mod nudge;
pub mod response_validator;
pub mod step_enforcer;

pub use error_tracker::{ErrorCategory, ErrorTracker, ToolError};
pub use facade::{
    Config, ConfiguredNudges, GUARDRAILS_CONFIG_VERSION, GuardrailResult, Guardrails, Outcome,
    Prerequisite, load_config, new_guardrails,
};
pub use nudge::{
    Nudge, NudgeKind, prerequisite_nudge, retry_nudge, step_nudge, unknown_tool_nudge,
};
pub use response_validator::{ResponseValidator, ToolCall, ValidationResult};
pub use step_enforcer::{ERR_STEP_NOT_ALLOWED, StepEnforcer, StepNotAllowedError};
