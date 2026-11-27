//! GPT-OSS Harmony format: render prompts and parse raw completions.
//!
//! GPT-OSS models are trained on the Harmony format. Over a chat-completions
//! API the server applies it; over raw `/v1/completions` the caller renders
//! the whole prompt and parses the completion text. [`HarmonyFormatter`] is
//! the [`Formatter`] for the family; [`render_harmony_prompt`] renders a whole
//! conversation; [`parse_harmony_completion`] splits completion text into
//! messages, with the analysis channel as the reasoning and the final channel
//! as the answer.
//!
//! Harmony is opt-in: [`super::for_model`] never returns
//! [`HarmonyFormatter`], so chat-completions callers keep their formatter;
//! raw-completions callers construct it themselves.
//!
//! The rendering follows the reference renderer in openai/harmony
//! (`src/encoding.rs`), including its JSON Schema to TypeScript mapping and
//! its schema key order. Port of `go/toolformat/harmony.go`; the shared
//! fixture `fixtures/toolformat/harmony-cases.json` replays openai/harmony's
//! test-data.

use super::{Formatter, ParsedToolCall, ToolDef, ToolResult};
use serde_json::{Value, json};

const START: &str = "<|start|>";
const END: &str = "<|end|>";
const MESSAGE: &str = "<|message|>";
const CHANNEL: &str = "<|channel|>";
const CONSTRAIN: &str = "<|constrain|>";
const CALL: &str = "<|call|>";
const RETURN: &str = "<|return|>";
const TERMINATORS: [&str; 3] = [END, CALL, RETURN];
const FUNCTIONS_PREFIX: &str = "functions.";
const ROLES: [&str; 5] = ["system", "developer", "user", "assistant", "tool"];

/// The content type of a JSON tool call: `<|constrain|>json`.
pub const HARMONY_JSON_CONTENT_TYPE: &str = "<|constrain|>json";
/// Stop sequences for a raw completion: the end of a final answer and the end
/// of a tool call. Servers usually strip the stop sequence from the text,
/// which the parser accepts.
pub const HARMONY_STOP_TOKENS: [&str; 2] = [RETURN, CALL];
/// The model identity GPT-OSS was trained with.
pub const DEFAULT_HARMONY_MODEL_IDENTITY: &str =
    "You are ChatGPT, a large language model trained by OpenAI.";

/// One Harmony message. `role` is system, developer, user, assistant or
/// tool. A tool message's `name` is its author, such as
/// `functions.get_weather`; for other roles a `name` renders as `role:name`.
/// `recipient` is the `to=` target (`functions.NAME` for a call).
/// `content_type` follows the channel, such as [`HARMONY_JSON_CONTENT_TYPE`].
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct HarmonyMessage {
    pub role: String,
    pub content: String,
    pub channel: String,
    pub recipient: String,
    pub content_type: String,
    pub name: String,
}

impl HarmonyMessage {
    /// A message with only a role and content.
    pub fn new(role: impl Into<String>, content: impl Into<String>) -> Self {
        Self {
            role: role.into(),
            content: content.into(),
            ..Self::default()
        }
    }
}

/// The system message. An empty field is left out of the rendering; the
/// default is what GPT-OSS expects (medium reasoning, no date).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct HarmonySystem {
    pub model_identity: String,
    pub knowledge_cutoff: String,
    pub current_date: String,
    /// low, medium, high or empty.
    pub reasoning_effort: String,
    pub valid_channels: Vec<String>,
}

impl Default for HarmonySystem {
    fn default() -> Self {
        Self {
            model_identity: DEFAULT_HARMONY_MODEL_IDENTITY.to_owned(),
            knowledge_cutoff: "2024-06".to_owned(),
            current_date: String::new(),
            reasoning_effort: "medium".to_owned(),
            valid_channels: ["analysis", "commentary", "final"]
                .map(str::to_owned)
                .to_vec(),
        }
    }
}

/// Options for [`render_harmony_prompt`].
#[derive(Debug, Clone, PartialEq)]
pub struct HarmonyPromptOptions {
    /// The system message; `None` leaves it out.
    pub system: Option<HarmonySystem>,
    /// Instructions and tools go in the developer message.
    pub instructions: String,
    pub tools: Vec<ToolDef>,
    /// Drop analysis messages before the first final message once the last
    /// assistant message is a final one.
    pub auto_drop_analysis: bool,
    /// The turn to open for sampling; `None` renders the conversation alone.
    pub next_role: Option<String>,
}

impl Default for HarmonyPromptOptions {
    fn default() -> Self {
        Self {
            system: Some(HarmonySystem::default()),
            instructions: String::new(),
            tools: Vec::new(),
            auto_drop_analysis: true,
            next_role: Some("assistant".to_owned()),
        }
    }
}

/// A parsed completion. `reasoning` joins the assistant's analysis-channel
/// messages and `content` its final-channel (or channel-less) messages, each
/// with a newline. `tool_calls` are the calls to the functions namespace
/// whose arguments are a JSON object; others are left out (fail closed), and
/// [`Formatter::validate_format`] reports them.
#[derive(Debug, Clone, Default, PartialEq)]
pub struct HarmonyCompletion {
    pub messages: Vec<HarmonyMessage>,
    pub reasoning: String,
    pub content: String,
    pub tool_calls: Vec<ParsedToolCall>,
}

/// GPT-OSS Harmony tool definitions, calls and results.
pub struct HarmonyFormatter;

impl Formatter for HarmonyFormatter {
    fn format_tools(&self, tools: &[ToolDef]) -> String {
        if tools.is_empty() {
            return String::new();
        }
        tools_section(tools)
    }

    fn parse_tool_calls(&self, response: &str) -> Result<Vec<ParsedToolCall>, String> {
        Ok(parse_harmony_completion(response).tool_calls)
    }

    /// Harmony results carry no call id; the author names the tool. A
    /// failure renders as `{"error": ...}`.
    fn format_tool_result(&self, name: &str, result: &ToolResult) -> String {
        let text = if result.success {
            result.output.clone()
        } else {
            json!({"error": result.error}).to_string()
        };
        render_message(&HarmonyMessage {
            role: "tool".to_owned(),
            content: text,
            channel: "commentary".to_owned(),
            recipient: "assistant".to_owned(),
            content_type: String::new(),
            name: format!("{FUNCTIONS_PREFIX}{name}"),
        })
    }

    fn model_family(&self) -> &'static str {
        "gpt-oss"
    }

    fn validate_format(&self, response: &str) -> Result<(), String> {
        for m in parse_messages(response) {
            let Some(name) = function_name(&m) else {
                continue;
            };
            if name.is_empty() {
                return Err("tool call missing function name".to_owned());
            }
            match serde_json::from_str::<Value>(&m.content) {
                Ok(Value::Object(_)) => {}
                Ok(_) => {
                    return Err(format!(
                        "tool call {name:?} arguments must be a JSON object"
                    ));
                }
                Err(error) => {
                    return Err(format!(
                        "tool call {name:?} has invalid JSON arguments: {error}"
                    ));
                }
            }
        }
        Ok(())
    }
}

/// The called function's name, when `m` is an assistant call to the
/// functions namespace.
fn function_name(m: &HarmonyMessage) -> Option<&str> {
    if m.role != "assistant" {
        return None;
    }
    m.recipient.strip_prefix(FUNCTIONS_PREFIX)
}

/// Render one message. An assistant message with a recipient ends with the
/// call token; every other message ends with the end token.
pub fn render_harmony_message(m: &HarmonyMessage) -> Result<String, String> {
    if !ROLES.contains(&m.role.as_str()) {
        return Err(format!("unknown Harmony role {:?}", m.role));
    }
    if m.role == "tool" && m.name.is_empty() {
        return Err(
            "a Harmony tool message needs a name, such as functions.get_weather".to_owned(),
        );
    }
    Ok(render_message(m))
}

fn render_message(m: &HarmonyMessage) -> String {
    let mut out = String::from(START);
    if m.role == "tool" {
        out.push_str(&m.name);
    } else {
        out.push_str(&m.role);
        if !m.name.is_empty() {
            out.push(':');
            out.push_str(&m.name);
        }
    }
    if !m.recipient.is_empty() && m.recipient != "all" {
        out.push_str(" to=");
        out.push_str(&m.recipient);
    }
    if !m.channel.is_empty() {
        out.push_str(CHANNEL);
        out.push_str(&m.channel);
    }
    if !m.content_type.is_empty() {
        out.push(' ');
        out.push_str(&m.content_type);
    }
    out.push_str(MESSAGE);
    out.push_str(&m.content);
    out.push_str(if m.role == "assistant" && !m.recipient.is_empty() {
        CALL
    } else {
        END
    });
    out
}

/// Render a conversation as a Harmony prompt for a raw completion: the
/// system message, a developer message holding the instructions and the
/// tools namespace when either is given, then `messages`. With
/// `auto_drop_analysis`, analysis messages before the first final message are
/// dropped once the last assistant message is a final one, as the reference
/// renderer does: the chain of thought of a finished answer is not sent back,
/// while an unfinished tool-calling turn keeps it.
pub fn render_harmony_prompt(
    messages: &[HarmonyMessage],
    opts: &HarmonyPromptOptions,
) -> Result<String, String> {
    let mut out = String::new();
    if let Some(system) = &opts.system {
        let text = system_text(system, !opts.tools.is_empty())?;
        out.push_str(&render_message(&HarmonyMessage::new("system", text)));
    }
    if !opts.instructions.is_empty() || !opts.tools.is_empty() {
        let mut sections = Vec::new();
        if !opts.instructions.is_empty() {
            sections.push("# Instructions".to_owned());
            sections.push(opts.instructions.clone());
        }
        if !opts.tools.is_empty() {
            sections.push(tools_section(&opts.tools));
        }
        out.push_str(&render_message(&HarmonyMessage::new(
            "developer",
            sections.join("\n\n"),
        )));
    }
    let keep = if opts.auto_drop_analysis {
        kept_messages(messages)
    } else {
        messages.iter().collect()
    };
    for m in keep {
        out.push_str(&render_harmony_message(m)?);
    }
    if let Some(role) = &opts.next_role {
        out.push_str(START);
        out.push_str(role);
    }
    Ok(out)
}

/// Parse completion text that follows a prompt ending in
/// `<|start|>assistant`. The first message may have no header role (the
/// prompt opened it). A message ends at an end, call or return token, or at
/// the end of the text, since servers strip the stop sequence. A call's
/// recipient is read from either header position: after the role (the
/// reference renderer) or after the channel (what the model emits).
pub fn parse_harmony_completion(text: &str) -> HarmonyCompletion {
    let messages = parse_messages(text);
    let mut reasoning = Vec::new();
    let mut content = Vec::new();
    let mut tool_calls = Vec::new();
    for m in &messages {
        if m.role != "assistant" {
            continue;
        }
        if let Some(name) = function_name(m) {
            if let Some(call) = decode_call(name, &m.content, text) {
                tool_calls.push(call);
            }
        } else if m.channel == "analysis" {
            reasoning.push(m.content.as_str());
        } else if m.channel == "final" || (m.channel.is_empty() && m.recipient.is_empty()) {
            content.push(m.content.as_str());
        }
    }
    HarmonyCompletion {
        reasoning: reasoning.join("\n"),
        content: content.join("\n"),
        tool_calls,
        messages,
    }
}

fn decode_call(name: &str, body: &str, raw: &str) -> Option<ParsedToolCall> {
    if name.is_empty() {
        return None;
    }
    match serde_json::from_str::<Value>(body) {
        Ok(Value::Object(args)) => Some(ParsedToolCall {
            id: String::new(),
            name: name.to_owned(),
            args,
            raw: raw.to_owned(),
        }),
        _ => None,
    }
}

fn parse_messages(text: &str) -> Vec<HarmonyMessage> {
    text.split(START)
        .enumerate()
        .filter(|(_, chunk)| !chunk.is_empty())
        .filter_map(|(i, chunk)| parse_chunk(chunk, i == 0))
        .collect()
}

fn parse_chunk(chunk: &str, first: bool) -> Option<HarmonyMessage> {
    let Some((header, body)) = chunk.split_once(MESSAGE) else {
        // Text before any header is a bare answer; a header with no message
        // token was cut off and carries no content.
        let has_token = [CHANNEL, CONSTRAIN]
            .iter()
            .chain(TERMINATORS.iter())
            .any(|t| chunk.contains(t));
        return (first && !has_token).then(|| HarmonyMessage::new("assistant", chunk));
    };
    let cut = TERMINATORS
        .iter()
        .filter_map(|t| body.find(t))
        .min()
        .unwrap_or(body.len());
    let mut m = HarmonyMessage::new("assistant", &body[..cut]);

    let mut header = header;
    if let Some((before, constrained)) = header.split_once(CONSTRAIN) {
        header = before;
        m.content_type = format!("{CONSTRAIN}{}", constrained.trim());
    }
    let (head, channel_part) = header.split_once(CHANNEL).unwrap_or((header, ""));
    let mut role_words = Vec::new();
    for word in head.split_whitespace() {
        match word.strip_prefix("to=") {
            Some(recipient) => m.recipient = recipient.to_owned(),
            None => role_words.push(word),
        }
    }
    let mut extra = Vec::new();
    let mut channel_words = channel_part.split_whitespace();
    if let Some(channel) = channel_words.next() {
        m.channel = channel.to_owned();
        for word in channel_words {
            match word.strip_prefix("to=") {
                Some(recipient) => m.recipient = recipient.to_owned(),
                None => extra.push(word),
            }
        }
    }
    if m.content_type.is_empty() && !extra.is_empty() {
        m.content_type = extra.join(" ");
    }
    if let Some(author) = role_words.first() {
        let (base, suffix) = author.split_once(':').unwrap_or((author, ""));
        if ROLES.contains(&base) && base != "tool" {
            m.role = base.to_owned();
            m.name = suffix.to_owned();
        } else {
            m.role = "tool".to_owned();
            m.name = (*author).to_owned();
        }
    }
    Some(m)
}

fn kept_messages(messages: &[HarmonyMessage]) -> Vec<&HarmonyMessage> {
    let last_assistant_is_final = messages
        .iter()
        .rev()
        .find(|m| m.role == "assistant")
        .is_some_and(|m| m.channel == "final");
    let first_final = messages.iter().position(|m| m.channel == "final");
    messages
        .iter()
        .enumerate()
        .filter(|(i, m)| {
            !(last_assistant_is_final
                && first_final.is_some_and(|first| *i < first)
                && m.channel == "analysis")
        })
        .map(|(_, m)| m)
        .collect()
}

fn system_text(s: &HarmonySystem, has_function_tools: bool) -> Result<String, String> {
    if !["", "low", "medium", "high"].contains(&s.reasoning_effort.as_str()) {
        return Err(format!(
            "reasoning effort must be low, medium or high, not {:?}",
            s.reasoning_effort
        ));
    }
    let mut sections = Vec::new();
    let mut top = Vec::new();
    if !s.model_identity.is_empty() {
        top.push(s.model_identity.clone());
    }
    if !s.knowledge_cutoff.is_empty() {
        top.push(format!("Knowledge cutoff: {}", s.knowledge_cutoff));
    }
    if !s.current_date.is_empty() {
        top.push(format!("Current date: {}", s.current_date));
    }
    if !top.is_empty() {
        sections.push(top.join("\n"));
    }
    if !s.reasoning_effort.is_empty() {
        sections.push(format!("Reasoning: {}", s.reasoning_effort));
    }
    if !s.valid_channels.is_empty() {
        let mut header = format!(
            "# Valid channels: {}. Channel must be included for every message.",
            s.valid_channels.join(", ")
        );
        if has_function_tools {
            header
                .push_str("\nCalls to these tools must go to the commentary channel: 'functions'.");
        }
        sections.push(header);
    }
    Ok(sections.join("\n\n"))
}

fn tools_section(tools: &[ToolDef]) -> String {
    let mut lines = vec![
        "## functions\n".to_owned(),
        "namespace functions {\n".to_owned(),
    ];
    for tool in tools {
        lines.extend(tool.description.lines().map(|line| format!("// {line}")));
        match &tool.input_schema {
            Some(schema) => lines.push(format!(
                "type {} = (_: {}) => any;\n",
                tool.name,
                typescript(&Value::Object(schema.clone()), "")
            )),
            None => lines.push(format!("type {} = () => any;\n", tool.name)),
        }
    }
    lines.push("} // namespace functions".to_owned());
    format!("# Tools\n\n{}", lines.join("\n"))
}

fn is_enum(schema: &Value) -> bool {
    schema
        .get("enum")
        .and_then(Value::as_array)
        .is_some_and(|values| !values.is_empty())
}

/// A default as the reference renders it beside a property: a string is
/// quoted unless the schema is an enum.
fn default_text(default: &Value, schema: &Value) -> String {
    match default.as_str() {
        Some(s) if is_enum(schema) => format!("default: {s}"),
        Some(s) => format!("default: \"{s}\""),
        None => format!("default: {default}"),
    }
}

fn nullable(type_text: String, schema: &Value) -> String {
    if schema
        .get("nullable")
        .and_then(Value::as_bool)
        .unwrap_or(false)
        && !type_text.contains("null")
    {
        format!("{type_text} | null")
    } else {
        type_text
    }
}

/// Port of `json_schema_to_typescript` in openai/harmony `src/encoding.rs`.
fn typescript(schema: &Value, indent: &str) -> String {
    if let Some(variants) = schema.get("oneOf").and_then(Value::as_array) {
        let mut out = String::new();
        for variant in variants {
            out.push_str(&format!("\n{indent} | "));
            out.push_str(&nullable(
                typescript(variant, &format!("{indent}   ")),
                variant,
            ));
            let mut comments = Vec::new();
            if let Some(description) = variant.get("description").and_then(Value::as_str) {
                comments.push(description.to_owned());
            }
            if let Some(default) = variant.get("default") {
                // Here the reference quotes a string default even for an enum.
                match default.as_str() {
                    Some(s) if !is_enum(variant) => comments.push(format!("default: \"{s}\"")),
                    _ => comments.push(format!("default: {default}")),
                }
            }
            if !comments.is_empty() {
                out.push_str(&format!(" // {}", comments.join(" ")));
            }
        }
        return out;
    }
    match schema.get("type") {
        Some(Value::Array(kinds)) => {
            let names: Vec<&str> = kinds
                .iter()
                .filter_map(Value::as_str)
                .map(|k| if k == "integer" { "number" } else { k })
                .collect();
            if names.is_empty() {
                "any".to_owned()
            } else {
                names.join(" | ")
            }
        }
        Some(Value::String(kind)) => match kind.as_str() {
            "object" => typescript_object(schema, indent),
            "string" => {
                let values: Vec<String> = schema
                    .get("enum")
                    .and_then(Value::as_array)
                    .map(|values| {
                        values
                            .iter()
                            .filter_map(Value::as_str)
                            .map(|v| format!("\"{v}\""))
                            .collect()
                    })
                    .unwrap_or_default();
                if values.is_empty() {
                    "string".to_owned()
                } else {
                    values.join(" | ")
                }
            }
            "number" | "integer" => "number".to_owned(),
            "boolean" => "boolean".to_owned(),
            "array" => match schema.get("items") {
                Some(items) => format!("{}[]", typescript(items, indent)),
                None => "Array<any>".to_owned(),
            },
            _ => "any".to_owned(),
        },
        _ => "any".to_owned(),
    }
}

fn typescript_object(schema: &Value, indent: &str) -> String {
    let mut out = String::new();
    if let Some(description) = schema.get("description").and_then(Value::as_str) {
        out.push_str(&format!("{indent}// {description}\n"));
    }
    out.push_str("{\n");
    if let Some(properties) = schema.get("properties").and_then(Value::as_object) {
        let required: Vec<&str> = schema
            .get("required")
            .and_then(Value::as_array)
            .map(|r| r.iter().filter_map(Value::as_str).collect())
            .unwrap_or_default();
        for (key, value) in properties {
            let optional = if required.contains(&key.as_str()) {
                ""
            } else {
                "?"
            };
            if let Some(title) = value.get("title").and_then(Value::as_str) {
                out.push_str(&format!("{indent}// {title}\n{indent}//\n"));
            }
            let has_one_of = value.get("oneOf").is_some();
            if !has_one_of {
                if let Some(description) = value.get("description").and_then(Value::as_str) {
                    out.push_str(&format!("{indent}// {description}\n"));
                }
            }
            if let Some(examples) = value.get("examples").and_then(Value::as_array) {
                if !examples.is_empty() {
                    out.push_str(&format!("{indent}// Examples:\n"));
                    for example in examples.iter().filter_map(Value::as_str) {
                        out.push_str(&format!("{indent}// - \"{example}\"\n"));
                    }
                }
            }
            if let Some(variants) = value.get("oneOf").and_then(Value::as_array) {
                out.push_str(&one_of_property(key, optional, value, variants, indent));
                continue;
            }
            out.push_str(&format!("{indent}{key}{optional}: "));
            out.push_str(&nullable(
                typescript(value, &format!("{indent}    ")),
                value,
            ));
            out.push(',');
            if !has_one_of {
                if let Some(default) = value.get("default") {
                    out.push_str(&format!(" // {}", default_text(default, value)));
                }
            }
            out.push('\n');
        }
    }
    out.push_str(&format!("{indent}}}"));
    out
}

fn one_of_property(
    key: &str,
    optional: &str,
    value: &Value,
    variants: &[Value],
    indent: &str,
) -> String {
    let mut out = String::new();
    let prop_description = value.get("description").and_then(Value::as_str);
    let skip = prop_description.is_some()
        && variants
            .first()
            .and_then(|v| v.get("description"))
            .and_then(Value::as_str)
            == prop_description;
    let mut described_above = false;
    if let (Some(description), false) = (prop_description, skip) {
        out.push_str(&format!("{indent}// {description}\n"));
        described_above = true;
    }
    if let Some(default) = value.get("default") {
        out.push_str(&format!("{indent}// {}\n", default_text(default, value)));
    }
    out.push_str(&format!("{indent}{key}{optional}:\n"));
    for (i, variant) in variants.iter().enumerate() {
        out.push_str(&format!("{indent} | "));
        out.push_str(&nullable(
            typescript(variant, &format!("{indent}   ")),
            variant,
        ));
        let mut comments = Vec::new();
        if i != 0 || !described_above {
            if let Some(description) = variant.get("description").and_then(Value::as_str) {
                if Some(description) != prop_description {
                    comments.push(description.to_owned());
                }
            }
        }
        if let Some(default) = variant.get("default") {
            comments.push(default_text(default, variant));
        }
        if !comments.is_empty() {
            out.push_str(&format!(" // {}", comments.join(" ")));
        }
        out.push('\n');
    }
    out.push_str(&format!("{indent},\n"));
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn object_without_properties_closes() {
        let schema: serde_json::Map<String, Value> =
            serde_json::from_str(r#"{"type":"object"}"#).expect("schema");
        assert_eq!(typescript(&Value::Object(schema), ""), "{\n}");
    }
}
