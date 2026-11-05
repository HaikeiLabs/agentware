//! Model-facing guidance nudges (port of `go/middleware/guardrails/nudge.go`).
//!
//! A nudge is a user-role message the harness appends to the conversation so
//! the model corrects itself. Nudges are quality guidance only; they never
//! authorize or deny tool execution.

/// The kind of guidance a nudge carries (Go `NudgeKind`).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum NudgeKind {
    /// The response was not a valid tool call; retry with one.
    Retry,
    /// The model named a tool that does not exist.
    UnknownTool,
    /// The terminal tool is blocked by incomplete required steps.
    Step,
    /// A prerequisite tool has not been recorded yet.
    Prerequisite,
}

impl NudgeKind {
    /// The wire spelling, matching the Go `NudgeKind` constants.
    pub fn as_str(&self) -> &'static str {
        match self {
            Self::Retry => "retry",
            Self::UnknownTool => "unknown_tool",
            Self::Step => "step",
            Self::Prerequisite => "prerequisite",
        }
    }
}

/// A user-role guidance message (Go `Nudge`).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Nudge {
    pub role: String,
    pub content: String,
    pub kind: NudgeKind,
    pub tier: i32,
}

/// Nudge for a response that was not a valid tool call (Go `RetryNudge`).
///
/// The raw response is accepted for parity with Go but is not echoed into the
/// nudge text.
pub fn retry_nudge(_raw_response: &str, tool_names: &[String]) -> Nudge {
    let tools_list = join_tool_names(tool_names);
    Nudge {
        role: "user".to_owned(),
        content: format!(
            "Your previous response was not a valid tool call. You must respond with a tool call, not free text. Available tools: {tools_list}. Please try again with a valid tool call."
        ),
        kind: NudgeKind::Retry,
        tier: 0,
    }
}

/// Nudge for a call to a tool that does not exist (Go `UnknownToolNudge`).
pub fn unknown_tool_nudge(tool_name: &str, tool_names: &[String]) -> Nudge {
    let tools_list = join_tool_names(tool_names);
    Nudge {
        role: "user".to_owned(),
        content: format!(
            "Tool '{tool_name}' does not exist. Available tools: {tools_list}. Call one of them."
        ),
        kind: NudgeKind::UnknownTool,
        tier: 0,
    }
}

/// Nudge for a terminal tool blocked by incomplete steps (Go `StepNudge`).
///
/// The tier is clamped to 1..=3; higher tiers escalate the wording.
pub fn step_nudge(terminal_tool: &str, pending_steps: &[String], tier: i32) -> Nudge {
    let tier = tier.clamp(1, 3);
    let steps = join_tool_names(pending_steps);

    let content = match tier {
        1 => format!(
            "You cannot call {terminal_tool} yet. You must first complete these required steps: {steps}. Call one of them now."
        ),
        2 => format!("You must call one of these tools now: {steps}. Pick one."),
        _ => format!(
            "STOP. You MUST call one of: {steps}. Do NOT call {terminal_tool}. Your next response MUST be a tool call to one of: {steps}."
        ),
    };

    Nudge {
        role: "user".to_owned(),
        content,
        kind: NudgeKind::Step,
        tier,
    }
}

/// Nudge for a tool whose prerequisite has not been recorded (Go
/// `PrerequisiteNudge`).
pub fn prerequisite_nudge(tool_name: &str, missing_prereqs: &[String]) -> Nudge {
    let prereqs = join_tool_names(missing_prereqs);
    Nudge {
        role: "user".to_owned(),
        content: format!(
            "You cannot call {tool_name} yet. You must first call: {prereqs}. Call the prerequisite tool now."
        ),
        kind: NudgeKind::Prerequisite,
        tier: 0,
    }
}

/// Joins tool names the way the Go `joinToolNames` helper does: one name
/// verbatim, two with " and ", three or more Oxford-comma separated, and an
/// empty list as "(no tools available)".
pub(crate) fn join_tool_names(names: &[String]) -> String {
    match names.len() {
        0 => "(no tools available)".to_owned(),
        1 => names[0].clone(),
        2 => format!("{} and {}", names[0], names[1]),
        _ => {
            let mut result = names[0].clone();
            for (i, name) in names.iter().enumerate().skip(1) {
                if i == names.len() - 1 {
                    result.push_str(", and ");
                } else {
                    result.push_str(", ");
                }
                result.push_str(name);
            }
            result
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Mirrors go TestRetryNudge.
    #[test]
    fn test_retry_nudge() {
        let tools = ["get_weather", "echo", "search"].map(str::to_owned);
        let nudge = retry_nudge("some text response", &tools);
        assert_eq!(nudge.role, "user");
        assert_eq!(nudge.kind, NudgeKind::Retry);
        assert_eq!(nudge.tier, 0);
        assert!(!nudge.content.is_empty());
    }

    /// Mirrors go TestUnknownToolNudge.
    #[test]
    fn test_unknown_tool_nudge() {
        let tools = ["echo", "search"].map(str::to_owned);
        let nudge = unknown_tool_nudge("nonexistent_tool", &tools);
        assert_eq!(nudge.kind, NudgeKind::UnknownTool);
        assert_eq!(nudge.tier, 0);
    }

    /// Mirrors go TestStepNudge_Tier1.
    #[test]
    fn step_nudge_tier1() {
        let pending = ["validate", "prepare"].map(str::to_owned);
        let nudge = step_nudge("submit", &pending, 1);
        assert_eq!(nudge.kind, NudgeKind::Step);
        assert_eq!(nudge.tier, 1);
    }

    /// Mirrors go TestStepNudge_Tier2.
    #[test]
    fn step_nudge_tier2() {
        let pending = ["validate", "prepare"].map(str::to_owned);
        let nudge = step_nudge("submit", &pending, 2);
        assert_eq!(nudge.tier, 2);
    }

    /// Mirrors go TestStepNudge_Tier3.
    #[test]
    fn step_nudge_tier3() {
        let pending = ["validate", "prepare"].map(str::to_owned);
        let nudge = step_nudge("submit", &pending, 3);
        assert_eq!(nudge.tier, 3);
    }

    /// Mirrors go TestStepNudge_TierClamping.
    #[test]
    fn step_nudge_tier_clamping() {
        let pending = ["validate"].map(str::to_owned);
        assert_eq!(step_nudge("submit", &pending, 0).tier, 1);
        assert_eq!(step_nudge("submit", &pending, 5).tier, 3);
    }

    /// Mirrors go TestPrerequisiteNudge.
    #[test]
    fn test_prerequisite_nudge() {
        let missing = ["authenticate", "validate"].map(str::to_owned);
        let nudge = prerequisite_nudge("submit", &missing);
        assert_eq!(nudge.kind, NudgeKind::Prerequisite);
        assert_eq!(nudge.tier, 0);
    }

    /// Mirrors go TestJoinToolNames.
    #[test]
    fn test_join_tool_names() {
        assert_eq!(join_tool_names(&[]), "(no tools available)");
        assert_eq!(join_tool_names(&["echo".to_owned()]), "echo");
        assert_eq!(
            join_tool_names(&["echo".to_owned(), "search".to_owned()]),
            "echo and search"
        );
        assert_eq!(
            join_tool_names(&["echo".to_owned(), "search".to_owned(), "weather".to_owned()]),
            "echo, search, and weather"
        );
    }
}
