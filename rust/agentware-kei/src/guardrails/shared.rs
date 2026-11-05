//! Minimal response-quality guardrails facade, matching the shared v1 contract.
//!
//! These checks do not authorize calls or execute tools. Integrators must
//! enforce their separate policy and audit boundary. One instance is per run.
use regex::Regex;
use serde_json::{Map, Value, json};

/// V1 guardrail facade using explicit configuration and per-run state.
pub struct Guardrails {
    config: Value,
    state: Value,
}

impl Guardrails {
    /// Construct a facade from version 1 configuration.
    pub fn new(config: Value) -> Result<Self, &'static str> {
        if config.get("version").and_then(Value::as_u64) != Some(1)
            || !config.get("tool_names").is_some_and(Value::is_array)
        {
            return Err("invalid guardrails configuration");
        }
        Ok(Self {
            config,
            state: json!({}),
        })
    }

    /// Check parsed tool calls; never invokes or authorizes a tool.
    pub fn check(
        &mut self,
        response: &Value,
        state: Option<&Value>,
    ) -> Result<Value, &'static str> {
        if let Some(s) = state {
            self.state = s.clone();
        }
        let calls = response
            .get("tool_calls")
            .and_then(Value::as_array)
            .ok_or("invalid tool calls")?;
        let names = self.config["tool_names"]
            .as_array()
            .ok_or("invalid tool names")?;
        if calls.is_empty() {
            return Ok(
                json!({"outcome":"retry", "nudge_kind":"retry", "nudge_text":"Respond with a tool call."}),
            );
        }
        for call in calls {
            if !call.get("args").is_some_and(Value::is_object) {
                return Ok(
                    json!({"outcome":"retry", "nudge_kind":"retry", "nudge_text":"Tool arguments must be an object."}),
                );
            }
            let name = call
                .get("tool")
                .and_then(Value::as_str)
                .ok_or("invalid tool call")?;
            if !names.iter().any(|x| x.as_str() == Some(name)) {
                return Ok(
                    json!({"outcome":"retry", "nudge_kind":"unknown_tool", "nudge_text":format!("Unknown tool: {name}")}),
                );
            }
        }
        let completed = self
            .state
            .get("completed_steps")
            .and_then(Value::as_array)
            .cloned()
            .unwrap_or_default();
        if self
            .config
            .get("terminal_tool")
            .is_some_and(|terminal| calls.iter().any(|call| call.get("tool") == Some(terminal)))
        {
            let missing: Vec<Value> = self
                .config
                .get("required_steps")
                .and_then(Value::as_array)
                .cloned()
                .unwrap_or_default()
                .into_iter()
                .filter(|step| !completed.iter().any(|c| c.get("tool") == Some(step)))
                .collect();
            if !missing.is_empty() {
                return Ok(
                    json!({"outcome":"step_blocked", "missing_steps":missing, "nudge_kind":"step", "nudge_text":"Complete the required steps before finishing."}),
                );
            }
        }
        let prereqs = self
            .config
            .get("prerequisites")
            .and_then(Value::as_array)
            .cloned()
            .unwrap_or_default();
        for call in calls {
            for prerequisite in &prereqs {
                if call.get("tool") != prerequisite.get("tool") {
                    continue;
                }
                let required = prerequisite
                    .get("requires")
                    .and_then(Value::as_str)
                    .ok_or("invalid prerequisite")?;
                let match_arg = prerequisite.get("match_arg").and_then(Value::as_str);
                let found = completed.iter().any(|entry| {
                    entry.get("tool").and_then(Value::as_str) == Some(required)
                        && match_arg.is_none_or(|key| {
                            match (
                                entry.get("args").and_then(|a| a.get(key)),
                                call.get("args").and_then(|a| a.get(key)),
                            ) {
                                (Some(left), Some(right))
                                    if !left.is_null() && !right.is_null() =>
                                {
                                    left == right
                                }
                                _ => false,
                            }
                        })
                });
                if !found {
                    let violations = self
                        .state
                        .get("prereq_violations")
                        .and_then(Value::as_u64)
                        .unwrap_or(0)
                        + 1;
                    self.state
                        .as_object_mut()
                        .ok_or("invalid state")?
                        .insert("prereq_violations".into(), json!(violations));
                    let max = self
                        .config
                        .get("max_prereq_violations")
                        .and_then(Value::as_u64)
                        .unwrap_or(2);
                    return Ok(
                        json!({"outcome":if violations > max {"fatal"} else {"retry"},
                        "missing_prerequisites":[required], "nudge_kind":"prerequisite", "prereq_violations":violations, "nudge_text":format!("Complete {required} before retrying.")}),
                    );
                }
            }
        }
        Ok(json!({"outcome":"execute", "tool_calls":calls}))
    }

    /// Rescue supported JSON, rehearsal, Qwen XML, and Mistral tool calls.
    ///
    /// This parser treats text as data and never evaluates model output as code.
    /// All rescued calls still pass the ordinary quality checks and must pass
    /// the caller's independent authorization boundary.
    pub fn check_text(&mut self, text: &str) -> Result<Value, &'static str> {
        let think =
            Regex::new(r"(?is)<think>.*?</think>|\[think\].*?\[/think\]").expect("static regex");
        let cleaned = think.replace_all(text, "");
        let mut calls = Vec::new();
        let tagged = cleaned.contains("[TOOL_CALLS]")
            || cleaned.contains("[ARGS]")
            || cleaned.contains("<function=");
        for candidate in if tagged {
            Vec::new()
        } else {
            json_objects(&cleaned)
        } {
            if let Ok(Value::Object(object)) = serde_json::from_str::<Value>(candidate) {
                let name = object
                    .get("tool")
                    .or_else(|| object.get("name"))
                    .and_then(Value::as_str);
                let args = object
                    .get("args")
                    .or_else(|| object.get("arguments"))
                    .cloned()
                    .unwrap_or_else(|| json!({}));
                if let Some(name) = name.filter(|_| args.is_object()) {
                    calls.push(json!({"tool":name,"args":args}));
                }
            }
        }
        if calls.is_empty() {
            let rehearsal = Regex::new(r"([A-Za-z0-9_]+)\[ARGS\]\s*").expect("static regex");
            for capture in rehearsal.captures_iter(&cleaned) {
                let whole = capture.get(0).expect("whole match");
                let tail = cleaned[whole.end()..].trim_start();
                if !tail.starts_with('{') {
                    continue;
                }
                if let Some(candidate) = json_objects(tail).first() {
                    if let Ok(args) = serde_json::from_str::<Value>(candidate) {
                        if args.is_object() {
                            calls.push(json!({"tool":&capture[1],"args":args}));
                        }
                    }
                }
            }
        }
        if calls.is_empty() {
            let function =
                Regex::new(r"(?s)<function=([^>\s]+)>(.*?)</function>").expect("static regex");
            let parameter =
                Regex::new(r"(?s)<parameter=([^>\s]+)>(.*?)</parameter>").expect("static regex");
            for capture in function.captures_iter(&cleaned) {
                let mut args = Map::new();
                for parameter in parameter.captures_iter(&capture[2]) {
                    let value = parameter[2].strip_prefix('\n').unwrap_or(&parameter[2]);
                    let value = value.strip_suffix('\n').unwrap_or(value);
                    args.insert(parameter[1].to_owned(), json!(value));
                }
                calls.push(json!({"tool":&capture[1],"args":args}));
            }
        }
        if calls.is_empty() {
            let mistral = Regex::new(r"\[TOOL_CALLS\]([A-Za-z0-9_]+)\s*").expect("static regex");
            for capture in mistral.captures_iter(&cleaned) {
                let whole = capture.get(0).expect("whole match");
                let tail = cleaned[whole.end()..].trim_start();
                if !tail.starts_with('{') {
                    continue;
                }
                if let Some(candidate) = json_objects(tail).first() {
                    if let Ok(args) = serde_json::from_str::<Value>(candidate) {
                        if args.is_object() {
                            calls.push(json!({"tool":&capture[1],"args":args}));
                        }
                    }
                }
            }
        }
        if calls.is_empty() {
            return Ok(json!({"outcome":"retry", "nudge_kind":"retry"}));
        }
        self.check(&json!({"tool_calls":calls}), None)
    }

    /// Record a successful call or categorized error, after the caller executes it.
    pub fn record(&mut self, record: &Value, state: Option<&Value>) -> Result<Value, &'static str> {
        if let Some(s) = state {
            self.state = s.clone();
        }
        let name = record
            .get("tool")
            .and_then(Value::as_str)
            .ok_or("invalid record")?;
        let state = self.state.as_object_mut().ok_or("invalid state")?;
        if record.get("error").is_none() && record.get("category").is_none() {
            let completed = state
                .entry("completed_steps")
                .or_insert_with(|| Value::Array(vec![]))
                .as_array_mut()
                .ok_or("invalid state")?;
            completed.push(json!({"tool":name, "args":record.get("args").cloned().unwrap_or_else(|| json!({}))}));
            return Ok(json!({"outcome":"recorded", "completed_steps":completed}));
        }
        let errors = state
            .entry("tool_errors")
            .or_insert_with(|| Value::Array(vec![]))
            .as_array_mut()
            .ok_or("invalid state")?;
        errors.push(json!({"tool":name,"category":record.get("category").cloned().unwrap_or_else(|| json!("unknown"))}));
        let mut output = Map::new();
        output.insert("outcome".into(), json!("recorded"));
        output.insert("tool_errors".into(), json!(errors));
        if let Some(text) = self.config.get("nudges").and_then(|n| n.get("retry")) {
            output.insert("nudge_text".into(), text.clone());
        }
        Ok(Value::Object(output))
    }
}

// Balanced JSON objects, respecting string quotes and escaped braces. Byte
// indices are safe because JSON structural delimiters are ASCII.
fn json_objects(text: &str) -> Vec<&str> {
    let mut found = Vec::new();
    let (mut start, mut depth) = (0usize, 0usize);
    let (mut quoted, mut escape) = (false, false);
    for (index, byte) in text.bytes().enumerate() {
        if depth == 0 {
            if byte == b'{' {
                start = index;
                depth = 1;
                quoted = false;
                escape = false;
            }
            continue;
        }
        if escape {
            escape = false;
            continue;
        }
        if quoted && byte == b'\\' {
            escape = true;
            continue;
        }
        if byte == b'"' {
            quoted = !quoted;
            continue;
        }
        if quoted {
            continue;
        }
        if byte == b'{' {
            depth += 1;
        } else if byte == b'}' {
            depth -= 1;
            if depth == 0 {
                found.push(&text[start..=index]);
            }
        }
    }
    found
}
