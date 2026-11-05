//! The configured agent-quality facade (port of
//! `go/middleware/guardrails/facade.go`).
//!
//! `Guardrails` composes response validation, step enforcement, error
//! tracking, and nudges behind one configured facade. Outcomes describe
//! agent quality and progress only; they never grant or deny authorization.

use serde_json::{Map, Value};
use std::collections::BTreeMap;

use super::error_tracker::{ErrorCategory, ErrorTracker};
use super::nudge::{Nudge, NudgeKind, prerequisite_nudge, retry_nudge, step_nudge};
use super::response_validator::{ResponseValidator, ToolCall};
use super::step_enforcer::StepEnforcer;

/// The supported guardrails config version (Go `guardrailsConfigVersion`).
pub const GUARDRAILS_CONFIG_VERSION: i64 = 1;

/// Quality and progress checks configuration (Go `Config`). It does not
/// express tool authorization or make allow/deny decisions.
#[derive(Debug, Clone, PartialEq)]
pub struct Config {
    pub version: i64,
    pub tool_names: Vec<String>,
    pub required_steps: Vec<String>,
    pub terminal_tool: String,
    pub prerequisites: Vec<Prerequisite>,
    pub max_prereq_violations: i64,
    pub step_nudge_tier: i64,
    pub nudges: Option<ConfiguredNudges>,
}

/// A tool that must have been recorded before another tool can be called
/// (Go `Prerequisite`). `match_arg` optionally requires both calls to share
/// an arg value.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Prerequisite {
    pub tool: String,
    pub requires: String,
    pub match_arg: String,
}

/// Optional overrides of the default quality guidance text (Go
/// `ConfiguredNudges`).
#[derive(Debug, Clone, PartialEq, Eq, Default)]
pub struct ConfiguredNudges {
    pub retry: String,
    pub step_blocked: String,
    pub prerequisite: String,
    pub fatal: String,
}

/// A quality and progress outcome, never an authorization decision (Go
/// `Outcome`).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Outcome {
    /// The tool calls are usable and progress is on track.
    Execute,
    /// The model must retry (invalid response or unknown tool).
    Retry,
    /// The terminal tool is blocked by incomplete required steps.
    StepBlocked,
    /// Prerequisite violations exceeded the limit.
    Fatal,
    /// Progress or an error was recorded.
    Recorded,
}

impl Outcome {
    /// The wire spelling, matching the Go `Outcome` constants.
    pub fn as_str(&self) -> &'static str {
        match self {
            Self::Execute => "execute",
            Self::Retry => "retry",
            Self::StepBlocked => "step_blocked",
            Self::Fatal => "fatal",
            Self::Recorded => "recorded",
        }
    }
}

/// The guidance returned for a response or tool result (Go
/// `GuardrailResult`). A nudge can be appended to the model-facing tool
/// response.
#[derive(Debug, Clone, PartialEq)]
pub struct GuardrailResult {
    pub outcome: Outcome,
    pub tool_calls: Vec<ToolCall>,
    pub missing_steps: Vec<String>,
    pub missing_prerequisites: Vec<String>,
    pub prereq_violations: i64,
    pub nudge: Option<Nudge>,
}

#[derive(Debug, Clone)]
struct RecordedCall {
    tool: String,
    args: Map<String, Value>,
}

/// The configured agent-quality facade (Go `Guardrails`).
#[derive(Debug)]
pub struct Guardrails {
    config: Config,
    validator: ResponseValidator,
    steps: StepEnforcer,
    errors: ErrorTracker,
    recorded: BTreeMap<String, Vec<RecordedCall>>,
    prereq_violations: BTreeMap<String, i64>,
}

impl Guardrails {
    /// Checks generated tool calls and returns quality or progress guidance
    /// (Go `Check`). It never authorizes or denies execution.
    pub fn check(&mut self, session_id: &str, calls: &[ToolCall]) -> GuardrailResult {
        if calls.is_empty() {
            let nudge = self.with_text(
                Some(retry_nudge("", &self.config.tool_names)),
                self.retry_text(),
            );
            return GuardrailResult {
                outcome: Outcome::Retry,
                tool_calls: Vec::new(),
                missing_steps: Vec::new(),
                missing_prerequisites: Vec::new(),
                prereq_violations: 0,
                nudge,
            };
        }
        let validation = self.validator.validate_tool_calls(calls);
        if validation.needs_retry {
            let nudge = self.with_text(validation.nudge, self.retry_text());
            return GuardrailResult {
                outcome: Outcome::Retry,
                tool_calls: Vec::new(),
                missing_steps: Vec::new(),
                missing_prerequisites: Vec::new(),
                prereq_violations: 0,
                nudge,
            };
        }

        for call in &validation.tool_calls {
            if !self.config.terminal_tool.is_empty() && call.tool == self.config.terminal_tool {
                let (allowed, missing) = self.steps.can_execute(session_id, &call.tool);
                if !allowed {
                    let nudge =
                        step_nudge(&call.tool, &missing, self.config.step_nudge_tier as i32);
                    let nudge = self.with_text(Some(nudge), self.step_text());
                    return GuardrailResult {
                        outcome: Outcome::StepBlocked,
                        tool_calls: Vec::new(),
                        missing_steps: missing,
                        missing_prerequisites: Vec::new(),
                        prereq_violations: 0,
                        nudge,
                    };
                }
            }
            let missing = self.missing_prerequisites(session_id, call);
            if !missing.is_empty() {
                let count = self
                    .prereq_violations
                    .entry(session_id.to_owned())
                    .or_insert(0);
                *count += 1;
                let count = *count;
                let nudge = prerequisite_nudge(&call.tool, &missing);
                let (outcome, text) = if count > self.config.max_prereq_violations {
                    (Outcome::Fatal, self.fatal_text())
                } else {
                    (Outcome::Retry, self.prerequisite_text())
                };
                let nudge = self.with_text(Some(nudge), text);
                return GuardrailResult {
                    outcome,
                    tool_calls: Vec::new(),
                    missing_steps: Vec::new(),
                    missing_prerequisites: missing,
                    prereq_violations: count,
                    nudge,
                };
            }
        }
        GuardrailResult {
            outcome: Outcome::Execute,
            tool_calls: validation.tool_calls,
            missing_steps: Vec::new(),
            missing_prerequisites: Vec::new(),
            prereq_violations: 0,
            nudge: None,
        }
    }

    /// Stores successful progress or tracks a tool error (Go `Record`). Error
    /// nudges are returned for injection into the model-facing tool-error
    /// response.
    pub fn record(
        &mut self,
        session_id: &str,
        call: &ToolCall,
        error: Option<&str>,
        category: ErrorCategory,
    ) -> GuardrailResult {
        match error {
            Some(message) => {
                self.errors.record_error(
                    session_id,
                    &call.tool,
                    call.args.clone(),
                    message,
                    category,
                );
                let nudge = Nudge {
                    role: "user".to_owned(),
                    content: format!(
                        "Tool {:?} failed: {message}. Correct the issue and try again.",
                        call.tool
                    ),
                    kind: NudgeKind::Retry,
                    tier: 0,
                };
                let nudge = self.with_text(Some(nudge), self.retry_text());
                GuardrailResult {
                    outcome: Outcome::Recorded,
                    tool_calls: Vec::new(),
                    missing_steps: Vec::new(),
                    missing_prerequisites: Vec::new(),
                    prereq_violations: 0,
                    nudge,
                }
            }
            None => {
                self.steps.mark_step_complete(session_id, &call.tool);
                self.recorded
                    .entry(session_id.to_owned())
                    .or_default()
                    .push(RecordedCall {
                        tool: call.tool.clone(),
                        args: call.args.clone(),
                    });
                GuardrailResult {
                    outcome: Outcome::Recorded,
                    tool_calls: Vec::new(),
                    missing_steps: Vec::new(),
                    missing_prerequisites: Vec::new(),
                    prereq_violations: 0,
                    nudge: None,
                }
            }
        }
    }

    /// The composed error tracker, for inspection and integration (Go
    /// `ErrorTracker()`).
    pub fn error_tracker(&self) -> &ErrorTracker {
        &self.errors
    }

    fn missing_prerequisites(&self, session_id: &str, call: &ToolCall) -> Vec<String> {
        let mut missing: Vec<String> = Vec::new();
        for rule in &self.config.prerequisites {
            if rule.tool != call.tool || self.has_recorded_prerequisite(session_id, rule, call) {
                continue;
            }
            if !missing.contains(&rule.requires) {
                missing.push(rule.requires.clone());
            }
        }
        missing
    }

    fn has_recorded_prerequisite(
        &self,
        session_id: &str,
        rule: &Prerequisite,
        call: &ToolCall,
    ) -> bool {
        for prior in self.recorded.get(session_id).into_iter().flatten() {
            if prior.tool != rule.requires {
                continue;
            }
            if rule.match_arg.is_empty() {
                return true;
            }
            let Some(prior_value) = prior.args.get(&rule.match_arg) else {
                continue;
            };
            let Some(call_value) = call.args.get(&rule.match_arg) else {
                continue;
            };
            if prior_value == call_value {
                return true;
            }
        }
        false
    }

    fn with_text(&self, nudge: Option<Nudge>, text: String) -> Option<Nudge> {
        nudge.map(|mut nudge| {
            if !text.is_empty() {
                nudge.content = text;
            }
            nudge
        })
    }

    fn retry_text(&self) -> String {
        self.config
            .nudges
            .as_ref()
            .map(|n| n.retry.clone())
            .unwrap_or_default()
    }

    fn step_text(&self) -> String {
        self.config
            .nudges
            .as_ref()
            .map(|n| n.step_blocked.clone())
            .unwrap_or_default()
    }

    fn prerequisite_text(&self) -> String {
        self.config
            .nudges
            .as_ref()
            .map(|n| n.prerequisite.clone())
            .unwrap_or_default()
    }

    fn fatal_text(&self) -> String {
        self.config
            .nudges
            .as_ref()
            .map(|n| n.fatal.clone())
            .unwrap_or_default()
    }
}

/// Decodes and validates a versioned guardrails config document (Go
/// `LoadConfig`). The decode is strict: unknown fields at any level and
/// trailing data are rejected.
pub fn load_config(data: &[u8]) -> Result<Config, String> {
    let doc: Value = serde_json::from_slice(data)
        .map_err(|error| format!("decode guardrails config: {error}"))?;
    let mut config = decode_config(doc)?;
    validate_config(&mut config)?;
    Ok(config)
}

/// Creates the facade from a validated configuration (Go `NewGuardrails`).
pub fn new_guardrails(mut config: Config) -> Result<Guardrails, String> {
    validate_config(&mut config)?;
    let mut steps = StepEnforcer::new();
    if !config.terminal_tool.is_empty() {
        steps.add_step(&config.terminal_tool, config.required_steps.clone());
    }
    let tool_names = config.tool_names.clone();
    Ok(Guardrails {
        config,
        validator: ResponseValidator::new(tool_names, false),
        steps,
        errors: ErrorTracker::new(),
        recorded: BTreeMap::new(),
        prereq_violations: BTreeMap::new(),
    })
}

/// Strict decode of the config object; unknown fields and wrong types are
/// rejected the way Go's `DisallowUnknownFields` decoder does.
fn decode_config(doc: Value) -> Result<Config, String> {
    let obj = match doc.as_object() {
        Some(obj) => obj,
        // A JSON null decodes to the zero Config in Go and then fails
        // validation; treat any non-object the same way.
        None => {
            return Ok(Config {
                version: 0,
                tool_names: Vec::new(),
                required_steps: Vec::new(),
                terminal_tool: String::new(),
                prerequisites: Vec::new(),
                max_prereq_violations: 0,
                step_nudge_tier: 0,
                nudges: None,
            });
        }
    };

    for key in obj.keys() {
        match key.as_str() {
            "version"
            | "tool_names"
            | "required_steps"
            | "terminal_tool"
            | "prerequisites"
            | "max_prereq_violations"
            | "step_nudge_tier"
            | "nudges" => {}
            other => return Err(format!("decode guardrails config: unknown field {other:?}")),
        }
    }

    // A JSON null into a field is a no-op in Go's decoder, so null decodes
    // to the field's zero value.
    let version = match obj.get("version") {
        None | Some(Value::Null) => 0,
        Some(Value::Number(n)) => n
            .as_i64()
            .ok_or_else(|| "decode guardrails config: version must be an integer".to_owned())?,
        Some(_) => return Err("decode guardrails config: version must be an integer".to_owned()),
    };

    let tool_names = match obj.get("tool_names") {
        None | Some(Value::Null) => Vec::new(),
        Some(Value::Array(items)) => items
            .iter()
            .map(|item| {
                item.as_str().map(str::to_owned).ok_or_else(|| {
                    "decode guardrails config: tool_names must be strings".to_owned()
                })
            })
            .collect::<Result<Vec<String>, String>>()?,
        Some(_) => {
            return Err(
                "decode guardrails config: tool_names must be an array of strings".to_owned(),
            );
        }
    };

    let required_steps = match obj.get("required_steps") {
        None | Some(Value::Null) => Vec::new(),
        Some(Value::Array(items)) => items
            .iter()
            .map(|item| {
                item.as_str().map(str::to_owned).ok_or_else(|| {
                    "decode guardrails config: required_steps must be strings".to_owned()
                })
            })
            .collect::<Result<Vec<String>, String>>()?,
        Some(_) => {
            return Err(
                "decode guardrails config: required_steps must be an array of strings".to_owned(),
            );
        }
    };

    let terminal_tool = match obj.get("terminal_tool") {
        None | Some(Value::Null) => String::new(),
        Some(Value::String(s)) => s.clone(),
        Some(_) => {
            return Err("decode guardrails config: terminal_tool must be a string".to_owned());
        }
    };

    let prerequisites = match obj.get("prerequisites") {
        None | Some(Value::Null) => Vec::new(),
        Some(Value::Array(items)) => items
            .iter()
            .map(decode_prerequisite)
            .collect::<Result<Vec<Prerequisite>, String>>()?,
        Some(_) => {
            return Err("decode guardrails config: prerequisites must be an array".to_owned());
        }
    };

    let max_prereq_violations = match obj.get("max_prereq_violations") {
        None | Some(Value::Null) => 0,
        Some(Value::Number(n)) => n.as_i64().ok_or_else(|| {
            "decode guardrails config: max_prereq_violations must be an integer".to_owned()
        })?,
        Some(_) => {
            return Err(
                "decode guardrails config: max_prereq_violations must be an integer".to_owned(),
            );
        }
    };

    let step_nudge_tier = match obj.get("step_nudge_tier") {
        None | Some(Value::Null) => 0,
        Some(Value::Number(n)) => n.as_i64().ok_or_else(|| {
            "decode guardrails config: step_nudge_tier must be an integer".to_owned()
        })?,
        Some(_) => {
            return Err("decode guardrails config: step_nudge_tier must be an integer".to_owned());
        }
    };

    let nudges = match obj.get("nudges") {
        None | Some(Value::Null) => None,
        Some(Value::Object(fields)) => {
            for key in fields.keys() {
                match key.as_str() {
                    "retry" | "step_blocked" | "prerequisite" | "fatal" => {}
                    other => {
                        return Err(format!(
                            "decode guardrails config: unknown nudges field {other:?}"
                        ));
                    }
                }
            }
            let string = |field: &str| -> Result<String, String> {
                match fields.get(field) {
                    None => Ok(String::new()),
                    Some(Value::String(s)) => Ok(s.clone()),
                    Some(_) => Err(format!(
                        "decode guardrails config: nudges.{field} must be a string"
                    )),
                }
            };
            Some(ConfiguredNudges {
                retry: string("retry")?,
                step_blocked: string("step_blocked")?,
                prerequisite: string("prerequisite")?,
                fatal: string("fatal")?,
            })
        }
        Some(_) => return Err("decode guardrails config: nudges must be an object".to_owned()),
    };

    Ok(Config {
        version,
        tool_names,
        required_steps,
        terminal_tool,
        prerequisites,
        max_prereq_violations,
        step_nudge_tier,
        nudges,
    })
}

fn decode_prerequisite(raw: &Value) -> Result<Prerequisite, String> {
    let obj = raw.as_object().ok_or_else(|| {
        "decode guardrails config: prerequisites entries must be objects".to_owned()
    })?;
    for key in obj.keys() {
        match key.as_str() {
            "tool" | "requires" | "match_arg" => {}
            other => {
                return Err(format!(
                    "decode guardrails config: unknown prerequisite field {other:?}"
                ));
            }
        }
    }
    let string = |field: &str| -> Result<String, String> {
        obj.get(field)
            .and_then(Value::as_str)
            .map(str::to_owned)
            .ok_or_else(|| {
                format!("decode guardrails config: prerequisite {field} must be a string")
            })
    };
    Ok(Prerequisite {
        tool: string("tool")?,
        requires: string("requires")?,
        match_arg: string("match_arg").unwrap_or_default(),
    })
}

/// Validates the config and applies the Go defaults: `max_prereq_violations`
/// 0 becomes 2, `step_nudge_tier` 0 becomes 1 (Go `validateConfig`).
fn validate_config(config: &mut Config) -> Result<(), String> {
    if config.version != GUARDRAILS_CONFIG_VERSION {
        return Err(format!(
            "unsupported guardrails config version {}",
            config.version
        ));
    }
    if config.tool_names.is_empty() {
        return Err("guardrails config requires at least one tool name".to_owned());
    }
    let mut tools: BTreeMap<&str, ()> = BTreeMap::new();
    for tool in &config.tool_names {
        if tool.is_empty() || tools.contains_key(tool.as_str()) {
            return Err(format!(
                "guardrails config has an empty or duplicate tool name {tool:?}"
            ));
        }
        tools.insert(tool.as_str(), ());
    }
    if !config.terminal_tool.is_empty() && !tools.contains_key(config.terminal_tool.as_str()) {
        return Err(format!(
            "terminal tool {:?} is not in tool_names",
            config.terminal_tool
        ));
    }
    for step in &config.required_steps {
        if !tools.contains_key(step.as_str()) {
            return Err(format!("required step {step:?} is not in tool_names"));
        }
    }
    let mut seen_prerequisites: BTreeMap<String, ()> = BTreeMap::new();
    for rule in &config.prerequisites {
        if !tools.contains_key(rule.tool.as_str())
            || !tools.contains_key(rule.requires.as_str())
            || rule.tool == rule.requires
        {
            return Err(format!(
                "invalid prerequisite rule {:?} requires {:?}",
                rule.tool, rule.requires
            ));
        }
        let key = format!("{}\0{}\0{}", rule.tool, rule.requires, rule.match_arg);
        if seen_prerequisites.contains_key(&key) {
            return Err(format!(
                "duplicate prerequisite rule for {:?} requiring {:?}",
                rule.tool, rule.requires
            ));
        }
        seen_prerequisites.insert(key, ());
    }
    if config.max_prereq_violations == 0 {
        config.max_prereq_violations = 2;
    }
    if config.max_prereq_violations < 1 {
        return Err("max_prereq_violations must be at least 1".to_owned());
    }
    if config.step_nudge_tier == 0 {
        config.step_nudge_tier = 1;
    }
    if config.step_nudge_tier < 1 || config.step_nudge_tier > 3 {
        return Err("step_nudge_tier must be from 1 to 3".to_owned());
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Mirrors go TestLoadConfigDefaultsAndValidation.
    #[test]
    fn load_config_defaults_and_validation() {
        let config = load_config(br#"{"version":1,"tool_names":["inspect"]}"#).expect("load");
        assert_eq!(config.max_prereq_violations, 2);
        assert_eq!(config.step_nudge_tier, 1);

        for input in [
            r#"{"version":2,"tool_names":["inspect"]}"#,
            r#"{"version":1,"tool_names":["inspect"],"unexpected":true}"#,
            r#"{"version":1,"tool_names":["inspect"]} {"version":1,"tool_names":["inspect"]}"#,
        ] {
            assert!(
                load_config(input.as_bytes()).is_err(),
                "LoadConfig({input}) succeeded, want error"
            );
        }
    }

    /// Mirrors go TestRecordToolErrorReturnsNudgeWithoutCompletingStep.
    #[test]
    fn record_tool_error_returns_nudge_without_completing_step() {
        let config = Config {
            version: 1,
            tool_names: ["inspect", "finish"].map(str::to_owned).to_vec(),
            required_steps: ["inspect"].map(str::to_owned).to_vec(),
            terminal_tool: "finish".to_owned(),
            prerequisites: Vec::new(),
            max_prereq_violations: 0,
            step_nudge_tier: 0,
            nudges: Some(ConfiguredNudges {
                retry: "Please correct the failed tool call.".to_owned(),
                ..Default::default()
            }),
        };
        let mut guardrails = new_guardrails(config).expect("new guardrails");

        let result = guardrails.record(
            "session",
            &ToolCall {
                tool: "inspect".to_owned(),
                args: Map::new(),
            },
            Some("temporary failure"),
            ErrorCategory::Timeout,
        );
        assert_eq!(result.outcome, Outcome::Recorded);
        assert_eq!(
            result.nudge.as_ref().map(|n| n.content.as_str()),
            Some("Please correct the failed tool call.")
        );
        assert_eq!(
            guardrails
                .error_tracker()
                .get_error_count("session", "inspect"),
            1
        );

        let blocked = guardrails.check(
            "session",
            &[ToolCall {
                tool: "finish".to_owned(),
                args: Map::new(),
            }],
        );
        assert_eq!(blocked.outcome, Outcome::StepBlocked);
    }

    /// Mirrors go ExampleNewGuardrails.
    #[test]
    fn new_guardrails_example() {
        let config = load_config(
            br#"{"version":1,"tool_names":["lookup","answer"],"required_steps":["lookup"],"terminal_tool":"answer"}"#,
        )
        .expect("load config");
        let mut guardrails = new_guardrails(config).expect("new guardrails");

        let first = guardrails.check(
            "session-1",
            &[ToolCall {
                tool: "lookup".to_owned(),
                args: Map::new(),
            }],
        );
        assert_eq!(first.outcome, Outcome::Execute);
        let _recorded = guardrails.record(
            "session-1",
            &first.tool_calls[0],
            None,
            ErrorCategory::Unknown,
        );
        let final_result = guardrails.check(
            "session-1",
            &[ToolCall {
                tool: "answer".to_owned(),
                args: Map::new(),
            }],
        );
        assert_eq!(final_result.outcome, Outcome::Execute);
    }
}
