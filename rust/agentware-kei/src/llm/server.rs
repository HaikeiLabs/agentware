//! OpenAI-compatible HTTP backend (port of `go/llm/server.go`).
//!
//! Wire-format note: Go's `serverBackend` marshals `llm.Message` with default
//! field names, which puts PascalCase keys (`Role`, `Content`, `Meta`, ...)
//! on the wire — a bug (go/llm/server.go:41-47, go/llm/request.go:42 has no
//! json tags). This port emits the OpenAI snake_case mapping instead, cited
//! from `go/llm/proxy/convert.go` `ToOpenAIMessages`/`ToOpenAITools`:
//! messages are `{role, content}` plus `tool_call_id` when non-empty and
//! `tool_calls` (`{id, type: "function", function: {name, arguments}}`) when
//! present; `Meta` is never serialized. Inbound `tool_calls` are parsed with
//! the same mapping (`ExtractToolCall`): `id`, `function.name`, and
//! `function.arguments` (JSON string), tolerating the direct-key shape Go's
//! decoder accepts (`id`/`name`/`args`).

use super::backend::Backend;
use super::request::{Message, Request};
use super::response::{Response, TokenUsage, ToolCall};
use serde_json::{Map, Value, json};
use std::time::Duration;

/// Configuration for the OpenAI-compatible HTTP backend (Go `ServerConfig`).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ServerConfig {
    pub base_url: String,
    pub api_key: String,
    pub model: String,
    pub context_window: i64,
    /// A zero duration means no timeout, mirroring Go's zero
    /// `http.Client.Timeout`.
    pub timeout: Duration,
}

/// The OpenAI-compatible backend (Go `serverBackend`).
pub struct ServerBackend {
    config: ServerConfig,
    agent: ureq::Agent,
}

impl ServerBackend {
    /// Go `NewServerBackend`.
    pub fn new(config: ServerConfig) -> Self {
        let mut builder = ureq::Agent::config_builder().http_status_as_error(false);
        if !config.timeout.is_zero() {
            builder = builder.timeout_global(Some(config.timeout));
        }
        Self {
            config,
            agent: ureq::Agent::new_with_config(builder.build()),
        }
    }
}

/// Serializes one message to the OpenAI wire object
/// (`go/llm/proxy/convert.go` `ToOpenAIMessages`).
pub fn message_to_wire(message: &Message) -> Value {
    let mut wire = json!({
        "role": message.role.as_str(),
        "content": message.content,
    });
    if !message.tool_call_id.is_empty() {
        wire["tool_call_id"] = json!(message.tool_call_id);
    }
    if !message.tool_calls.is_empty() {
        wire["tool_calls"] =
            Value::Array(message.tool_calls.iter().map(tool_call_to_wire).collect());
    }
    wire
}

/// Serializes one tool call to the OpenAI wire object
/// (`go/llm/proxy/convert.go` `ToOpenAIMessages`; `arguments` carries the
/// args as a JSON string, as required by OpenAI-compatible chat APIs).
pub fn tool_call_to_wire(tool_call: &ToolCall) -> Value {
    let mut wire = json!({
        "id": tool_call.id,
        "type": "function",
        "function": { "name": tool_call.name, "arguments": serde_json::to_string(&tool_call.args).expect("JSON object serialization") },
    });
    if let Some(metadata) = &tool_call.metadata {
        wire["extra_content"] = metadata.clone();
    }
    wire
}

/// Serializes tool definitions to the OpenAI wire array
/// (`go/llm/proxy/convert.go` `ToOpenAITools`).
pub fn tools_to_wire(tools: &[super::request::ToolDefinition]) -> Value {
    Value::Array(
        tools
            .iter()
            .map(|tool| {
                json!({
                    "type": "function",
                    "function": {
                        "name": tool.name,
                        "description": tool.description,
                        "parameters": tool
                            .input_schema
                            .as_ref()
                            .map(|schema| Value::Object(schema.clone()))
                            .unwrap_or(Value::Null),
                    },
                })
            })
            .collect(),
    )
}

impl Backend for ServerBackend {
    fn complete(&self, req: &Request) -> Result<Response, String> {
        let mut payload = json!({
            "model": self.config.model,
            "messages": Value::Array(req.messages.iter().map(message_to_wire).collect()),
            "temperature": req.temperature,
            "max_tokens": req.max_tokens,
            "stop": req.stop
                .as_ref()
                .map(|stop| json!(stop))
                .unwrap_or(Value::Null),
        });
        if !req.tools.is_empty() {
            payload["tools"] = tools_to_wire(&req.tools);
        }
        if let Some(thinking) = &req.thinking {
            payload["thinking"] = json!({
                "type": thinking.r#type,
                "max_tokens": thinking.max_tokens,
            });
        }

        let body = serde_json::to_vec(&payload)
            .map_err(|error| format!("failed to marshal request: {error}"))?;

        // Go trims a trailing "/v1" twice, then appends "/v1/chat/completions".
        let mut base = self.config.base_url.as_str();
        if let Some(stripped) = base.strip_suffix("/v1") {
            base = stripped;
        }
        if let Some(stripped) = base.strip_suffix("/v1") {
            base = stripped;
        }
        let url = format!("{base}/v1/chat/completions");

        let mut request = self
            .agent
            .post(&url)
            .header("Content-Type", "application/json");
        if !self.config.api_key.is_empty() {
            request = request.header("Authorization", format!("Bearer {}", self.config.api_key));
        }

        let response = request
            .send(&body)
            .map_err(|error| format!("request failed: {error}"))?;

        let status = response.status().as_u16();
        if status != 200 {
            return Err(format!("server returned status {status}"));
        }

        let text = response
            .into_body()
            .read_to_string()
            .map_err(|error| format!("failed to decode response: {error}"))?;
        let result: Value = serde_json::from_str(&text)
            .map_err(|error| format!("failed to decode response: {error}"))?;

        let choices = result
            .get("choices")
            .and_then(Value::as_array)
            .cloned()
            .unwrap_or_default();
        if choices.is_empty() {
            return Ok(Response {
                content: String::new(),
                finish_reason: "empty".into(),
                ..Default::default()
            });
        }

        let choice = &choices[0];
        let message = choice.get("message").cloned().unwrap_or(Value::Null);
        let usage = result.get("usage").cloned().unwrap_or(Value::Null);

        Ok(Response {
            content: string_field(&message, "content"),
            reasoning: string_field(&message, "reasoning_content"),
            tool_calls: parse_tool_calls(message.get("tool_calls")),
            finish_reason: string_field(choice, "finish_reason"),
            usage_tokens: TokenUsage {
                prompt_tokens: int_field(&usage, "prompt_tokens"),
                completion_tokens: int_field(&usage, "completion_tokens"),
                total_tokens: int_field(&usage, "total_tokens"),
                cached_tokens: usage
                    .get("prompt_tokens_details")
                    .and_then(|details| details.get("cached_tokens"))
                    .and_then(Value::as_i64)
                    .unwrap_or(0),
            },
        })
    }

    /// OpenAI-compatible APIs support tools.
    fn supports_native_tool_calling(&self) -> bool {
        true
    }

    fn model_name(&self) -> String {
        self.config.model.clone()
    }

    fn context_window_size(&self) -> i64 {
        self.config.context_window
    }
}

fn string_field(value: &Value, key: &str) -> String {
    value
        .get(key)
        .and_then(Value::as_str)
        .unwrap_or_default()
        .to_owned()
}

fn int_field(value: &Value, key: &str) -> i64 {
    value.get(key).and_then(Value::as_i64).unwrap_or(0)
}

/// Parses `tool_calls` from a response message. Supports the OpenAI shape
/// (`id`, `function.name`, `function.arguments` as a JSON string — Go
/// `ExtractToolCall`) and the direct-key shape Go's decoder accepts
/// case-insensitively (`id`, `name`, `args` as an object). Malformed
/// arguments yield empty args, mirroring Go's silently ignored unmarshal.
fn parse_tool_calls(value: Option<&Value>) -> Vec<ToolCall> {
    let Some(entries) = value.and_then(Value::as_array) else {
        return Vec::new();
    };
    entries
        .iter()
        .map(|entry| {
            let function = entry.get("function");
            let name = function
                .and_then(|function| function.get("name"))
                .and_then(Value::as_str)
                .or_else(|| entry.get("name").and_then(Value::as_str))
                .unwrap_or_default()
                .to_owned();
            let args = parse_tool_call_args(entry, function);
            ToolCall {
                id: string_field(entry, "id"),
                name,
                args,
                metadata: entry.get("extra_content").cloned(),
            }
        })
        .collect()
}

fn parse_tool_call_args(entry: &Value, function: Option<&Value>) -> Map<String, Value> {
    if let Some(arguments) = function.and_then(|function| function.get("arguments")) {
        match arguments {
            Value::String(text) => {
                // Go: `_ = json.Unmarshal([]byte(args), &argsMap)` — errors
                // leave the map nil.
                return match serde_json::from_str::<Value>(text) {
                    Ok(Value::Object(map)) => map,
                    _ => Map::new(),
                };
            }
            Value::Object(map) => return map.clone(),
            _ => {}
        }
    }
    entry
        .get("args")
        .and_then(Value::as_object)
        .cloned()
        .unwrap_or_default()
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::llm::request::{MessageMeta, MessageType, Role, ThinkingConfig};
    use std::io::{Read, Write};
    use std::net::{TcpListener, TcpStream};
    use std::sync::mpsc;
    use std::thread;

    #[test]
    fn provider_extras_fixtures() {
        let cases: Value = serde_json::from_str(include_str!(
            "../../../../fixtures/llm/provider-extras/cases.json"
        ))
        .unwrap();
        for case in cases.as_array().unwrap() {
            let calls = parse_tool_calls(case.pointer("/response/choices/0/message/tool_calls"));
            let actual: Vec<Value> = calls.iter().map(tool_call_to_wire).collect();
            assert_eq!(
                Value::Array(actual),
                case["expected"]["tool_calls"],
                "{}",
                case["name"]
            );
        }
    }

    /// A minimal local HTTP server that captures the request body and replies
    /// with a canned status and body (the Rust equivalent of Go's
    /// `httptest.Server`).
    struct FakeServer {
        url: String,
        shutdown: mpsc::Sender<()>,
    }

    impl FakeServer {
        fn start(response_body: &'static str) -> (Self, mpsc::Receiver<Vec<u8>>) {
            Self::start_with_status(200, response_body)
        }

        fn start_with_status(
            status: u16,
            response_body: &'static str,
        ) -> (Self, mpsc::Receiver<Vec<u8>>) {
            let listener = TcpListener::bind("127.0.0.1:0").expect("bind");
            let port = listener.local_addr().expect("addr").port();
            let (shutdown_tx, shutdown_rx) = mpsc::channel::<()>();
            let (body_tx, body_rx) = mpsc::channel::<Vec<u8>>();
            thread::spawn(move || {
                for stream in listener.incoming() {
                    if shutdown_rx.try_recv().is_ok() {
                        break;
                    }
                    let Ok(mut stream) = stream else { continue };
                    let captured = handle_connection(&mut stream, status, response_body);
                    let _ = body_tx.send(captured);
                }
            });
            (
                Self {
                    url: format!("http://127.0.0.1:{port}"),
                    shutdown: shutdown_tx,
                },
                body_rx,
            )
        }
    }

    impl Drop for FakeServer {
        fn drop(&mut self) {
            let _ = self.shutdown.send(());
        }
    }

    fn handle_connection(
        stream: &mut TcpStream,
        status: u16,
        response_body: &'static str,
    ) -> Vec<u8> {
        let mut buffer = [0u8; 65536];
        let mut raw = Vec::new();
        // Read until end of headers, then Content-Length bytes of body.
        loop {
            let read = stream.read(&mut buffer).unwrap_or(0);
            if read == 0 {
                break;
            }
            raw.extend_from_slice(&buffer[..read]);
            if let Ok(text) = std::str::from_utf8(&raw) {
                if let Some(header_end) = text.find("\r\n\r\n") {
                    let length = text
                        .lines()
                        .find_map(|line| {
                            let (name, value) = line.split_once(':')?;
                            name.eq_ignore_ascii_case("content-length")
                                .then(|| value.trim().parse::<usize>().ok())?
                        })
                        .unwrap_or(0);
                    let body_start = header_end + 4;
                    if raw.len() >= body_start + length {
                        break;
                    }
                }
            }
        }
        let body_start = std::str::from_utf8(&raw)
            .ok()
            .and_then(|text| text.find("\r\n\r\n"))
            .map(|index| index + 4)
            .unwrap_or(raw.len());
        let reason = match status {
            200 => "OK",
            500 => "Internal Server Error",
            _ => "Error",
        };
        let response = format!(
            "HTTP/1.1 {status} {reason}\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{response_body}",
            response_body.len()
        );
        let _ = stream.write_all(response.as_bytes());
        raw[body_start..].to_vec()
    }

    fn test_backend(url: &str) -> ServerBackend {
        ServerBackend::new(ServerConfig {
            base_url: url.to_owned(),
            api_key: String::new(),
            model: "test-model".into(),
            context_window: 0,
            timeout: Duration::from_secs(5),
        })
    }

    #[test]
    fn complete_captures_cached_tokens() {
        let (server, _body_rx) = FakeServer::start(
            r#"{
                "choices": [{"message": {"role": "assistant", "content": "hi"}, "finish_reason": "stop"}],
                "usage": {
                    "prompt_tokens": 100,
                    "completion_tokens": 20,
                    "total_tokens": 120,
                    "prompt_tokens_details": {"cached_tokens": 64}
                }
            }"#,
        );
        let backend = test_backend(&server.url);

        let response = backend
            .complete(&Request {
                messages: vec![Message {
                    role: Role::User,
                    content: "hello".into(),
                    ..Default::default()
                }],
                ..Default::default()
            })
            .expect("complete");

        assert_eq!(response.usage_tokens.cached_tokens, 64);
        assert_eq!(response.usage_tokens.prompt_tokens, 100);
        assert_eq!(response.usage_tokens.completion_tokens, 20);
        assert_eq!(response.usage_tokens.total_tokens, 120);
    }

    #[test]
    fn complete_missing_cached_tokens_defaults_to_zero() {
        let (server, _body_rx) = FakeServer::start(
            r#"{
                "choices": [{"message": {"role": "assistant", "content": "hi"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120}
            }"#,
        );
        let backend = test_backend(&server.url);

        let response = backend
            .complete(&Request {
                messages: vec![Message {
                    role: Role::User,
                    content: "hello".into(),
                    ..Default::default()
                }],
                ..Default::default()
            })
            .expect("complete");

        assert_eq!(response.usage_tokens.cached_tokens, 0);
        assert_eq!(response.usage_tokens.prompt_tokens, 100);
    }

    #[test]
    fn complete_empty_choices_reports_empty_finish_reason() {
        let (server, _body_rx) = FakeServer::start(r#"{"choices": []}"#);
        let backend = test_backend(&server.url);

        let response = backend.complete(&Request::default()).expect("complete");
        assert_eq!(response.finish_reason, "empty");
        assert_eq!(response.content, "");
    }

    #[test]
    fn complete_non_200_is_an_error() {
        let (server, _body_rx) = FakeServer::start_with_status(500, "{}");
        let backend = test_backend(&server.url);

        let error = backend.complete(&Request::default()).unwrap_err();
        assert_eq!(error, "server returned status 500");
    }

    #[test]
    fn complete_invalid_json_is_an_error() {
        let (server, _body_rx) = FakeServer::start("not json");
        let backend = test_backend(&server.url);

        let error = backend.complete(&Request::default()).unwrap_err();
        assert!(
            error.starts_with("failed to decode response:"),
            "unexpected error: {error}"
        );
    }

    #[test]
    fn complete_parses_reasoning_content_and_tool_calls() {
        let (server, _body_rx) = FakeServer::start(
            r#"{
                "choices": [{
                    "message": {
                        "role": "assistant",
                        "content": "calling",
                        "reasoning_content": "thinking hard",
                        "tool_calls": [
                            {"id": "call_1", "type": "function", "function": {"name": "get_issue", "arguments": "{\"owner\":\"acme\"}"}},
                            {"id": "call_2", "type": "function", "function": {"name": "no_args", "arguments": ""}},
                            {"id": "call_3", "name": "direct", "args": {"k": "v"}}
                        ]
                    },
                    "finish_reason": "tool_calls"
                }],
                "usage": {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3}
            }"#,
        );
        let backend = test_backend(&server.url);

        let response = backend.complete(&Request::default()).expect("complete");
        assert_eq!(response.reasoning, "thinking hard");
        assert_eq!(response.finish_reason, "tool_calls");
        assert_eq!(response.tool_calls.len(), 3);
        assert_eq!(response.tool_calls[0].id, "call_1");
        assert_eq!(response.tool_calls[0].name, "get_issue");
        assert_eq!(
            response.tool_calls[0]
                .args
                .get("owner")
                .and_then(Value::as_str),
            Some("acme")
        );
        assert!(response.tool_calls[1].args.is_empty());
        assert_eq!(response.tool_calls[2].name, "direct");
        assert_eq!(
            response.tool_calls[2].args.get("k").and_then(Value::as_str),
            Some("v")
        );
    }

    /// Pins the exact JSON body keys the backend puts on the wire
    /// (orchestrator decision 2026-11-04): OpenAI snake_case per
    /// `go/llm/proxy/convert.go`, and `Meta` never serialized.
    #[test]
    fn request_body_keys_are_openai_snake_case_without_meta() {
        let (server, body_rx) = FakeServer::start(
            r#"{"choices":[{"message":{"role":"assistant","content":"ok"},"finish_reason":"stop"}]}"#,
        );
        let backend = test_backend(&server.url);

        let _ = backend
            .complete(&Request {
                messages: vec![
                    Message {
                        role: Role::System,
                        content: "be helpful".into(),
                        meta: MessageMeta {
                            message_type: MessageType::SystemPrompt,
                            step_index: Some(3),
                            original_type: Some(MessageType::UserInput),
                            token_estimate: Some(12),
                        },
                        ..Default::default()
                    },
                    Message {
                        role: Role::User,
                        content: "hello".into(),
                        ..Default::default()
                    },
                ],
                tools: vec![super::super::request::ToolDefinition {
                    name: "my_tool".into(),
                    description: "Does something".into(),
                    input_schema: Some(serde_json::from_str(r#"{"type":"object"}"#).unwrap()),
                }],
                temperature: 0.7,
                max_tokens: 1000,
                stop: Some(vec!["STOP".into(), "DONE".into()]),
                thinking: Some(ThinkingConfig {
                    r#type: "enabled".into(),
                    max_tokens: 8192,
                }),
            })
            .expect("complete");

        let body: Value =
            serde_json::from_slice(&body_rx.recv().expect("captured body")).expect("body is JSON");

        // Top-level envelope keys, exactly as go/llm/server.go builds them.
        let envelope = body.as_object().expect("body is an object");
        let mut keys: Vec<&str> = envelope.keys().map(String::as_str).collect();
        keys.sort();
        assert_eq!(
            keys,
            vec![
                "max_tokens",
                "messages",
                "model",
                "stop",
                "temperature",
                "thinking",
                "tools"
            ]
        );

        assert_eq!(body["model"], "test-model");
        assert_eq!(body["temperature"], 0.7);
        assert_eq!(body["max_tokens"], 1000);
        assert_eq!(body["stop"], json!(["STOP", "DONE"]));
        assert_eq!(
            body["thinking"],
            json!({"type": "enabled", "max_tokens": 8192})
        );

        // Messages: role/content only, no Meta, no PascalCase keys.
        let messages = body["messages"].as_array().expect("messages array");
        assert_eq!(messages.len(), 2);
        let first = messages[0].as_object().expect("message object");
        let mut first_keys: Vec<&str> = first.keys().map(String::as_str).collect();
        first_keys.sort();
        assert_eq!(first_keys, vec!["content", "role"]);
        assert_eq!(first["role"], "system");
        assert_eq!(first["content"], "be helpful");
        assert_eq!(messages[1], json!({"role": "user", "content": "hello"}));

        // Tools: OpenAI function shape.
        let tools = body["tools"].as_array().expect("tools array");
        assert_eq!(
            tools[0],
            json!({
                "type": "function",
                "function": {
                    "name": "my_tool",
                    "description": "Does something",
                    "parameters": {"type": "object"},
                },
            })
        );
    }

    #[test]
    fn message_with_tool_calls_and_tool_call_id_serializes_openai_shape() {
        let mut args = Map::new();
        args.insert("owner".into(), json!("acme"));
        let message = Message {
            role: Role::Assistant,
            content: String::new(),
            tool_call_id: "call_1".into(),
            tool_calls: vec![ToolCall {
                metadata: None,
                id: "call_1".into(),
                name: "get_issue".into(),
                args,
            }],
            ..Default::default()
        };

        assert_eq!(
            message_to_wire(&message),
            json!({
                "role": "assistant",
                "content": "",
                "tool_call_id": "call_1",
                "tool_calls": [{
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "get_issue", "arguments": "{\"owner\":\"acme\"}"},
                }],
            })
        );
    }

    #[test]
    fn base_url_loses_one_trailing_v1() {
        // Go strips "/v1" twice from the suffix; a single trailing "/v1" is
        // removed once and "/v1/chat/completions" is appended.
        let (server, body_rx) = FakeServer::start(
            r#"{"choices":[{"message":{"role":"assistant","content":"ok"},"finish_reason":"stop"}]}"#,
        );
        let base_with_v1 = format!("{}/v1", server.url);
        let backend = ServerBackend::new(ServerConfig {
            base_url: base_with_v1,
            api_key: String::new(),
            model: "test-model".into(),
            context_window: 0,
            timeout: Duration::from_secs(5),
        });
        let _ = backend.complete(&Request::default()).expect("complete");
        let body: Value =
            serde_json::from_slice(&body_rx.recv().expect("captured body")).expect("json");
        assert_eq!(body["model"], "test-model");
    }

    #[test]
    fn backend_metadata_reports_config() {
        let backend = test_backend("http://127.0.0.1:1");
        assert!(backend.supports_native_tool_calling());
        assert_eq!(backend.model_name(), "test-model");
        assert_eq!(backend.context_window_size(), 0);
    }
}
