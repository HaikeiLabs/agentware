//! Synchronous client for the `kei-proxy serve` Unix socket.
//!
//! The implementation deliberately uses only the standard library and
//! `serde_json`: the wire protocol is small HTTP/1.1 and does not need a
//! general-purpose HTTP client (which could accidentally honor ambient proxies).

use serde_json::{Map, Value, json};
use std::{
    collections::HashMap,
    io::{Read, Write},
    os::unix::net::UnixStream,
    path::PathBuf,
    sync::{
        Arc,
        atomic::{AtomicBool, Ordering},
    },
    time::{Duration, Instant},
};

pub const DEFAULT_SOCKET_PATH: &str = "/run/kei-proxy/runtime.sock";
/// Environment variable naming the daemon socket (Go: SocketPathEnv).
pub const SOCKET_PATH_ENV: &str = "KEI_RUNTIME_SOCKET_PATH";
pub const SESSION_SCHEMA: &str = "kei.session/v2";
pub const AUTHORIZE_REQUEST_SCHEMA: &str = "kei.authorize-request/v1";
pub const AUTHORIZE_RESPONSE_SCHEMA: &str = "kei.authorize-response/v1";
pub const POLICY_BUNDLE_STATUS_SCHEMA: &str = "kei.policy-bundle-status/v1";
/// Schema for POST /v1/credentials:release requests.
pub const CREDENTIAL_RELEASE_REQUEST_SCHEMA: &str = "kei.credential-release-request/v1";
/// Schema for POST /v1/credentials:release responses.
pub const CREDENTIAL_RELEASE_RESPONSE_SCHEMA: &str = "kei.credential-release-response/v1";
pub const SESSION_HEADER: &str = "X-Kei-Session";
pub const SESSION_NOT_FOUND: &str = "session_not_found";
pub const TOOLS_REGISTER_PATH: &str = "/v1/tools:register";
/// Bounds one non-streamed answer and one streamed line.
const MAX_RESPONSE_BYTES: usize = 8 << 20;

/// Fail-closed socket refusal class.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ErrorKind {
    ProxyUnavailable,
    ProxyTimeout,
    ProxyError,
    MalformedResponse,
    MissingToken,
    Cancelled,
    InvalidArgument,
}

/// Error safe to display: daemon response bodies and messages are never retained.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SocketError {
    pub kind: ErrorKind,
    pub code: String,
}
impl std::fmt::Display for SocketError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(
            f,
            "kei-proxy {:?}{}",
            self.kind,
            if self.code.is_empty() {
                String::new()
            } else {
                format!(" ({})", self.code)
            }
        )
    }
}
impl std::error::Error for SocketError {}
fn err(kind: ErrorKind) -> SocketError {
    SocketError {
        kind,
        code: String::new(),
    }
}

/// Explicit runtime identity, never inferred from ambient request data.
#[derive(Debug, Clone, PartialEq, Eq, Hash)]
pub struct Identity {
    pub source: String,
    pub external_id: String,
}
/// A named daemon session.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Session {
    pub source: String,
    pub external_id: String,
    pub session_id: String,
}
/// Request body for POST /v1/credentials:release. Exactly one of `tool` or
/// `connector_id`+`capability` must be set. `resource` is required when a
/// credential is requested.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct ReleaseRequest {
    /// Harness tool name. Exactly one of `tool` or `connector_id`+`capability`.
    pub tool: String,
    /// Connector to release a credential for.
    pub connector_id: String,
    /// Capability on that connector, e.g. "read".
    pub capability: String,
    /// Resource the provider call targets.
    pub resource: String,
    /// Audit context (optional, as in /v1/authorize).
    pub span_id: String,
    pub parent_span: String,
    pub delegation_depth: u64,
    pub framework: String,
    pub tool_args_digest: String,
}
/// A released connector credential value. It is short-lived and must never be
/// logged or printed.
#[derive(Clone, Default, PartialEq, Eq)]
pub struct Credential {
    pub connector_id: String,
    pub issuer: String,
    pub resource: String,
    pub account_model: String,
    pub access_token: String,
    pub expires_at: String,
}
impl std::fmt::Debug for Credential {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("Credential")
            .field("connector_id", &self.connector_id)
            .field("issuer", &self.issuer)
            .field("resource", &self.resource)
            .field("account_model", &self.account_model)
            .field("access_token", &"[REDACTED]")
            .field("expires_at", &self.expires_at)
            .finish()
    }
}
/// Parsed response from POST /v1/credentials:release. On allow, `credential`
/// is `Some`. On deny, `reason_code` is set and `credential` is `None`.
/// Transport failures are returned as an error, never a result.
#[derive(Clone, Default, PartialEq, Eq)]
pub struct ReleaseResult {
    pub decision: String,
    pub reason_code: String,
    pub reason: String,
    pub credential: Option<Credential>,
    pub policy_id: String,
    pub bundle_state: String,
}
impl std::fmt::Debug for ReleaseResult {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("ReleaseResult")
            .field("decision", &self.decision)
            .field("reason_code", &self.reason_code)
            .field("reason", &self.reason)
            .field("credential", &self.credential)
            .field("policy_id", &self.policy_id)
            .field("bundle_state", &self.bundle_state)
            .finish()
    }
}

/// Runtime socket client. Tokens are fetched for each request and are not stored.
#[derive(Debug, Clone)]
pub struct Client {
    pub socket_path: PathBuf,
    pub timeout: Duration,
    /// Bounds a chat completion, and the wait for each line of a streamed
    /// one. Default: 120 seconds.
    pub model_timeout: Duration,
    pub env: HashMap<String, String>,
    /// Optional caller-side deadline. Expiry is reported as `Cancelled`.
    pub caller_timeout: Option<Duration>,
    cancelled: Arc<AtomicBool>,
    sessions: HashMap<Identity, Session>,
}
impl Default for Client {
    fn default() -> Self {
        Self {
            socket_path: std::env::var("KEI_RUNTIME_SOCKET_PATH")
                .map(PathBuf::from)
                .unwrap_or_else(|_| PathBuf::from(DEFAULT_SOCKET_PATH)),
            timeout: Duration::from_secs(10),
            model_timeout: Duration::from_secs(120),
            env: HashMap::new(),
            caller_timeout: None,
            cancelled: Arc::new(AtomicBool::new(false)),
            sessions: HashMap::new(),
        }
    }
}
impl Client {
    pub fn new(path: impl Into<PathBuf>) -> Self {
        Self {
            socket_path: path.into(),
            ..Self::default()
        }
    }
    /// Cancel this client and all of its clones. Future or in-flight calls
    /// return `ErrorKind::Cancelled`, distinct from a daemon timeout.
    pub fn cancel(&self) {
        self.cancelled.store(true, Ordering::SeqCst);
    }
    fn token(&self) -> Result<String, SocketError> {
        self.env
            .get("KEI_RUNTIME_TOKEN")
            .cloned()
            .or_else(|| std::env::var("KEI_RUNTIME_TOKEN").ok())
            .filter(|s| !s.is_empty())
            .ok_or_else(|| err(ErrorKind::MissingToken))
    }
    fn timeout(&self) -> Duration {
        if self.timeout.is_zero() {
            Duration::from_secs(10)
        } else {
            self.timeout
        }
    }
    fn model_timeout(&self) -> Duration {
        if self.model_timeout.is_zero() {
            Duration::from_secs(120)
        } else {
            self.model_timeout
        }
    }
    /// Dial the socket and write one request. The token is read now, so a
    /// missing token refuses before anything is sent.
    fn send(
        &self,
        method: &str,
        path: &str,
        session: &str,
        body: Option<&Value>,
        timeout: Duration,
    ) -> Result<UnixStream, SocketError> {
        let token = self.token()?;
        let mut stream = UnixStream::connect(&self.socket_path).map_err(|e| {
            if e.kind() == std::io::ErrorKind::TimedOut {
                err(ErrorKind::ProxyTimeout)
            } else {
                err(ErrorKind::ProxyUnavailable)
            }
        })?;
        stream
            .set_read_timeout(Some(Duration::from_millis(20).min(timeout)))
            .map_err(|_| err(ErrorKind::ProxyUnavailable))?;
        stream
            .set_write_timeout(Some(timeout))
            .map_err(|_| err(ErrorKind::ProxyUnavailable))?;
        let bytes = body
            .map(serde_json::to_vec)
            .transpose()
            .map_err(|_| err(ErrorKind::MalformedResponse))?
            .unwrap_or_default();
        let mut request = format!(
            "{method} {path} HTTP/1.1\r\nHost: kei-proxy\r\nAuthorization: Bearer {token}\r\nConnection: close\r\n"
        );
        if !session.is_empty() {
            request.push_str(&format!("{SESSION_HEADER}: {session}\r\n"));
        }
        if body.is_some() {
            request.push_str("Content-Type: application/json\r\n");
        }
        request.push_str(&format!("Content-Length: {}\r\n\r\n", bytes.len()));
        stream
            .write_all(request.as_bytes())
            .and_then(|_| stream.write_all(&bytes))
            .map_err(transport_error)?;
        Ok(stream)
    }
    /// One request bounded by `timeout`; the answer is one JSON object.
    fn request(
        &self,
        method: &str,
        path: &str,
        session: &str,
        body: Option<&Value>,
        timeout: Duration,
    ) -> Result<(u16, Value), SocketError> {
        if self.cancelled.load(Ordering::SeqCst) || self.caller_timeout == Some(Duration::ZERO) {
            return Err(err(ErrorKind::Cancelled));
        }
        let started = Instant::now();
        let mut stream = self.send(method, path, session, body, timeout)?;
        let mut response = Vec::new();
        let mut buffer = [0u8; 8192];
        loop {
            if self.cancelled.load(Ordering::SeqCst)
                || self.caller_timeout.is_some_and(|d| started.elapsed() >= d)
            {
                return Err(err(ErrorKind::Cancelled));
            }
            match stream.read(&mut buffer) {
                Ok(0) => break,
                Ok(n) => response.extend_from_slice(&buffer[..n]),
                Err(e)
                    if matches!(
                        e.kind(),
                        std::io::ErrorKind::TimedOut | std::io::ErrorKind::WouldBlock
                    ) =>
                {
                    if started.elapsed() >= timeout {
                        return Err(err(ErrorKind::ProxyTimeout));
                    }
                }
                Err(e) => return Err(transport_error(e)),
            }
        }
        parse_response(&response)
    }
    /// Open or reuse the daemon session for an explicit source and external id.
    pub fn open_session(&mut self, identity: Identity) -> Result<Session, SocketError> {
        if identity.source.trim().is_empty() || identity.external_id.trim().is_empty() {
            return Err(err(ErrorKind::InvalidArgument));
        }
        if let Some(s) = self.sessions.get(&identity) {
            return Ok(s.clone());
        }
        let (status, v) = self.request("PUT", "/v1/session", "", Some(&json!({"schema":SESSION_SCHEMA,"source":identity.source,"external_id":identity.external_id})), self.timeout())?;
        check_status(status, &v)?;
        let id = v
            .get("session_id")
            .and_then(Value::as_str)
            .filter(|s| !s.is_empty())
            .ok_or_else(|| err(ErrorKind::MalformedResponse))?;
        if v.get("schema").and_then(Value::as_str) != Some(SESSION_SCHEMA) {
            return Err(err(ErrorKind::MalformedResponse));
        }
        let s = Session {
            source: identity.source.clone(),
            external_id: identity.external_id.clone(),
            session_id: id.into(),
        };
        self.sessions.insert(identity, s.clone());
        Ok(s)
    }
    /// Forget the cached session for an identity without contacting the daemon.
    pub fn forget_session(&mut self, identity: &Identity) {
        self.sessions.remove(identity);
    }
    /// Close a named session.
    pub fn close_session(&mut self, s: &Session) -> Result<(), SocketError> {
        self.sessions.remove(&Identity {
            source: s.source.clone(),
            external_id: s.external_id.clone(),
        });
        let (status, body) =
            self.request("DELETE", "/v1/session", &s.session_id, None, self.timeout())?;
        if status == 404 && error_code(&body) == SESSION_NOT_FOUND {
            return Ok(());
        }
        check_status(status, &body)
    }
    /// Authorize a tool request; daemon denials are successful JSON results.
    pub fn authorize(
        &mut self,
        s: &Session,
        fields: &Map<String, Value>,
    ) -> Result<Value, SocketError> {
        if s.source.is_empty() || s.external_id.is_empty() || s.session_id.is_empty() {
            return Err(err(ErrorKind::InvalidArgument));
        }
        let mut active = s.clone();
        let mut body = Map::new();
        body.insert("schema".into(), json!(AUTHORIZE_REQUEST_SCHEMA));
        for k in [
            "tool",
            "action",
            "resource",
            "span_id",
            "parent_span",
            "delegation_depth",
            "agent_id",
            "framework",
            "tool_args_digest",
        ] {
            if let Some(v) = fields.get(k) {
                if !v.is_null() && v != "" && v != 0 {
                    body.insert(k.into(), v.clone());
                }
            }
        }
        for attempt in 0..2 {
            let (status, answer) = self.request(
                "POST",
                "/v1/authorize",
                &active.session_id,
                Some(&Value::Object(body.clone())),
                self.timeout(),
            )?;
            if status == 404 && error_code(&answer) == SESSION_NOT_FOUND && attempt == 0 {
                self.forget_session(&active.identity());
                active = self.open_session(active.identity())?;
                continue;
            }
            check_status(status, &answer)?;
            if answer.get("schema").and_then(Value::as_str) != Some(AUTHORIZE_RESPONSE_SCHEMA)
                || !answer.get("decision").is_some_and(Value::is_string)
            {
                return Err(err(ErrorKind::MalformedResponse));
            }
            if attempt == 0
                && answer.get("reason_code").and_then(Value::as_str) == Some(SESSION_NOT_FOUND)
            {
                self.forget_session(&active.identity());
                active = self.open_session(active.identity())?;
                continue;
            }
            return Ok(answer);
        }
        Err(err(ErrorKind::ProxyError))
    }
    /// Pull a session credential from the daemon over the Unix socket, after
    /// an allow decision. A deny is returned as a typed result (the
    /// reason_code), not an error. Transport failures return an error. The
    /// credential value must never be logged or printed.
    pub fn release_credential(
        &mut self,
        session: &Session,
        req: &ReleaseRequest,
    ) -> Result<ReleaseResult, SocketError> {
        if session.source.is_empty()
            || session.external_id.is_empty()
            || session.session_id.is_empty()
        {
            return Err(err(ErrorKind::InvalidArgument));
        }
        let mut body = Map::new();
        body.insert("schema".into(), json!(CREDENTIAL_RELEASE_REQUEST_SCHEMA));
        body.insert("resource".into(), json!(req.resource));
        body.insert(
            "connector".into(),
            json!({"connector_id": req.connector_id, "capability": req.capability}),
        );
        if !req.tool.is_empty() {
            body.insert("tool".into(), json!(req.tool));
        }
        for (key, value) in [
            ("span_id", &req.span_id),
            ("parent_span", &req.parent_span),
            ("framework", &req.framework),
            ("tool_args_digest", &req.tool_args_digest),
        ] {
            if !value.is_empty() {
                body.insert(key.into(), json!(value));
            }
        }
        if req.delegation_depth > 0 {
            body.insert("delegation_depth".into(), json!(req.delegation_depth));
        }
        let mut active = session.clone();
        for attempt in 0..2 {
            let (status, answer) = self.request(
                "POST",
                "/v1/credentials:release",
                &active.session_id,
                Some(&Value::Object(body.clone())),
                self.timeout(),
            )?;
            if let Err(e) = check_status(status, &answer) {
                if attempt == 0 && e.code == SESSION_NOT_FOUND {
                    active = self.reopen(&active)?;
                    continue;
                }
                return Err(e);
            }
            let decision = answer
                .get("decision")
                .and_then(Value::as_str)
                .unwrap_or_default()
                .to_owned();
            if decision.is_empty()
                || answer.get("schema").and_then(Value::as_str)
                    != Some(CREDENTIAL_RELEASE_RESPONSE_SCHEMA)
            {
                return Err(err(ErrorKind::MalformedResponse));
            }
            if attempt == 0
                && answer.get("reason_code").and_then(Value::as_str) == Some(SESSION_NOT_FOUND)
            {
                active = self.reopen(&active)?;
                continue;
            }
            let mut result = ReleaseResult {
                decision,
                ..ReleaseResult::default()
            };
            if let Some(v) = answer.get("reason_code").and_then(Value::as_str) {
                result.reason_code = v.to_owned();
            }
            if let Some(v) = answer.get("reason").and_then(Value::as_str) {
                result.reason = v.to_owned();
            }
            if let Some(v) = answer.get("policy_id").and_then(Value::as_str) {
                result.policy_id = v.to_owned();
            }
            if let Some(v) = answer.get("bundle_state").and_then(Value::as_str) {
                result.bundle_state = v.to_owned();
            }
            if let Some(cred_raw) = answer.get("credential").filter(|v| v.is_object()) {
                let f = |key: &str| {
                    cred_raw
                        .get(key)
                        .and_then(Value::as_str)
                        .unwrap_or_default()
                        .to_owned()
                };
                result.credential = Some(Credential {
                    connector_id: f("connector_id"),
                    issuer: f("issuer"),
                    resource: f("resource"),
                    account_model: f("account_model"),
                    access_token: f("access_token"),
                    expires_at: f("expires_at"),
                });
            }
            return Ok(result);
        }
        Err(err(ErrorKind::ProxyError))
    }
    /// Read the policy bundle status document.
    pub fn policy_bundle_status(&self) -> Result<Value, SocketError> {
        let (status, v) =
            self.request("GET", "/v1/policy-bundle/status", "", None, self.timeout())?;
        check_status(status, &v)?;
        if v.get("schema").and_then(Value::as_str) != Some(POLICY_BUNDLE_STATUS_SCHEMA)
            || !v.get("bundle_schema").is_some_and(Value::is_string)
        {
            return Err(err(ErrorKind::MalformedResponse));
        }
        Ok(v)
    }
    /// Read the daemon liveness endpoint.
    pub fn readyz(&self) -> Result<Value, SocketError> {
        let (status, v) = self.request("GET", "/readyz", "", None, self.timeout())?;
        check_status(status, &v)?;
        Ok(v)
    }
    /// Register a v3 tool manifest with kei-proxy. The request carries metadata only.
    pub fn send_tools(&self, manifest: &[u8]) -> Result<(), SocketError> {
        let value: Value =
            serde_json::from_slice(manifest).map_err(|_| err(ErrorKind::InvalidArgument))?;
        if !value.is_object() {
            return Err(err(ErrorKind::InvalidArgument));
        }
        let (status, answer) = self.request(
            "POST",
            TOOLS_REGISTER_PATH,
            "",
            Some(&value),
            self.timeout(),
        )?;
        check_status(status, &answer)
    }
    /// List the models the daemon advertises, keeping entries that carry a
    /// non-empty string id. Optional selectors are query params.
    pub fn list_models(
        &self,
        agent_id: Option<&str>,
        profile_id: Option<&str>,
    ) -> Result<Value, SocketError> {
        let route = model_route(
            "/v1/models",
            agent_id.unwrap_or(""),
            profile_id.unwrap_or(""),
        );
        let (status, v) = self.request("GET", &route, "", None, self.timeout())?;
        check_status(status, &v)?;
        let data = v
            .get("data")
            .and_then(Value::as_array)
            .ok_or_else(|| err(ErrorKind::MalformedResponse))?;
        let models: Vec<Value> = data
            .iter()
            .filter(|m| {
                m.get("id")
                    .and_then(Value::as_str)
                    .is_some_and(|id| !id.is_empty())
            })
            .cloned()
            .collect();
        Ok(json!(models))
    }
    /// Returns the selected runtime model configuration. Selectors are query
    /// parameters; credential fields are always removed before returning.
    pub fn model_config(
        &self,
        agent_id: Option<&str>,
        profile_id: Option<&str>,
    ) -> Result<Value, SocketError> {
        let route = model_route(
            "/v1/model-config",
            agent_id.unwrap_or(""),
            profile_id.unwrap_or(""),
        );
        let (status, v) = self.request("GET", &route, "", None, self.timeout())?;
        check_status(status, &v)?;
        Ok(without_model_secrets(&v))
    }
    /// Returns assigned profile metadata, without credentials or secret
    /// references.
    pub fn list_model_profiles(&self, agent_id: Option<&str>) -> Result<Value, SocketError> {
        let route = model_route("/v1/model-profiles", agent_id.unwrap_or(""), "");
        let (status, v) = self.request("GET", &route, "", None, self.timeout())?;
        check_status(status, &v)?;
        let entries = v
            .get("model_profiles")
            .and_then(Value::as_array)
            .ok_or_else(|| err(ErrorKind::MalformedResponse))?;
        let profiles: Vec<Value> = entries
            .iter()
            .filter(|entry| entry.is_object())
            .map(without_model_secrets)
            .collect();
        Ok(json!(profiles))
    }
    /// Run one non-streaming chat completion in session. The body is the
    /// payload with "stream": false.
    pub fn chat_completion(
        &mut self,
        session: &Session,
        payload: &Value,
    ) -> Result<Value, SocketError> {
        let mut body = match payload {
            Value::Object(map) => map.clone(),
            _ => Map::new(),
        };
        body.insert("stream".into(), json!(false));
        let value = Value::Object(body);
        let mut active = session.clone();
        let (status, answer) = self.request(
            "POST",
            "/v1/chat/completions",
            &active.session_id,
            Some(&value),
            self.model_timeout(),
        )?;
        if !(200..300).contains(&status) && error_code(&answer) == SESSION_NOT_FOUND {
            active = self.reopen(&active)?;
            let (status, answer) = self.request(
                "POST",
                "/v1/chat/completions",
                &active.session_id,
                Some(&value),
                self.model_timeout(),
            )?;
            check_status(status, &answer)?;
            return Ok(answer);
        }
        check_status(status, &answer)?;
        Ok(answer)
    }
    /// Run a streaming chat completion in session and call `yield_line` with
    /// each server-sent "data:" line, unchanged ("data: [DONE]" included);
    /// blank lines, comments and other fields are dropped. The body is the
    /// payload with "stream": true. The model timeout bounds the wait for the
    /// answer and for each following line, not the whole stream. If
    /// `yield_line` returns an error the stream stops and that error is
    /// returned as is.
    pub fn stream_chat_completion(
        &mut self,
        session: &Session,
        payload: &Value,
        mut yield_line: impl FnMut(&str) -> Result<(), SocketError>,
    ) -> Result<(), SocketError> {
        let mut body = match payload {
            Value::Object(map) => map.clone(),
            _ => Map::new(),
        };
        body.insert("stream".into(), json!(true));
        let value = Value::Object(body);
        let mut active = session.clone();
        for attempt in 0..2 {
            let retry = self.stream(&active.session_id, &value, attempt == 0, &mut yield_line)?;
            if !retry {
                return Ok(());
            }
            active = self.reopen(&active)?;
        }
        Err(err(ErrorKind::ProxyError))
    }
    /// One streaming attempt. Returns Ok(true) when the daemon said
    /// session_not_found and `may_retry` is set.
    fn stream(
        &self,
        session_id: &str,
        body: &Value,
        may_retry: bool,
        yield_line: &mut impl FnMut(&str) -> Result<(), SocketError>,
    ) -> Result<bool, SocketError> {
        if self.cancelled.load(Ordering::SeqCst) || self.caller_timeout == Some(Duration::ZERO) {
            return Err(err(ErrorKind::Cancelled));
        }
        let started = Instant::now();
        let idle = self.model_timeout();
        let mut stream = self.send("POST", "/v1/chat/completions", session_id, Some(body), idle)?;
        let mut buffered: Vec<u8> = Vec::new();
        let mut chunk = [0u8; 8192];
        let head_end = loop {
            if self.cancelled.load(Ordering::SeqCst)
                || self.caller_timeout.is_some_and(|d| started.elapsed() >= d)
            {
                return Err(err(ErrorKind::Cancelled));
            }
            match stream.read(&mut chunk) {
                Ok(0) => return Err(err(ErrorKind::MalformedResponse)),
                Ok(n) => {
                    buffered.extend_from_slice(&chunk[..n]);
                    if let Some(p) = buffered.windows(4).position(|w| w == b"\r\n\r\n") {
                        break p + 4;
                    }
                    if buffered.len() > MAX_RESPONSE_BYTES {
                        return Err(err(ErrorKind::MalformedResponse));
                    }
                }
                Err(e)
                    if matches!(
                        e.kind(),
                        std::io::ErrorKind::TimedOut | std::io::ErrorKind::WouldBlock
                    ) =>
                {
                    if started.elapsed() >= idle {
                        return Err(err(ErrorKind::ProxyTimeout));
                    }
                }
                Err(e) => return Err(transport_error(e)),
            }
        };
        let head = String::from_utf8_lossy(&buffered[..head_end - 4]);
        let status = head
            .lines()
            .next()
            .and_then(|l| l.split_whitespace().nth(1))
            .and_then(|n| n.parse::<u16>().ok())
            .ok_or_else(|| err(ErrorKind::MalformedResponse))?;
        if !(200..300).contains(&status) {
            // The daemon closes after its error body; carry only its code.
            let mut body_bytes = buffered.split_off(head_end);
            loop {
                if body_bytes.len() > MAX_RESPONSE_BYTES {
                    break;
                }
                match stream.read(&mut chunk) {
                    Ok(0) | Err(_) => break,
                    Ok(n) => body_bytes.extend_from_slice(&chunk[..n]),
                }
            }
            let code = serde_json::from_slice::<Value>(&body_bytes)
                .map(|v| error_code(&v))
                .unwrap_or_default();
            if may_retry && code == SESSION_NOT_FOUND {
                return Ok(true);
            }
            return Err(SocketError {
                kind: ErrorKind::ProxyError,
                code,
            });
        }
        // Every complete line gets one idle window; only "data:" lines are
        // yielded, unchanged.
        let mut pending = buffered.split_off(head_end);
        let mut last = Instant::now();
        loop {
            while let Some(pos) = pending.iter().position(|&b| b == b'\n') {
                let mut line: Vec<u8> = pending.drain(..=pos).collect();
                line.pop();
                if line.last() == Some(&b'\r') {
                    line.pop();
                }
                if line.len() > MAX_RESPONSE_BYTES {
                    return Err(err(ErrorKind::MalformedResponse));
                }
                let line = String::from_utf8_lossy(&line).into_owned();
                if line.starts_with("data:") {
                    yield_line(&line)?;
                }
                last = Instant::now();
            }
            if pending.len() > MAX_RESPONSE_BYTES {
                return Err(err(ErrorKind::MalformedResponse));
            }
            match stream.read(&mut chunk) {
                Ok(0) => break,
                Ok(n) => pending.extend_from_slice(&chunk[..n]),
                Err(e)
                    if matches!(
                        e.kind(),
                        std::io::ErrorKind::TimedOut | std::io::ErrorKind::WouldBlock
                    ) =>
                {
                    if last.elapsed() >= idle {
                        return Err(err(ErrorKind::ProxyTimeout));
                    }
                }
                Err(e) => return Err(transport_error(e)),
            }
        }
        // A final unterminated line is still a line.
        if !pending.is_empty() {
            if pending.len() > MAX_RESPONSE_BYTES {
                return Err(err(ErrorKind::MalformedResponse));
            }
            let mut line = std::mem::take(&mut pending);
            if line.last() == Some(&b'\r') {
                line.pop();
            }
            let line = String::from_utf8_lossy(&line).into_owned();
            if line.starts_with("data:") {
                yield_line(&line)?;
            }
        }
        Ok(false)
    }
    /// Forget a stale session and open a fresh one for the same identity.
    fn reopen(&mut self, stale: &Session) -> Result<Session, SocketError> {
        self.forget_session(&stale.identity());
        self.open_session(stale.identity())
    }
}
impl Session {
    fn identity(&self) -> Identity {
        Identity {
            source: self.source.clone(),
            external_id: self.external_id.clone(),
        }
    }
}
fn error_code(v: &Value) -> String {
    v.pointer("/error/code")
        .and_then(Value::as_str)
        .or_else(|| v.get("code").and_then(Value::as_str))
        .unwrap_or("")
        .to_owned()
}
fn check_status(status: u16, v: &Value) -> Result<(), SocketError> {
    if (200..300).contains(&status) {
        Ok(())
    } else {
        Err(SocketError {
            kind: ErrorKind::ProxyError,
            code: error_code(v),
        })
    }
}
fn transport_error(e: std::io::Error) -> SocketError {
    if e.kind() == std::io::ErrorKind::TimedOut || e.kind() == std::io::ErrorKind::WouldBlock {
        err(ErrorKind::ProxyTimeout)
    } else {
        err(ErrorKind::ProxyUnavailable)
    }
}
fn parse_response(bytes: &[u8]) -> Result<(u16, Value), SocketError> {
    let split = bytes
        .windows(4)
        .position(|w| w == b"\r\n\r\n")
        .ok_or_else(|| err(ErrorKind::MalformedResponse))?;
    let head =
        std::str::from_utf8(&bytes[..split]).map_err(|_| err(ErrorKind::MalformedResponse))?;
    let status = head
        .lines()
        .next()
        .and_then(|l| l.split_whitespace().nth(1))
        .and_then(|n| n.parse::<u16>().ok())
        .ok_or_else(|| err(ErrorKind::MalformedResponse))?;
    let raw = &bytes[split + 4..];
    let value = if raw.is_empty() {
        Value::Object(Map::new())
    } else {
        serde_json::from_slice(raw).map_err(|_| err(ErrorKind::MalformedResponse))?
    };
    if !value.is_object() {
        return Err(err(ErrorKind::MalformedResponse));
    }
    Ok((status, value))
}
/// Append the non-empty selectors to a model route as query parameters
/// (`agent_id` first, then `profile_id`, as Go's sorted url.Values do).
fn model_route(path: &str, agent_id: &str, profile_id: &str) -> String {
    let mut query = Vec::new();
    if !agent_id.is_empty() {
        query.push(format!("agent_id={}", query_escape(agent_id)));
    }
    if !profile_id.is_empty() {
        query.push(format!("profile_id={}", query_escape(profile_id)));
    }
    if query.is_empty() {
        path.to_owned()
    } else {
        format!("{path}?{}", query.join("&"))
    }
}
/// Percent-encode a query component the way Go's url.QueryEscape does:
/// space becomes `+`, alphanumerics and `-_.~` stay raw, everything else
/// becomes %XX.
fn query_escape(value: &str) -> String {
    let mut out = String::with_capacity(value.len());
    for b in value.bytes() {
        match b {
            b'A'..=b'Z' | b'a'..=b'z' | b'0'..=b'9' | b'-' | b'_' | b'.' | b'~' => {
                out.push(b as char)
            }
            b' ' => out.push('+'),
            _ => out.push_str(&format!("%{b:02X}")),
        }
    }
    out
}
/// Remove credential fields (`secret_ref`, `credential`, `api_key`,
/// `apiKey`) at any depth, as Go's withoutModelSecrets does.
fn without_model_secrets(value: &Value) -> Value {
    match value {
        Value::Object(map) => map
            .iter()
            .filter(|(k, _)| {
                !matches!(
                    k.as_str(),
                    "secret_ref" | "credential" | "api_key" | "apiKey"
                )
            })
            .map(|(k, v)| (k.clone(), without_model_secrets(v)))
            .collect(),
        Value::Array(items) => Value::Array(items.iter().map(without_model_secrets).collect()),
        other => other.clone(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::{os::unix::net::UnixListener, sync::atomic::AtomicUsize};

    /// A tiny daemon: answers each queued response in order and returns the
    /// requests it saw (method, target, session, authorization, parsed body).
    fn fake_daemon(responses: &[Value]) -> (PathBuf, std::thread::JoinHandle<Vec<Value>>) {
        static NEXT: AtomicUsize = AtomicUsize::new(0);
        let path = std::env::temp_dir().join(format!(
            "kei-test-{}-{}.sock",
            std::process::id(),
            NEXT.fetch_add(1, Ordering::SeqCst)
        ));
        let listener = UnixListener::bind(&path).unwrap();
        let responses = responses.to_vec();
        let handle = std::thread::spawn(move || {
            let mut seen = Vec::new();
            for response in responses {
                let (mut s, _) = listener.accept().unwrap();
                let mut buf = Vec::new();
                let mut tmp = [0u8; 4096];
                let head_end = loop {
                    let n = s.read(&mut tmp).unwrap();
                    buf.extend_from_slice(&tmp[..n]);
                    if let Some(p) = buf.windows(4).position(|w| w == b"\r\n\r\n") {
                        break p + 4;
                    }
                };
                let head = String::from_utf8_lossy(&buf[..head_end - 4]).into_owned();
                let len = head
                    .lines()
                    .find_map(|l| {
                        l.to_ascii_lowercase()
                            .strip_prefix("content-length: ")
                            .and_then(|v| v.parse::<usize>().ok())
                    })
                    .unwrap_or(0);
                while buf.len() < head_end + len {
                    let n = s.read(&mut tmp).unwrap();
                    buf.extend_from_slice(&tmp[..n]);
                }
                let method = head.split_whitespace().next().unwrap_or_default();
                let target = head
                    .lines()
                    .next()
                    .and_then(|l| l.split_whitespace().nth(1))
                    .unwrap_or_default();
                let session = head
                    .lines()
                    .find_map(|l| l.strip_prefix("X-Kei-Session: "))
                    .unwrap_or_default();
                let authorization = head
                    .lines()
                    .find_map(|l| l.strip_prefix("Authorization: "))
                    .unwrap_or_default();
                let body = if buf.len() == head_end {
                    Value::Null
                } else {
                    serde_json::from_slice(&buf[head_end..]).unwrap()
                };
                seen.push(json!({
                    "method": method,
                    "target": target,
                    "session": session,
                    "authorization": authorization,
                    "body": body
                }));
                let raw = serde_json::to_vec(&response).unwrap();
                let wire = format!(
                    "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n",
                    raw.len()
                );
                s.write_all(wire.as_bytes()).unwrap();
                s.write_all(&raw).unwrap();
            }
            seen
        });
        (path, handle)
    }
    #[test]
    fn model_profile_selection_and_secret_redaction() {
        let (path, server) = fake_daemon(&[
            json!({"model": "selected", "endpoint": "https://model.example", "credential": "hidden"}),
            json!({"model_profiles": [{"profile_id": "p1", "is_default": false, "secret_ref": "hidden"}]}),
        ]);
        let mut c = Client::new(&path);
        c.env = HashMap::from([("KEI_RUNTIME_TOKEN".to_owned(), "test-token".to_owned())]);
        let config = c.model_config(Some("agent 1"), Some("profile/2")).unwrap();
        assert_eq!(config["model"], "selected");
        assert!(config.get("credential").is_none());
        let profiles = c.list_model_profiles(Some("agent 1")).unwrap();
        let profiles = profiles.as_array().unwrap();
        assert_eq!(profiles.len(), 1);
        assert_eq!(profiles[0]["profile_id"], "p1");
        assert_eq!(profiles[0]["is_default"], false);
        assert!(profiles[0].get("secret_ref").is_none());
        let seen = server.join().unwrap();
        assert_eq!(
            seen[0]["target"],
            "/v1/model-config?agent_id=agent+1&profile_id=profile%2F2"
        );
        assert_eq!(seen[1]["target"], "/v1/model-profiles?agent_id=agent+1");
    }
    #[test]
    fn list_models_selectors_are_query_params() {
        let (path, server) = fake_daemon(&[json!({"data": [{"id": "m1"}]})]);
        let mut c = Client::new(&path);
        c.env = HashMap::from([("KEI_RUNTIME_TOKEN".to_owned(), "test-token".to_owned())]);
        let models = c.list_models(Some("agent 1"), Some("profile/2")).unwrap();
        assert_eq!(models[0]["id"], "m1");
        let seen = server.join().unwrap();
        assert_eq!(
            seen[0]["target"],
            "/v1/models?agent_id=agent+1&profile_id=profile%2F2"
        );
    }
    #[test]
    fn model_secrets_are_stripped_at_any_depth() {
        let v = json!({
            "model": "m",
            "credential": "top",
            "apiKey": "raw",
            "nested": {"secret_ref": "s", "api_key": "k", "keep": [1, {"credential": "deep"}]},
            "keep": "value"
        });
        assert_eq!(
            without_model_secrets(&v),
            json!({"model": "m", "nested": {"keep": [1, {}]}, "keep": "value"})
        );
    }
    #[test]
    fn release_credential_allow_releases_the_credential() {
        let (path, server) = fake_daemon(&[
            json!({"schema": SESSION_SCHEMA, "session_id": "sess-1"}),
            json!({
                "schema": CREDENTIAL_RELEASE_RESPONSE_SCHEMA,
                "decision": "allow",
                "credential": {
                    "connector_id": "github",
                    "issuer": "https://auth.example.com",
                    "resource": "repo:owner/name",
                    "account_model": "per_user",
                    "access_token": "ghs_secret",
                    "expires_at": "2026-10-17T10:00:00Z"
                },
                "policy_id": "pol_01J",
                "bundle_state": "ready"
            }),
        ]);
        let mut c = Client::new(&path);
        c.env = HashMap::from([("KEI_RUNTIME_TOKEN".to_owned(), "test-token".to_owned())]);
        let sess = c
            .open_session(Identity {
                source: "test".into(),
                external_id: "ada".into(),
            })
            .unwrap();
        let result = c
            .release_credential(
                &sess,
                &ReleaseRequest {
                    connector_id: "github".into(),
                    capability: "read".into(),
                    resource: "repo:owner/name".into(),
                    ..ReleaseRequest::default()
                },
            )
            .unwrap();
        assert_eq!(result.decision, "allow");
        let cred = result.credential.expect("credential on allow");
        assert_eq!(cred.access_token, "ghs_secret");
        assert_eq!(cred.connector_id, "github");
        let seen = server.join().unwrap();
        assert_eq!(seen.len(), 2);
        assert_eq!(seen[1]["method"], "POST");
        assert_eq!(seen[1]["target"], "/v1/credentials:release");
        assert_eq!(seen[1]["session"], "sess-1");
        assert_eq!(seen[1]["authorization"], "Bearer test-token");
        let body = &seen[1]["body"];
        assert_eq!(body["schema"], CREDENTIAL_RELEASE_REQUEST_SCHEMA);
        assert_eq!(
            body["connector"],
            json!({"connector_id": "github", "capability": "read"})
        );
        assert_eq!(body["resource"], "repo:owner/name");
        assert!(body.get("span_id").is_none());
    }
    #[test]
    fn release_credential_deny_returns_reason_code_without_credential() {
        let (path, server) = fake_daemon(&[
            json!({"schema": SESSION_SCHEMA, "session_id": "sess-1"}),
            json!({
                "schema": CREDENTIAL_RELEASE_RESPONSE_SCHEMA,
                "decision": "deny",
                "reason_code": "credential_not_connected",
                "reason": "the connector is not connected for this user",
                "bundle_state": "ready"
            }),
        ]);
        let mut c = Client::new(&path);
        c.env = HashMap::from([("KEI_RUNTIME_TOKEN".to_owned(), "test-token".to_owned())]);
        let sess = c
            .open_session(Identity {
                source: "test".into(),
                external_id: "ada".into(),
            })
            .unwrap();
        let result = c
            .release_credential(
                &sess,
                &ReleaseRequest {
                    connector_id: "github".into(),
                    capability: "read".into(),
                    resource: "repo:owner/name".into(),
                    ..ReleaseRequest::default()
                },
            )
            .unwrap();
        assert_eq!(result.decision, "deny");
        assert!(result.credential.is_none());
        assert_eq!(result.reason_code, "credential_not_connected");
        let seen = server.join().unwrap();
        assert_eq!(seen[1]["body"]["tool"], Value::Null);
    }
    #[test]
    fn release_credential_transport_failure_is_an_error() {
        let mut c = Client::new("/nonexistent");
        c.env = HashMap::from([("KEI_RUNTIME_TOKEN".to_owned(), "test-token".to_owned())]);
        let err = c
            .release_credential(
                &Session {
                    source: "test".into(),
                    external_id: "ada".into(),
                    session_id: "sess-1".into(),
                },
                &ReleaseRequest {
                    connector_id: "github".into(),
                    capability: "read".into(),
                    resource: "repo:owner/name".into(),
                    ..ReleaseRequest::default()
                },
            )
            .unwrap_err();
        assert_eq!(err.kind, ErrorKind::ProxyUnavailable);
    }
    #[test]
    fn released_secret_never_appears_in_debug_or_errors() {
        // A session without source/external id refuses before any daemon call.
        let mut c = Client::new("/nonexistent");
        c.env = HashMap::from([("KEI_RUNTIME_TOKEN".to_owned(), "test-token".to_owned())]);
        let err = c
            .release_credential(
                &Session {
                    source: String::new(),
                    external_id: String::new(),
                    session_id: "sess-1".into(),
                },
                &ReleaseRequest::default(),
            )
            .unwrap_err();
        assert_eq!(err.kind, ErrorKind::InvalidArgument);
        let msg = err.to_string();
        assert!(!msg.contains("ghs_secret"));
        assert!(!msg.contains("secret"));
        // Debug output redacts the credential value.
        let result = ReleaseResult {
            decision: "allow".into(),
            credential: Some(Credential {
                connector_id: "github".into(),
                access_token: "ghs_secret".into(),
                ..Credential::default()
            }),
            ..ReleaseResult::default()
        };
        let rendered = format!("{result:?}");
        assert!(!rendered.contains("ghs_secret"));
        assert!(rendered.contains("[REDACTED]"));
        let cred = result.credential.expect("credential set");
        assert!(!format!("{cred:?}").contains("ghs_secret"));
    }
    #[test]
    fn error_classes_are_distinct_and_token_safe() {
        let cases = [
            ErrorKind::ProxyUnavailable,
            ErrorKind::ProxyTimeout,
            ErrorKind::ProxyError,
            ErrorKind::MalformedResponse,
            ErrorKind::MissingToken,
            ErrorKind::Cancelled,
            ErrorKind::InvalidArgument,
        ];
        for kind in cases {
            let e = err(kind);
            assert_eq!(e.kind, kind);
            assert!(!e.to_string().contains("kei_rt_contract-canary"));
        }
    }
    #[test]
    fn response_requires_object() {
        assert_eq!(
            parse_response(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\n[]")
                .unwrap_err()
                .kind,
            ErrorKind::MalformedResponse
        );
    }
    #[test]
    fn caller_cancellation_and_deadline_have_their_own_error_kind() {
        let c = Client::new("/nonexistent");
        c.cancel();
        assert_eq!(
            c.policy_bundle_status().unwrap_err().kind,
            ErrorKind::Cancelled
        );
        let mut c = Client::new("/nonexistent");
        c.caller_timeout = Some(Duration::ZERO);
        assert_eq!(
            c.policy_bundle_status().unwrap_err().kind,
            ErrorKind::Cancelled
        );
    }
}
