//! MiniMax M2 and M3 tool calls, as the official chat templates render them.
//!
//! M2 wraps `<invoke name="…">` blocks of `<parameter name="…">value</parameter>`
//! tags in a `minimax:tool_call` block. M3 prefixes every structural tag with
//! [`MINIMAX_M3_NAMESPACE`] and writes parameters as nested elements. The
//! version picks the tool-definition prompt; parsing and validation accept
//! both syntaxes, and a tool result is a `response` block in both. Parameter
//! values are not coerced to the tool schema: they arrive as strings (M3
//! nested elements as objects, repeated names as lists), the same as vLLM's
//! parsers without a request schema. Mirrors `go/toolformat/minimax.go`.

use std::sync::LazyLock;

use regex::Regex;
use serde_json::{Map, Value, json};

use super::{Formatter, ParsedToolCall, ToolDef, ToolResult};

/// The marker MiniMax M3 puts before every structural tag of a tool call.
pub const MINIMAX_M3_NAMESPACE: &str = "]<]minimax[>[";

/// Selects the MiniMax tool-definition prompt.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub enum MiniMaxVersion {
    /// Parameter tags (the default).
    #[default]
    M2,
    /// Namespaced, nested elements.
    M3,
}

/// Formats tools and parses tool calls for MiniMax M2 and M3.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub struct MiniMaxFormatter {
    pub version: MiniMaxVersion,
}

impl MiniMaxFormatter {
    /// A formatter that renders the given version's prompt.
    pub fn new(version: MiniMaxVersion) -> Self {
        Self { version }
    }
}

const MM2_BLOCK: &str = "<minimax:tool_call>";
const MM2_INVOKE_OPEN: &str = "<invoke name=";
const MM3_MAX_DEPTH: usize = 128;
const MM3_MIXED_TEXT: &str = "$text";
const MM_TOOLS_HEADER: &str = "# Tools\nYou may call one or more tools to assist with the user query.\nHere are the tools available in JSONSchema format:\n\n<tools>\n";
const MM2_INSTRUCTIONS: &str = "When making tool calls, use XML format to invoke tools and pass parameters:\n\n<minimax:tool_call>\n<invoke name=\"tool-name-1\">\n<parameter name=\"param-key-1\">param-value-1</parameter>\n<parameter name=\"param-key-2\">param-value-2</parameter>\n...\n</invoke>\n</minimax:tool_call>";

fn ns(tag: &str) -> String {
    format!("{MINIMAX_M3_NAMESPACE}{tag}")
}

fn mm3_instructions() -> String {
    let n = MINIMAX_M3_NAMESPACE;
    format!(
        "To call tools, wrap all invocations in a single {n}<tool_call>{n}</tool_call> block. \
         Parameter values containing nested objects or arrays are recursively expanded into XML elements. Example:\n\n\
         {n}<tool_call>\n\
         {n}<invoke name=\"tool-name-1\">{n}<param-1>value-1{n}</param-1>{n}<param-2>{n}<item>\
         {n}<key-a>val-a{n}</key-a>{n}<key-b>val-b{n}</key-b>{n}</item>{n}</param-2>{n}</invoke>\n\
         {n}<invoke name=\"tool-name-2\">{n}<param-1>value-1{n}</param-1>{n}</invoke>\n\
         {n}</tool_call>"
    )
}

static MM2_INVOKE_RE: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r#"(?s)<invoke name=(?:"([^"]*)"|'([^']*)'|([^>\s]*))>(.*?)</invoke>"#)
        .expect("valid minimax invoke regex")
});
static MM2_PARAM_OPEN_RE: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r#"<\s*parameter\s+name\s*=\s*(?:"([^"]*)"|'([^']*)'|([^>\s]+))\s*>"#)
        .expect("valid minimax parameter regex")
});
static MM2_PARAM_CLOSE_RE: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r"<\s*/\s*parameter\s*>").expect("valid minimax parameter close regex")
});

fn sorted(value: Value) -> Value {
    match value {
        Value::Object(m) => {
            let mut entries: Vec<_> = m.into_iter().collect();
            entries.sort_by(|a, b| a.0.cmp(&b.0));
            Value::Object(entries.into_iter().map(|(k, v)| (k, sorted(v))).collect())
        }
        Value::Array(a) => Value::Array(a.into_iter().map(sorted).collect()),
        other => other,
    }
}

fn first_group(c: &regex::Captures<'_>, groups: &[usize]) -> String {
    groups
        .iter()
        .find_map(|&i| c.get(i).map(|m| m.as_str()).filter(|s| !s.is_empty()))
        .unwrap_or_default()
        .trim()
        .to_owned()
}

struct Invoke {
    name: String,
    args: Map<String, Value>,
    raw: String,
    complete: bool,
}

/// Parses M2 parameter tags; the flag is false if one was dropped
/// unterminated. A value runs to its closing tag or, as in vLLM, to the next
/// parameter tag. Values stay verbatim strings.
fn mm2_params(body: &str) -> (Map<String, Value>, bool) {
    let openers: Vec<_> = MM2_PARAM_OPEN_RE.captures_iter(body).collect();
    let mut params = Map::new();
    let mut complete = true;
    for (i, opener) in openers.iter().enumerate() {
        let next = openers.get(i + 1).map(|c| c.get(0).expect("match").start());
        let start = opener.get(0).expect("match").end();
        let region = &body[start..next.unwrap_or(body.len())];
        let value = if let Some(close) = MM2_PARAM_CLOSE_RE.find(region) {
            &region[..close.start()]
        } else if next.is_some() {
            region
        } else {
            complete = false;
            continue;
        };
        let name = first_group(opener, &[1, 2, 3]);
        if !name.is_empty() {
            params.insert(name, Value::String(value.to_owned()));
        }
    }
    (params, complete)
}

/// An M3 element value: its text, or its `(name, value)` children in order.
enum Node {
    Text(String),
    Children(Vec<(String, Node)>),
}

/// Parses the M3 element starting at `pos`; `None` if it is malformed.
fn mm3_element(text: &str, pos: usize, depth: usize) -> Option<(String, Node, usize)> {
    if depth > MM3_MAX_DEPTH {
        return None;
    }
    let element = ns("<");
    let name_start = pos + element.len();
    let name_end = name_start + text[name_start..].find('>')?;
    if name_end == name_start {
        return None;
    }
    let name = &text[name_start..name_end];
    if name.starts_with('/') || name.trim().is_empty() {
        return None;
    }
    let close_tag = ns(&format!("</{name}>"));
    let mut pos = name_end + 1;
    let mut body = String::new();
    let mut children: Vec<(String, Node)> = Vec::new();
    loop {
        let marker = pos + text[pos..].find(MINIMAX_M3_NAMESPACE)?;
        body.push_str(&text[pos..marker]);
        if text[marker..].starts_with(&close_tag) {
            pos = marker + close_tag.len();
            break;
        }
        if !text[marker..].starts_with(&element) {
            return None;
        }
        let (child_name, child, next) = mm3_element(text, marker, depth + 1)?;
        children.push((child_name, child));
        pos = next;
    }
    if children.is_empty() {
        return Some((name.to_owned(), Node::Text(body), pos));
    }
    if !body.trim().is_empty() {
        let mut field = MM3_MIXED_TEXT.to_owned();
        while children.iter().any(|(n, _)| *n == field) {
            field.insert(0, '$');
        }
        children.push((field, Node::Text(body)));
    }
    Some((name.to_owned(), Node::Children(children), pos))
}

/// Parses an M3 invoke body; the flag is false if parsing stopped at a
/// malformed element (the elements before it are kept).
fn mm3_params(body: &str) -> (Vec<(String, Node)>, bool) {
    let element = ns("<");
    let mut params = Vec::new();
    let mut pos = 0;
    loop {
        while let Some(c) = body[pos..].chars().next().filter(|c| c.is_whitespace()) {
            pos += c.len_utf8();
        }
        if pos == body.len() {
            return (params, true);
        }
        if !body[pos..].starts_with(&element) {
            return (params, false);
        }
        match mm3_element(body, pos, 1) {
            Some((name, node, next)) => {
                params.push((name, node));
                pos = next;
            }
            None => return (params, false),
        }
    }
}

/// Elements to an object without a schema; a repeated name collects a list.
fn mm3_object(elements: Vec<(String, Node)>) -> Map<String, Value> {
    let mut result = Map::new();
    let mut repeated: Vec<String> = Vec::new();
    for (name, node) in elements {
        let value = match node {
            Node::Text(text) => Value::String(text),
            Node::Children(children) => Value::Object(mm3_object(children)),
        };
        match result.get_mut(&name) {
            None => {
                result.insert(name, value);
            }
            Some(Value::Array(list)) if repeated.contains(&name) => list.push(value),
            Some(existing) => {
                let first = existing.take();
                *existing = Value::Array(vec![first, value]);
                repeated.push(name);
            }
        }
    }
    result
}

fn mm3_invokes(response: &str) -> (Vec<Invoke>, Option<String>) {
    let tool_call = ns("<tool_call>");
    let invoke_open = ns("<invoke name=");
    let invoke_end = ns("</invoke>");
    let Some(start) = response.find(&tool_call) else {
        if response.contains(&invoke_open) {
            return (
                Vec::new(),
                Some("MiniMax M3 invoke outside a tool_call block".into()),
            );
        }
        return (Vec::new(), None);
    };
    let mut invokes = Vec::new();
    let mut pos = start + tool_call.len();
    while let Some(rel) = response[pos..].find(&invoke_open) {
        let begin = pos + rel;
        let mut name_start = begin + invoke_open.len();
        let quote = response[name_start..]
            .chars()
            .next()
            .filter(|c| *c == '"' || *c == '\'');
        let found = match quote {
            Some(q) => {
                name_start += 1;
                response[name_start..]
                    .find(&format!("{q}>"))
                    .map(|i| (name_start + i, name_start + i + 2))
            }
            None => response[name_start..]
                .find('>')
                .map(|i| (name_start + i, name_start + i + 1)),
        };
        let Some((name_end, body_start)) = found else {
            return (invokes, Some("unterminated MiniMax invoke".into()));
        };
        let Some(end_rel) = response[body_start..].find(&invoke_end) else {
            return (invokes, Some("unterminated MiniMax invoke".into()));
        };
        let end = body_start + end_rel;
        let (params, complete) = mm3_params(&response[body_start..end]);
        pos = end + invoke_end.len();
        invokes.push(Invoke {
            name: response[name_start..name_end].trim().to_owned(),
            args: mm3_object(params),
            raw: response[begin..pos].to_owned(),
            complete,
        });
    }
    if invokes.is_empty() {
        return (
            Vec::new(),
            Some("MiniMax tool_call block has no invoke".into()),
        );
    }
    (invokes, None)
}

/// M2 invokes anywhere in the response, as vLLM finds them.
fn mm2_invokes(response: &str) -> (Vec<Invoke>, Option<String>) {
    let invokes: Vec<Invoke> = MM2_INVOKE_RE
        .captures_iter(response)
        .map(|c| {
            let (args, complete) = mm2_params(&c[4]);
            Invoke {
                name: first_group(&c, &[1, 2, 3]),
                args,
                raw: c[0].to_owned(),
                complete,
            }
        })
        .collect();
    if response.matches(MM2_INVOKE_OPEN).count() != invokes.len() {
        return (invokes, Some("unterminated MiniMax invoke".into()));
    }
    if invokes.is_empty() && response.contains(MM2_BLOCK) {
        return (
            Vec::new(),
            Some("MiniMax tool_call block has no invoke".into()),
        );
    }
    (invokes, None)
}

/// A response with the M3 namespace marker is M3; any other is M2.
fn invokes(response: &str) -> (Vec<Invoke>, Option<String>) {
    if response.contains(MINIMAX_M3_NAMESPACE) {
        mm3_invokes(response)
    } else {
        mm2_invokes(response)
    }
}

impl Formatter for MiniMaxFormatter {
    fn format_tools(&self, tools: &[ToolDef]) -> String {
        if tools.is_empty() {
            return String::new();
        }
        let lines: Vec<String> = tools
            .iter()
            .map(|t| {
                let mut f = Map::new();
                f.insert("name".into(), json!(t.name));
                f.insert("description".into(), json!(t.description));
                if let Some(s) = &t.input_schema {
                    f.insert("parameters".into(), json!(s));
                }
                format!("<tool>{}</tool>", sorted(Value::Object(f)))
            })
            .collect();
        let instructions = match self.version {
            MiniMaxVersion::M2 => MM2_INSTRUCTIONS.to_owned(),
            MiniMaxVersion::M3 => mm3_instructions(),
        };
        format!(
            "{MM_TOOLS_HEADER}{}\n</tools>\n\n{instructions}",
            lines.join("\n")
        )
    }

    /// Returns the well-formed calls; an invoke without a name is skipped.
    /// Format errors are reported by `validate_format`, not here.
    fn parse_tool_calls(&self, response: &str) -> Result<Vec<ParsedToolCall>, String> {
        Ok(invokes(response)
            .0
            .into_iter()
            .filter(|i| !i.name.is_empty())
            .map(|i| ParsedToolCall {
                id: String::new(),
                name: i.name,
                args: i.args,
                raw: i.raw,
            })
            .collect())
    }

    /// Output goes in verbatim: both chat templates emit a tool message's
    /// content unchanged, and structured data is already JSON text. A failure
    /// renders as `{"error": ...}`.
    fn format_tool_result(&self, _name: &str, result: &ToolResult) -> String {
        if result.success {
            format!("<response>{}</response>", result.output)
        } else {
            format!("<response>{}</response>", json!({"error": result.error}))
        }
    }

    fn model_family(&self) -> &'static str {
        "minimax"
    }

    fn validate_format(&self, response: &str) -> Result<(), String> {
        let (invokes, error) = invokes(response);
        if let Some(error) = error {
            return Err(error);
        }
        for invoke in invokes {
            if invoke.name.is_empty() {
                return Err("MiniMax invoke missing tool name".into());
            }
            if !invoke.complete {
                return Err(format!(
                    "MiniMax invoke {:?} has a malformed or unterminated parameter",
                    invoke.name
                ));
            }
        }
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::toolformat::for_model;

    #[test]
    fn empty_tool_list_renders_nothing() {
        for version in [MiniMaxVersion::M2, MiniMaxVersion::M3] {
            assert_eq!(MiniMaxFormatter::new(version).format_tools(&[]), "");
        }
    }

    #[test]
    fn default_is_m2() {
        assert_eq!(MiniMaxFormatter::default().version, MiniMaxVersion::M2);
    }

    #[test]
    fn m3_parse_keeps_nested_values_as_json() {
        let n = MINIMAX_M3_NAMESPACE;
        let text = format!(
            "{n}<tool_call>\n{n}<invoke name=\"t\">{n}<a>{n}<b>x{n}</b>{n}</a>{n}</invoke>\n{n}</tool_call>"
        );
        let calls = MiniMaxFormatter::default().parse_tool_calls(&text).unwrap();
        assert_eq!(calls.len(), 1);
        assert_eq!(
            Value::Object(calls[0].args.clone()),
            json!({"a": {"b": "x"}})
        );
    }

    #[test]
    fn routes_minimax_ids() {
        for (model, m3) in [
            ("MiniMax-M2", false),
            ("MiniMaxAI/MiniMax-M2.5", false),
            ("minimax-m2.1", false),
            ("MiniMax-M3", true),
            ("MiniMaxAI/MiniMax-M3", true),
            ("minimax-m3-preview", true),
        ] {
            let formatter = for_model(model);
            assert_eq!(formatter.model_family(), "minimax", "{model}");
            let tool = ToolDef {
                name: "t".into(),
                description: "d".into(),
                input_schema: None,
            };
            let prompt = formatter.format_tools(&[tool]);
            assert_eq!(prompt.contains(MINIMAX_M3_NAMESPACE), m3, "{model}");
        }
        for model in ["MiniMax-Text-01", "minimax-7b", "MiniMax-M1"] {
            assert_ne!(for_model(model).model_family(), "minimax", "{model}");
        }
    }
}
