//! Per-session tool error tracking (port of
//! `go/middleware/guardrails/error_tracker.go`).
//!
//! Records tool errors per session and tool, prunes errors outside the time
//! window, and reports whether a tool's error rate is exceeded. Tracking is
//! quality state only; it never authorizes or denies execution.

use serde_json::{Map, Value};
use std::collections::BTreeMap;
use std::time::{Duration, SystemTime};

/// The category of a tool error (Go `ErrorCategory`).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ErrorCategory {
    Timeout,
    NotFound,
    InvalidArgs,
    Permission,
    RateLimit,
    Unknown,
}

impl ErrorCategory {
    /// The wire spelling, matching the Go `ErrorCategory` constants.
    pub fn as_str(&self) -> &'static str {
        match self {
            Self::Timeout => "timeout",
            Self::NotFound => "not_found",
            Self::InvalidArgs => "invalid_args",
            Self::Permission => "permission",
            Self::RateLimit => "rate_limit",
            Self::Unknown => "unknown",
        }
    }

    /// Parses the wire spelling; any unrecognized value is `Unknown`, the
    /// Rust form of Go's untyped `ErrorCategory(...)` cast.
    pub fn parse(value: &str) -> Self {
        match value {
            "timeout" => Self::Timeout,
            "not_found" => Self::NotFound,
            "invalid_args" => Self::InvalidArgs,
            "permission" => Self::Permission,
            "rate_limit" => Self::RateLimit,
            _ => Self::Unknown,
        }
    }
}

/// One recorded tool error (Go `ToolError`).
#[derive(Debug, Clone, PartialEq)]
pub struct ToolError {
    pub timestamp: SystemTime,
    pub tool: String,
    pub args: Map<String, Value>,
    pub category: ErrorCategory,
    pub message: String,
    pub session_id: String,
    pub retry_count: i32,
}

/// Tracks tool errors per session (Go `ErrorTracker`).
#[derive(Debug, Default)]
pub struct ErrorTracker {
    errors: BTreeMap<String, Vec<ToolError>>,
    max_errors_per_tool: usize,
    window_duration: Duration,
}

impl ErrorTracker {
    /// Creates a tracker with the Go defaults: 5 errors per tool over a 5
    /// minute window (Go `NewErrorTracker`).
    pub fn new() -> Self {
        Self {
            errors: BTreeMap::new(),
            max_errors_per_tool: 5,
            window_duration: Duration::from_secs(5 * 60),
        }
    }

    /// Replaces the error-rate thresholds (Go `SetThresholds`).
    pub fn set_thresholds(&mut self, max_errors: usize, window: Duration) {
        self.max_errors_per_tool = max_errors;
        self.window_duration = window;
    }

    /// Records a tool error for a session (Go `RecordError`). The retry count
    /// is the number of earlier errors for the same tool in the session.
    pub fn record_error(
        &mut self,
        session_id: &str,
        tool: &str,
        args: Map<String, Value>,
        message: &str,
        category: ErrorCategory,
    ) {
        let retry_count = self.get_retry_count(session_id, tool) as i32;
        self.errors
            .entry(session_id.to_owned())
            .or_default()
            .push(ToolError {
                timestamp: SystemTime::now(),
                tool: tool.to_owned(),
                args,
                category,
                message: message.to_owned(),
                session_id: session_id.to_owned(),
                retry_count,
            });
        self.prune_old_errors(session_id);
    }

    fn get_retry_count(&self, session_id: &str, tool: &str) -> usize {
        self.errors
            .get(session_id)
            .map(|errors| errors.iter().filter(|err| err.tool == tool).count())
            .unwrap_or(0)
    }

    fn prune_old_errors(&mut self, session_id: &str) {
        let Some(errors) = self.errors.get_mut(session_id) else {
            return;
        };
        if errors.len() <= self.max_errors_per_tool {
            return;
        }

        let cutoff = SystemTime::now() - self.window_duration;
        errors.retain(|err| err.timestamp > cutoff);
    }

    /// The number of recorded errors for a tool in a session (Go
    /// `GetErrorCount`).
    pub fn get_error_count(&self, session_id: &str, tool: &str) -> i32 {
        self.get_retry_count(session_id, tool) as i32
    }

    /// The session's errors, pruned to the window (Go `GetRecentErrors`).
    pub fn get_recent_errors(&mut self, session_id: &str) -> Vec<ToolError> {
        self.prune_old_errors(session_id);
        self.errors.get(session_id).cloned().unwrap_or_default()
    }

    /// The session's errors of one category (Go `GetErrorsByCategory`), which
    /// does not prune, matching Go.
    pub fn get_errors_by_category(
        &self,
        session_id: &str,
        category: ErrorCategory,
    ) -> Vec<ToolError> {
        self.errors
            .get(session_id)
            .map(|errors| {
                errors
                    .iter()
                    .filter(|err| err.category == category)
                    .cloned()
                    .collect()
            })
            .unwrap_or_default()
    }

    /// Whether a tool's error count reached the threshold (Go
    /// `IsErrorRateExceeded`).
    pub fn is_error_rate_exceeded(&self, session_id: &str, tool: &str) -> bool {
        self.get_error_count(session_id, tool) as usize >= self.max_errors_per_tool
    }

    /// Drops all recorded errors for a session (Go `ResetSession`).
    pub fn reset_session(&mut self, session_id: &str) {
        self.errors.remove(session_id);
    }

    /// Whether a tool should be blocked for its error rate (Go
    /// `ShouldBlockTool`).
    pub fn should_block_tool(&self, session_id: &str, tool: &str) -> bool {
        self.is_error_rate_exceeded(session_id, tool)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Mirrors go TestErrorTracker_RecordError.
    #[test]
    fn record_error() {
        let mut et = ErrorTracker::new();
        et.record_error(
            "session1",
            "tool1",
            Map::from_iter([("key".to_owned(), Value::String("value".to_owned()))]),
            "test error",
            ErrorCategory::Unknown,
        );

        assert_eq!(et.get_error_count("session1", "tool1"), 1);
    }

    /// Mirrors go TestErrorTracker_GetErrorCount.
    #[test]
    fn get_error_count() {
        let mut et = ErrorTracker::new();
        et.record_error(
            "session1",
            "tool1",
            Map::new(),
            "error1",
            ErrorCategory::Timeout,
        );
        et.record_error(
            "session1",
            "tool1",
            Map::new(),
            "error2",
            ErrorCategory::Timeout,
        );
        et.record_error(
            "session1",
            "tool2",
            Map::new(),
            "error3",
            ErrorCategory::NotFound,
        );

        assert_eq!(et.get_error_count("session1", "tool1"), 2);
        assert_eq!(et.get_error_count("session1", "tool2"), 1);
    }

    /// Mirrors go TestErrorTracker_GetRecentErrors.
    #[test]
    fn get_recent_errors() {
        let mut et = ErrorTracker::new();
        et.record_error(
            "session1",
            "tool1",
            Map::new(),
            "error1",
            ErrorCategory::Unknown,
        );

        let errors = et.get_recent_errors("session1");
        assert_eq!(errors.len(), 1);
    }

    /// Mirrors go TestErrorTracker_GetErrorsByCategory.
    #[test]
    fn get_errors_by_category() {
        let mut et = ErrorTracker::new();
        et.record_error(
            "session1",
            "tool1",
            Map::new(),
            "timeout",
            ErrorCategory::Timeout,
        );
        et.record_error(
            "session1",
            "tool1",
            Map::new(),
            "not found",
            ErrorCategory::NotFound,
        );

        let timeout_errors = et.get_errors_by_category("session1", ErrorCategory::Timeout);
        assert_eq!(timeout_errors.len(), 1);
    }

    /// Mirrors go TestErrorTracker_IsErrorRateExceeded.
    #[test]
    fn is_error_rate_exceeded() {
        let mut et = ErrorTracker::new();
        et.set_thresholds(3, Duration::from_secs(60));

        for _ in 0..3 {
            et.record_error(
                "session1",
                "tool1",
                Map::new(),
                "error",
                ErrorCategory::Unknown,
            );
        }

        assert!(et.is_error_rate_exceeded("session1", "tool1"));
    }

    /// Mirrors go TestErrorTracker_ResetSession.
    #[test]
    fn reset_session() {
        let mut et = ErrorTracker::new();
        et.record_error(
            "session1",
            "tool1",
            Map::new(),
            "error",
            ErrorCategory::Unknown,
        );

        et.reset_session("session1");
        assert_eq!(et.get_error_count("session1", "tool1"), 0);
    }

    /// Mirrors go TestErrorTracker_ShouldBlockTool.
    #[test]
    fn should_block_tool() {
        let mut et = ErrorTracker::new();
        et.set_thresholds(2, Duration::from_secs(60));

        et.record_error(
            "session1",
            "tool1",
            Map::new(),
            "error1",
            ErrorCategory::Unknown,
        );
        et.record_error(
            "session1",
            "tool1",
            Map::new(),
            "error2",
            ErrorCategory::Unknown,
        );

        assert!(et.should_block_tool("session1", "tool1"));
        assert!(!et.should_block_tool("session1", "tool2"));
    }

    /// Mirrors go TestErrorTracker_SetThresholds.
    #[test]
    fn set_thresholds() {
        let mut et = ErrorTracker::new();
        et.set_thresholds(10, Duration::from_secs(10 * 60));

        assert_eq!(et.max_errors_per_tool, 10);
        assert_eq!(et.window_duration, Duration::from_secs(10 * 60));
    }
}
