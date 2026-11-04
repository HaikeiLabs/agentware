//! Step-order enforcement per session (port of
//! `go/middleware/guardrails/step_enforcer.go`).
//!
//! A step may declare prerequisite steps that must have been marked complete
//! in the session before it can execute. This is progress tracking only; it
//! never authorizes or denies execution.

use std::collections::{BTreeMap, BTreeSet};

/// The Go `ErrStepNotAllowed` sentinel message. In Go, `ValidateExecution`
/// returns a `*StepNotAllowedError` that matches this sentinel via `Is`; the
/// Rust equivalent is the [`StepNotAllowedError`] type itself.
pub const ERR_STEP_NOT_ALLOWED: &str =
    "step not allowed: prerequisite steps must be completed first";

/// A tool call rejected because prerequisite steps are incomplete (Go
/// `StepNotAllowedError`).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct StepNotAllowedError {
    pub tool: String,
    pub missing_steps: Vec<String>,
}

impl std::fmt::Display for StepNotAllowedError {
    // Go `StepNotAllowedError.Error()` returns "step not allowed".
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str("step not allowed")
    }
}

impl std::error::Error for StepNotAllowedError {}

/// Enforces step prerequisites per session (Go `StepEnforcer`).
#[derive(Debug, Default)]
pub struct StepEnforcer {
    step_definitions: BTreeMap<String, Vec<String>>,
    completed_steps: BTreeMap<String, BTreeSet<String>>,
    // Written by `add_terminal` and never read, mirroring the Go
    // `allowedTerminals` field, which is also write-only.
    allowed_terminals: BTreeMap<String, BTreeSet<String>>,
}

impl StepEnforcer {
    /// Creates an empty enforcer (Go `NewStepEnforcer`).
    pub fn new() -> Self {
        Self::default()
    }

    /// Declares the prerequisites for a step (Go `AddStep`).
    pub fn add_step(&mut self, tool: &str, prerequisites: Vec<String>) {
        self.step_definitions.insert(tool.to_owned(), prerequisites);
    }

    /// Records the terminals allowed for a step (Go `AddTerminal`).
    pub fn add_terminal(&mut self, tool: &str, allowed: Vec<String>) {
        self.allowed_terminals
            .insert(tool.to_owned(), allowed.into_iter().collect());
    }

    /// Marks a step complete for a session (Go `MarkStepComplete`).
    pub fn mark_step_complete(&mut self, session_id: &str, step: &str) {
        self.completed_steps
            .entry(session_id.to_owned())
            .or_default()
            .insert(step.to_owned());
    }

    /// Drops all recorded progress for a session (Go `ResetSession`).
    pub fn reset_session(&mut self, session_id: &str) {
        self.completed_steps.remove(session_id);
    }

    /// Whether a step may execute in a session, and which prerequisites are
    /// still missing (Go `CanExecute`). A step with no definition is always
    /// allowed.
    pub fn can_execute(&self, session_id: &str, tool: &str) -> (bool, Vec<String>) {
        let Some(prereqs) = self.step_definitions.get(tool) else {
            return (true, Vec::new());
        };

        let completed = self.completed_steps.get(session_id);
        let missing: Vec<String> = prereqs
            .iter()
            .filter(|prereq| completed.map(|set| !set.contains(*prereq)).unwrap_or(true))
            .cloned()
            .collect();

        (missing.is_empty(), missing)
    }

    /// Validates that a step may execute, returning the missing steps when it
    /// cannot (Go `ValidateExecution`).
    pub fn validate_execution(
        &self,
        session_id: &str,
        tool: &str,
    ) -> Result<(), StepNotAllowedError> {
        let (allowed, missing) = self.can_execute(session_id, tool);
        if allowed {
            Ok(())
        } else {
            Err(StepNotAllowedError {
                tool: tool.to_owned(),
                missing_steps: missing,
            })
        }
    }

    /// Whether a terminal tool may execute in a session (Go
    /// `IsTerminalAllowed`), which is the step check for that tool.
    pub fn is_terminal_allowed(&self, session_id: &str, terminal_tool: &str) -> bool {
        let (allowed, _) = self.can_execute(session_id, terminal_tool);
        allowed
    }

    /// The steps whose prerequisites are complete in a session (Go
    /// `GetAllowedTerminals`). Go iterates a map in random order; the Rust
    /// port returns them in sorted order.
    pub fn get_allowed_terminals(&self, session_id: &str) -> Vec<String> {
        self.step_definitions
            .keys()
            .filter(|tool| self.is_terminal_allowed(session_id, tool))
            .cloned()
            .collect()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Mirrors go TestStepEnforcer_AddStep.
    #[test]
    fn add_step() {
        let mut se = StepEnforcer::new();
        se.add_step("deploy", ["build", "test"].map(str::to_owned).to_vec());

        let (allowed, missing) = se.can_execute("session1", "deploy");
        assert!(!allowed);
        assert_eq!(missing.len(), 2);
    }

    /// Mirrors go TestStepEnforcer_MarkStepComplete.
    #[test]
    fn mark_step_complete() {
        let mut se = StepEnforcer::new();
        se.add_step("deploy", ["build", "test"].map(str::to_owned).to_vec());

        se.mark_step_complete("session1", "build");
        let (allowed, missing) = se.can_execute("session1", "deploy");

        assert!(!allowed);
        assert_eq!(missing, vec!["test".to_owned()]);
    }

    /// Mirrors go TestStepEnforcer_ValidateExecution_Success.
    #[test]
    fn validate_execution_success() {
        let mut se = StepEnforcer::new();
        se.add_step("deploy", ["build"].map(str::to_owned).to_vec());

        se.mark_step_complete("session1", "build");
        assert!(se.validate_execution("session1", "deploy").is_ok());
    }

    /// Mirrors go TestStepEnforcer_ValidateExecution_Failure.
    #[test]
    fn validate_execution_failure() {
        let mut se = StepEnforcer::new();
        se.add_step("deploy", ["build"].map(str::to_owned).to_vec());

        // The typed error is the Rust form of Go's
        // errors.Is(err, ErrStepNotAllowed).
        let err = se
            .validate_execution("session1", "deploy")
            .expect_err("expected error");
        assert_eq!(err.to_string(), "step not allowed");
        assert_eq!(
            err,
            StepNotAllowedError {
                tool: "deploy".to_owned(),
                missing_steps: vec!["build".to_owned()],
            }
        );
    }

    /// Mirrors go TestStepEnforcer_ResetSession.
    #[test]
    fn reset_session() {
        let mut se = StepEnforcer::new();
        se.add_step("deploy", ["build"].map(str::to_owned).to_vec());

        se.mark_step_complete("session1", "build");
        se.reset_session("session1");

        let (allowed, _) = se.can_execute("session1", "deploy");
        assert!(!allowed);
    }

    /// Mirrors go TestStepEnforcer_IsTerminalAllowed.
    #[test]
    fn is_terminal_allowed() {
        let mut se = StepEnforcer::new();
        se.add_step("deploy", ["build"].map(str::to_owned).to_vec());
        se.add_step("build", Vec::new());

        se.mark_step_complete("session1", "build");
        assert!(se.is_terminal_allowed("session1", "deploy"));
    }

    /// Mirrors go TestStepEnforcer_GetAllowedTerminals.
    #[test]
    fn get_allowed_terminals() {
        let mut se = StepEnforcer::new();
        se.add_step("deploy", ["build"].map(str::to_owned).to_vec());
        se.add_step("test", Vec::new());

        let allowed = se.get_allowed_terminals("session1");
        assert_eq!(allowed, vec!["test".to_owned()]);
    }

    /// Mirrors go TestStepEnforcer_NoPrerequisites.
    #[test]
    fn no_prerequisites() {
        let mut se = StepEnforcer::new();
        se.add_step("build", Vec::new());

        let (allowed, _) = se.can_execute("session1", "build");
        assert!(allowed);
    }

    /// Mirrors go TestStepEnforcer_InvalidSession.
    #[test]
    fn invalid_session() {
        let mut se = StepEnforcer::new();
        se.add_step("deploy", ["build"].map(str::to_owned).to_vec());

        let (allowed, _) = se.can_execute("nonexistent", "deploy");
        assert!(!allowed);
    }
}
