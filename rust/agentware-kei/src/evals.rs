//! Eval runner (port of `go/evals/runner.go`).
//!
//! Runs a case against an [`llm::Backend`](crate::llm::Backend) and scores
//! whether the expected tool was (or, for `expect: "no_call"`, was not)
//! called. Cases follow the shared JSON case-file contract in
//! `docs/evals/README.md`; [`EvalCase::from_json`] reads one.

use std::time::Instant;

use serde_json::{Map, Value, json};

use crate::llm::{Backend, Message, Request, Role, ToolDefinition};

/// `expect` value (the default): a repeat passes when the expected tool is
/// called.
pub const EXPECT_CALL: &str = "call";
/// `expect` value: a repeat passes when the expected tool (with
/// `expected_args`, if given) is NOT called.
pub const EXPECT_NO_CALL: &str = "no_call";

/// One eval case.
#[derive(Debug, Clone, PartialEq)]
pub struct EvalCase {
    pub name: String,
    pub description: String,
    pub system_prompt: String,
    pub user_message: String,
    pub tools: Vec<ToolDefinition>,
    pub expected_tool: String,
    /// [`EXPECT_CALL`] or [`EXPECT_NO_CALL`]; anything else fails the case
    /// before any model call.
    pub expect: String,
    /// Partial match on the expected tool call's arguments: every key must be
    /// present with an equal value; extra arguments are allowed.
    pub expected_args: Option<Map<String, Value>>,
    /// How many times to run the case (>= 1).
    pub repeats: i64,
    /// The case passes when `passes / repeats >= min_pass_rate` (0..1).
    pub min_pass_rate: f64,
    pub max_turns: usize,
}

impl Default for EvalCase {
    fn default() -> Self {
        Self {
            name: String::new(),
            description: String::new(),
            system_prompt: String::new(),
            user_message: String::new(),
            tools: Vec::new(),
            expected_tool: String::new(),
            expect: EXPECT_CALL.into(),
            expected_args: None,
            repeats: 1,
            min_pass_rate: 1.0,
            max_turns: 10,
        }
    }
}

fn str_field(obj: &Map<String, Value>, key: &str) -> Result<String, String> {
    match obj.get(key) {
        None | Some(Value::Null) => Ok(String::new()),
        Some(Value::String(s)) => Ok(s.clone()),
        Some(_) => Err(format!("{key} must be a string")),
    }
}

impl EvalCase {
    /// Reads one case in the shared JSON case-file contract. Missing optional
    /// fields take their defaults; a field of the wrong JSON type is an error.
    /// Value ranges are checked when the case runs ([`EvalCase::validate`]).
    pub fn from_json(value: &Value) -> Result<Self, String> {
        let obj = value.as_object().ok_or("case must be a JSON object")?;
        let mut case = EvalCase {
            name: str_field(obj, "name")?,
            description: str_field(obj, "description")?,
            system_prompt: str_field(obj, "systemPrompt")?,
            user_message: str_field(obj, "userMessage")?,
            expected_tool: str_field(obj, "expectedTool")?,
            ..EvalCase::default()
        };
        if let Some(expect) = obj.get("expect") {
            case.expect = expect.as_str().ok_or("expect must be a string")?.into();
        }
        match obj.get("expectedArgs") {
            None | Some(Value::Null) => {}
            Some(Value::Object(args)) => case.expected_args = Some(args.clone()),
            Some(_) => return Err("expectedArgs must be an object".into()),
        }
        if let Some(repeats) = obj.get("repeats") {
            case.repeats = repeats.as_i64().ok_or("repeats must be an integer")?;
        }
        if let Some(rate) = obj.get("minPassRate") {
            case.min_pass_rate = rate.as_f64().ok_or("minPassRate must be a number")?;
        }
        if let Some(turns) = obj.get("maxTurns") {
            let turns = turns
                .as_u64()
                .ok_or("maxTurns must be a non-negative integer")?;
            case.max_turns = usize::try_from(turns).map_err(|_| "maxTurns is too large")?;
        }
        if let Some(tools) = obj.get("tools") {
            let tools = tools.as_array().ok_or("tools must be an array")?;
            for tool in tools {
                let function = tool
                    .get("function")
                    .and_then(Value::as_object)
                    .ok_or("each tool needs a function object")?;
                case.tools.push(ToolDefinition {
                    name: str_field(function, "name")?,
                    description: str_field(function, "description")?,
                    input_schema: function
                        .get("parameters")
                        .and_then(Value::as_object)
                        .cloned(),
                });
            }
        }
        Ok(case)
    }

    /// Reports an invalid `expect`, `repeats` or `min_pass_rate`.
    pub fn validate(&self) -> Result<(), String> {
        if self.expect != EXPECT_CALL && self.expect != EXPECT_NO_CALL {
            return Err(format!(
                "invalid expect {:?}: must be \"call\" or \"no_call\"",
                self.expect
            ));
        }
        if self.repeats < 1 {
            return Err(format!(
                "invalid repeats {}: must be an integer >= 1",
                self.repeats
            ));
        }
        if !(0.0..=1.0).contains(&self.min_pass_rate) {
            return Err(format!(
                "invalid minPassRate {}: must be between 0 and 1",
                self.min_pass_rate
            ));
        }
        Ok(())
    }
}

/// True when every key of `expected` is in `args` with an equal value.
pub fn args_match(expected: &Map<String, Value>, args: &Map<String, Value>) -> bool {
    expected
        .iter()
        .all(|(key, want)| args.get(key) == Some(want))
}

/// One tool call the model made, across all repeats.
#[derive(Debug, Clone, PartialEq)]
pub struct RecordedToolCall {
    pub turn: usize,
    pub name: String,
    /// The arguments as a JSON string.
    pub arguments: String,
}

/// The outcome of one case.
#[derive(Debug, Clone, PartialEq)]
pub struct EvalResult {
    pub case_name: String,
    pub model_name: String,
    pub success: bool,
    /// Total turns across all repeats.
    pub turns: usize,
    /// Tool calls across all repeats, in order.
    pub tool_calls: Vec<RecordedToolCall>,
    pub error: String,
    pub duration_ms: u64,
    /// Repeats that passed.
    pub passes: i64,
    /// Repeats run.
    pub repeats: i64,
}

/// Results across models.
#[derive(Debug, Clone, PartialEq, Default)]
pub struct EvalReport {
    pub timestamp: String,
    pub models: Vec<String>,
    pub results: Vec<EvalResult>,
}

impl EvalReport {
    /// The fraction of `model`'s cases that passed (0 when it has none).
    pub fn pass_rate(&self, model: &str) -> f64 {
        let results: Vec<_> = self
            .results
            .iter()
            .filter(|r| r.model_name == model)
            .collect();
        if results.is_empty() {
            return 0.0;
        }
        results.iter().filter(|r| r.success).count() as f64 / results.len() as f64
    }

    /// The saved-report JSON (same shape as the Go, Python and TypeScript
    /// runners).
    pub fn to_json(&self) -> Value {
        json!({
            "timestamp": self.timestamp,
            "models": self.models,
            "results": self.results.iter().map(report_row).collect::<Vec<_>>(),
        })
    }
}

/// One result as a saved-report row. `passes` and `repeats` are added only
/// when the case ran more than once, so single-run reports keep their shape.
pub fn report_row(result: &EvalResult) -> Value {
    let mut row = Map::new();
    row.insert("case".into(), json!(result.case_name));
    row.insert("model".into(), json!(result.model_name));
    row.insert("success".into(), json!(result.success));
    row.insert("turns".into(), json!(result.turns));
    row.insert(
        "tool_calls".into(),
        Value::Array(
            result
                .tool_calls
                .iter()
                .map(|tc| json!({"turn": tc.turn, "name": tc.name, "arguments": tc.arguments}))
                .collect(),
        ),
    );
    row.insert("error".into(), json!(result.error));
    row.insert("duration_ms".into(), json!(result.duration_ms));
    if result.repeats > 1 {
        row.insert("passes".into(), json!(result.passes));
        row.insert("repeats".into(), json!(result.repeats));
    }
    Value::Object(row)
}

/// Runs eval cases against a backend.
#[derive(Debug, Clone, PartialEq)]
pub struct EvalRunner {
    pub max_turns: usize,
}

struct RepeatOutcome {
    passed: bool,
    turns: usize,
    error: String,
}

impl EvalRunner {
    pub fn new(max_turns: usize) -> Self {
        Self { max_turns }
    }

    /// Runs `case` `case.repeats` times against `backend`. The case passes
    /// when `passes / repeats >= case.min_pass_rate`. Tool calls other than a
    /// decisive one go to `executor`, whose result is fed back to the model.
    pub fn run_case(
        &self,
        case: &EvalCase,
        backend: &dyn Backend,
        executor: &mut dyn FnMut(&str, &Map<String, Value>) -> String,
    ) -> EvalResult {
        let start = Instant::now();
        let mut result = EvalResult {
            case_name: case.name.clone(),
            model_name: backend.model_name(),
            success: false,
            turns: 0,
            tool_calls: Vec::new(),
            error: String::new(),
            duration_ms: 0,
            passes: 0,
            repeats: case.repeats.max(1),
        };

        if let Err(err) = case.validate() {
            result.error = err;
            result.duration_ms = elapsed_ms(start);
            return result;
        }

        let mut first_error = String::new();
        for _ in 0..case.repeats {
            let outcome = run_repeat(case, backend, executor, &mut result.tool_calls);
            result.turns += outcome.turns;
            if outcome.passed {
                result.passes += 1;
            } else if first_error.is_empty() {
                first_error = outcome.error;
            }
        }

        result.success = result.passes as f64 / case.repeats as f64 >= case.min_pass_rate;
        if !result.success {
            result.error = if case.repeats == 1 {
                first_error
            } else {
                format!(
                    "passed {}/{} repeats (minPassRate {}): {first_error}",
                    result.passes, case.repeats, case.min_pass_rate
                )
            };
        }
        result.duration_ms = elapsed_ms(start);
        result
    }
}

fn elapsed_ms(start: Instant) -> u64 {
    u64::try_from(start.elapsed().as_millis()).unwrap_or(u64::MAX)
}

fn run_repeat(
    case: &EvalCase,
    backend: &dyn Backend,
    executor: &mut dyn FnMut(&str, &Map<String, Value>) -> String,
    tool_calls: &mut Vec<RecordedToolCall>,
) -> RepeatOutcome {
    let no_call = case.expect == EXPECT_NO_CALL;
    let mut messages = vec![
        Message {
            role: Role::System,
            content: case.system_prompt.clone(),
            ..Message::default()
        },
        Message {
            role: Role::User,
            content: case.user_message.clone(),
            ..Message::default()
        },
    ];
    let mut turns = 0;

    while turns < case.max_turns {
        let request = Request {
            messages: messages.clone(),
            tools: case.tools.clone(),
            temperature: 0.0,
            max_tokens: 2048,
            ..Request::default()
        };
        let response = backend.complete(&request);
        turns += 1;
        let response = match response {
            Ok(response) => response,
            Err(err) => {
                return RepeatOutcome {
                    passed: false,
                    turns,
                    error: err,
                };
            }
        };

        if response.tool_calls.is_empty() {
            break;
        }

        for tc in response.tool_calls {
            tool_calls.push(RecordedToolCall {
                turn: turns,
                name: tc.name.clone(),
                arguments: Value::Object(tc.args.clone()).to_string(),
            });

            if tc.name == case.expected_tool {
                let matches = case
                    .expected_args
                    .as_ref()
                    .is_none_or(|expected| args_match(expected, &tc.args));
                if no_call {
                    if matches {
                        return RepeatOutcome {
                            passed: false,
                            turns,
                            error: format!("Tool '{}' was called but expect is no_call", tc.name),
                        };
                    }
                } else {
                    // Reaching the expected tool decides the repeat either way.
                    return RepeatOutcome {
                        passed: matches,
                        turns,
                        error: if matches {
                            String::new()
                        } else {
                            format!(
                                "Tool '{}' called with arguments that do not match expectedArgs",
                                tc.name
                            )
                        },
                    };
                }
            }

            let content = executor(&tc.name, &tc.args);
            let id = tc.id.clone();
            messages.push(Message {
                role: Role::Assistant,
                tool_calls: vec![tc],
                ..Message::default()
            });
            messages.push(Message {
                role: Role::Tool,
                content,
                tool_call_id: id,
                ..Message::default()
            });
        }
    }

    if no_call {
        RepeatOutcome {
            passed: true,
            turns,
            error: String::new(),
        }
    } else {
        RepeatOutcome {
            passed: false,
            turns,
            error: format!(
                "Expected tool '{}' not called in {turns} turns",
                case.expected_tool
            ),
        }
    }
}
