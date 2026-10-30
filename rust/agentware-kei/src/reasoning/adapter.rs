//! The model-agnostic reasoning adapter (port of `go/reasoning/adapter.go`).
//!
//! The adapter recognizes three shapes of model output:
//!
//!   - native reasoning fields (DeepSeek/Qwen/Kimi/GLM-style JSON)
//!   - generic thinking tags (`<thinking>...</thinking>`, `[THINK]...[/THINK]`)
//!   - model-specific reasoning fields (registered per model name)
//!
//! Reasoning is stripped from the model output before ordinary tool-call
//! parsing, and the extracted reasoning text is normalized into a versioned
//! context tree. Raw reasoning is never exposed in normal replies or default
//! audits: tree nodes carry only bounded summaries.
//!
//! The adapter fails closed. Malformed input (a reasoning field that is not a
//! string, unbalanced thinking tags, malformed JSON) and unbounded input
//! (reasoning larger than the configured byte cap, more nodes or more depth
//! than configured) return errors instead of emitting partial reasoning.

use crate::reasoning::options::Options;
use crate::reasoning::types::{CONTRACT_VERSION, ContextTree, Format};
use regex::Regex;
use serde_json::{Map, Value};
use std::collections::HashMap;
use std::fmt;
use std::sync::LazyLock;

// Go's RE2 `\s` is ASCII-only; Rust's is Unicode by default. Spell the ASCII
// class out so both ports classify identical inputs identically.
static THINKING_OPEN: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"(?i)<thinking>|\[THINK\]").expect("valid regex"));

static THINKING_CLOSE: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"(?i)</thinking>|\[/THINK\]").expect("valid regex"));

/// Fail-closed outcomes of the reasoning adapter (mirrors Go's sentinel
/// errors). Malformed or unbounded input never produces a partial tree or
/// partially-stripped content.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ReasoningError {
    /// The raw input has no usable shape.
    MalformedInput(String),
    /// Input that looks like JSON cannot be decoded.
    MalformedJSON(String),
    /// A reasoning field is not a string (e.g. reasoning_content is an object
    /// or array).
    MalformedReasoningField(String),
    /// A structured content field is neither a string nor null.
    MalformedContent(String),
    /// A thinking tag is opened but never closed, so the rest of the output
    /// cannot be trusted.
    UnbalancedThinking,
    /// The extracted reasoning exceeds the configured byte cap (unbounded
    /// input).
    ReasoningTooLarge,
    /// The reasoning would produce more nodes than configured.
    TooManyNodes,
    /// The reasoning would produce a deeper chain than configured.
    TooDeep,
}

impl fmt::Display for ReasoningError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::MalformedInput(detail) => write!(f, "reasoning: malformed input: {detail}"),
            Self::MalformedJSON(detail) => write!(f, "reasoning: malformed JSON: {detail}"),
            Self::MalformedReasoningField(detail) => {
                write!(f, "reasoning: reasoning field is not a string: {detail}")
            }
            Self::MalformedContent(detail) => {
                write!(f, "reasoning: content field is not a string: {detail}")
            }
            Self::UnbalancedThinking => write!(f, "reasoning: unbalanced thinking tag"),
            Self::ReasoningTooLarge => write!(f, "reasoning: reasoning exceeds maximum size"),
            Self::TooManyNodes => write!(f, "reasoning: too many reasoning nodes"),
            Self::TooDeep => write!(f, "reasoning: reasoning tree too deep"),
        }
    }
}

impl std::error::Error for ReasoningError {}

/// The shared result of inspecting a raw model output: the extracted
/// reasoning text, the clean content left after stripping, and the input
/// format that produced them.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Analysis {
    pub reasoning: String,
    pub clean: String,
    pub format: Format,
}

/// A model-agnostic reasoning adapter. It is safe for concurrent use after
/// construction; model field registration is read-only afterwards.
#[derive(Debug, Clone)]
pub struct Adapter {
    pub(crate) opts: Options,
    model_field: HashMap<String, String>,
}

/// Field names recognized as reasoning on a structured response, in priority
/// order. `reasoning_content` is the de-facto native field (DeepSeek, Qwen/
/// QwQ, Kimi, GLM); the others cover common model-specific names.
const KNOWN_REASONING_FIELDS: [&str; 4] = [
    "reasoning_content",
    "thinking",
    "thinking_content",
    "reasoning",
];

/// Keys on a structured response that make it worth entering the structured
/// path. A mapping without any of these is treated as opaque text and
/// returned unchanged.
const RECOGNIZED_KEYS: [&str; 6] = [
    "message",
    "content",
    "reasoning_content",
    "thinking",
    "thinking_content",
    "reasoning",
];

impl Default for Adapter {
    fn default() -> Self {
        Self::new()
    }
}

impl Adapter {
    /// Returns a reasoning adapter with the default options.
    pub fn new() -> Self {
        Self::with_options(Options::default())
    }

    /// Returns a reasoning adapter with the given options (defaults applied
    /// for zero-valued limits).
    pub fn with_options(options: Options) -> Self {
        Self {
            opts: options.normalize(),
            model_field: HashMap::new(),
        }
    }

    /// Maps a model name to a model-specific reasoning field name. Fields
    /// registered here are consulted before the known field set, so callers
    /// can teach the adapter names like Spark's thinking payload field.
    pub fn register_model_field(&mut self, model: &str, field: &str) {
        if !model.is_empty() && !field.is_empty() {
            self.model_field
                .insert(model.to_lowercase(), field.to_owned());
        }
    }

    /// Returns a copy of the adapter's effective options.
    pub fn options(&self) -> Options {
        self.opts
    }

    /// Removes reasoning from raw and returns the clean content that ordinary
    /// tool-call parsers should see. It fails closed: malformed or unbounded
    /// input returns an error instead of partially-stripped output.
    pub fn strip(&self, raw: &Value) -> Result<String, ReasoningError> {
        Ok(self.analyze(raw, "")?.clean)
    }

    /// Normalizes reasoning found in raw into a versioned context tree. Raw
    /// reasoning is never stored on the tree: nodes carry bounded summaries.
    /// The tree has no nodes (root_id empty) when raw contains no reasoning.
    pub fn extract(
        &self,
        raw: &Value,
        model: &str,
        backend: &str,
    ) -> Result<ContextTree, ReasoningError> {
        let analysis = self.analyze(raw, model)?;
        self.build_tree(&analysis, model, backend)
    }

    /// Strip and extract in one call: it returns the clean content and the
    /// normalized context tree for raw.
    pub fn parse(
        &self,
        raw: &Value,
        model: &str,
        backend: &str,
    ) -> Result<(String, ContextTree), ReasoningError> {
        let analysis = self.analyze(raw, model)?;
        let tree = self.build_tree(&analysis, model, backend)?;
        Ok((analysis.clean, tree))
    }

    /// Inspects raw and splits it into reasoning text and clean content.
    ///
    /// This is the seam shared by [`strip`](Self::strip),
    /// [`extract`](Self::extract) and [`parse`](Self::parse); it is public so
    /// the shared-fixture runner (`tests/reasoning_fixtures.rs`) can assert on
    /// it, mirroring Go's in-package fixture test.
    pub fn analyze(&self, raw: &Value, model: &str) -> Result<Analysis, ReasoningError> {
        match raw {
            Value::String(text) => self.analyze_string(text, model),
            Value::Object(mapping) => self.analyze_structured(mapping, model),
            _ => Err(ReasoningError::MalformedInput(format!(
                "unsupported raw type {raw}"
            ))),
        }
    }

    fn analyze_string(&self, text: &str, model: &str) -> Result<Analysis, ReasoningError> {
        let trimmed = text.trim();
        if trimmed.starts_with('{') {
            match serde_json::from_str::<Value>(trimmed) {
                Ok(parsed) => {
                    if let Value::Object(mapping) = &parsed {
                        if has_recognized_key(mapping) {
                            return self.analyze_structured(mapping, model);
                        }
                    }
                }
                Err(error) => return Err(ReasoningError::MalformedJSON(error.to_string())),
            }
        }
        self.analyze_text(text)
    }

    /// Inspects a decoded JSON object. It descends into a "message" key
    /// (OpenAI-style, including the choices[0].message nesting of a full
    /// OpenAI-compatible response) and looks for reasoning fields, then for
    /// the clean content. Content may be a string, null, or (Anthropic
    /// Messages API) a list of typed parts.
    fn analyze_structured(
        &self,
        mapping: &Map<String, Value>,
        model: &str,
    ) -> Result<Analysis, ReasoningError> {
        if let Some(Value::Object(message)) = mapping.get("message") {
            return self.analyze_structured(message, model);
        }
        if let Some(message) = first_choice_message(mapping) {
            return self.analyze_structured(message, model);
        }

        // Content first: a string, null, or a list of typed parts. Fail
        // closed on any other shape.
        let mut content = String::new();
        let mut part_reasoning = String::new();
        if let Some(value) = mapping.get("content") {
            match value {
                Value::Null => {}
                Value::String(text) => content = text.clone(),
                Value::Array(parts) => {
                    let (text, reasoning) = extract_content_parts(parts)?;
                    content = text;
                    part_reasoning = reasoning;
                }
                other => {
                    return Err(ReasoningError::MalformedContent(format!(
                        "got {}",
                        json_type_name(other)
                    )));
                }
            }
        }

        let (mut reasoning, mut format) = self.extract_reasoning_field(mapping, model)?;
        // A native reasoning field takes priority; otherwise use the reasoning
        // carried by Anthropic-style thinking parts.
        if reasoning.is_empty() && !part_reasoning.is_empty() {
            reasoning = part_reasoning;
            format = Format::Native;
        }

        // A native reasoning field means the content is already clean, but
        // thinking tags may still be embedded in it.
        if !reasoning.is_empty() {
            let clean = strip_thinking_tags(&content)?;
            return Ok(Analysis {
                reasoning,
                clean,
                format,
            });
        }

        // No native reasoning field: fall back to thinking tags in the
        // content.
        let (clean, tags) = split_thinking_tags(&content)?;
        if !tags.is_empty() {
            return Ok(Analysis {
                reasoning: tags,
                clean,
                format: Format::Thinking,
            });
        }
        Ok(Analysis {
            reasoning: String::new(),
            clean: content,
            format: Format::None,
        })
    }

    /// Pulls the reasoning text out of a structured object, consulting the
    /// registered model-specific field first, then the known field set in
    /// priority order. It returns the detected format.
    fn extract_reasoning_field(
        &self,
        mapping: &Map<String, Value>,
        model: &str,
    ) -> Result<(String, Format), ReasoningError> {
        if !model.is_empty() {
            if let Some(field) = self.model_field.get(&model.to_lowercase()) {
                if !field.is_empty() {
                    if let Some(value) = mapping.get(field) {
                        if !value.is_null() {
                            match value.as_str() {
                                Some(text) if !text.is_empty() => {
                                    return Ok((text.to_owned(), Format::ModelSpecific));
                                }
                                Some(_) => {}
                                None => {
                                    return Err(ReasoningError::MalformedReasoningField(format!(
                                        "field {field:?} is {}",
                                        json_type_name(value)
                                    )));
                                }
                            }
                        }
                    }
                }
            }
        }
        for field in KNOWN_REASONING_FIELDS {
            let Some(value) = mapping.get(field) else {
                continue;
            };
            if value.is_null() {
                continue;
            }
            match value.as_str() {
                Some(text) if !text.is_empty() => return Ok((text.to_owned(), Format::Native)),
                Some(_) => {}
                None => {
                    return Err(ReasoningError::MalformedReasoningField(format!(
                        "field {field:?} is {}",
                        json_type_name(value)
                    )));
                }
            }
        }
        Ok((String::new(), Format::None))
    }

    /// Inspects plain text for generic thinking tags.
    fn analyze_text(&self, text: &str) -> Result<Analysis, ReasoningError> {
        let (clean, tags) = split_thinking_tags(text)?;
        if !tags.is_empty() {
            return Ok(Analysis {
                reasoning: tags,
                clean,
                format: Format::Thinking,
            });
        }
        Ok(Analysis {
            reasoning: String::new(),
            clean: text.to_owned(),
            format: Format::None,
        })
    }

    fn build_tree(
        &self,
        analysis: &Analysis,
        model: &str,
        backend: &str,
    ) -> Result<ContextTree, ReasoningError> {
        let now = self.opts.clock();
        let mut tree = ContextTree {
            version: CONTRACT_VERSION.to_owned(),
            format: analysis.format,
            model: model.to_owned(),
            backend: backend.to_owned(),
            created_at: now,
            root_id: String::new(),
            nodes: Vec::new(),
        };

        if analysis.reasoning.trim().is_empty() {
            return Ok(tree);
        }

        let nodes = self.parse_nodes(&analysis.reasoning, now)?;
        tree.root_id = nodes.first().map_or(String::new(), |node| node.id.clone());
        tree.nodes = nodes;
        Ok(tree)
    }
}

/// Returns the message object of the first choice of a full OpenAI-compatible
/// response (`{choices: [{message: {...}}]}`), or `None` when the mapping is
/// not shaped that way.
fn first_choice_message(mapping: &Map<String, Value>) -> Option<&Map<String, Value>> {
    let choices = mapping.get("choices")?.as_array()?;
    let choice = choices.first()?.as_object()?;
    choice.get("message")?.as_object()
}

/// Splits an Anthropic Messages API content list into the clean text (joined
/// "text" parts) and the reasoning (joined "thinking" parts). Other part
/// types (tool_use, tool_result, ...) are ignored: they carry neither clean
/// text nor reasoning.
fn extract_content_parts(parts: &[Value]) -> Result<(String, String), ReasoningError> {
    let mut text_parts: Vec<&str> = Vec::new();
    let mut thinking_parts: Vec<&str> = Vec::new();
    for part in parts {
        let Some(mapping) = part.as_object() else {
            return Err(ReasoningError::MalformedContent(
                "content part is not an object".to_owned(),
            ));
        };
        match mapping.get("type").and_then(Value::as_str) {
            Some("text") => {
                let Some(text) = mapping.get("text").and_then(Value::as_str) else {
                    return Err(ReasoningError::MalformedContent(
                        "text part has no string text".to_owned(),
                    ));
                };
                text_parts.push(text);
            }
            Some("thinking") => {
                let Some(thinking) = mapping.get("thinking").and_then(Value::as_str) else {
                    return Err(ReasoningError::MalformedReasoningField(
                        "thinking part has no string thinking".to_owned(),
                    ));
                };
                thinking_parts.push(thinking);
            }
            _ => {}
        }
    }
    Ok((text_parts.join("\n"), thinking_parts.join("\n")))
}

/// Reports whether a structured object carries any key that makes it a
/// reasoning-shaped response.
fn has_recognized_key(mapping: &Map<String, Value>) -> bool {
    mapping
        .keys()
        .any(|key| RECOGNIZED_KEYS.contains(&key.as_str()))
}

/// Names a JSON value the way Go's `%T` names its decoded type, for error
/// messages.
fn json_type_name(value: &Value) -> &'static str {
    match value {
        Value::Null => "null",
        Value::Bool(_) => "bool",
        Value::Number(_) => "number",
        Value::String(_) => "string",
        Value::Array(_) => "array",
        Value::Object(_) => "object",
    }
}

/// Extracts embedded thinking blocks from text and returns the clean text and
/// the joined reasoning. It fails closed on unbalanced tags: an open tag
/// without a matching close is an error, not partial output.
fn split_thinking_tags(text: &str) -> Result<(String, String), ReasoningError> {
    if !THINKING_OPEN.is_match(text) && !THINKING_CLOSE.is_match(text) {
        return Ok((text.to_owned(), String::new()));
    }

    let mut clean = String::new();
    let mut reason = String::new();
    let mut rest = text;
    loop {
        let Some(open) = THINKING_OPEN.find(rest) else {
            // No more open tags; any remaining close tag is unbalanced.
            if THINKING_CLOSE.is_match(rest) {
                return Err(ReasoningError::UnbalancedThinking);
            }
            clean.push_str(rest);
            break;
        };
        clean.push_str(&rest[..open.start()]);
        let inner = &rest[open.end()..];
        let Some(close) = THINKING_CLOSE.find(inner) else {
            return Err(ReasoningError::UnbalancedThinking);
        };
        let body = &inner[..close.start()];
        if !reason.is_empty() {
            reason.push('\n');
        }
        reason.push_str(body);
        rest = &inner[close.end()..];
    }
    Ok((clean, reason.trim().to_owned()))
}

/// Removes thinking blocks and returns the remaining text.
fn strip_thinking_tags(text: &str) -> Result<String, ReasoningError> {
    Ok(split_thinking_tags(text)?.0)
}
