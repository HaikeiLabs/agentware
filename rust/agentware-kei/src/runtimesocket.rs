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
pub const SESSION_SCHEMA: &str = "kei.session/v2";
pub const AUTHORIZE_REQUEST_SCHEMA: &str = "kei.authorize-request/v1";
pub const AUTHORIZE_RESPONSE_SCHEMA: &str = "kei.authorize-response/v1";
pub const POLICY_BUNDLE_STATUS_SCHEMA: &str = "kei.policy-bundle-status/v1";
pub const SESSION_HEADER: &str = "X-Kei-Session";
pub const SESSION_NOT_FOUND: &str = "session_not_found";

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

/// Runtime socket client. Tokens are fetched for each request and are not stored.
#[derive(Debug, Clone)]
pub struct Client {
    pub socket_path: PathBuf,
    pub timeout: Duration,
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
    fn request(
        &self,
        method: &str,
        path: &str,
        session: &str,
        body: Option<&Value>,
    ) -> Result<(u16, Value), SocketError> {
        if self.cancelled.load(Ordering::SeqCst) || self.caller_timeout == Some(Duration::ZERO) {
            return Err(err(ErrorKind::Cancelled));
        }
        let started = Instant::now();
        let token = self.token()?;
        #[cfg(not(unix))]
        {
            let _ = (method, path, session, body, token);
            return Err(err(ErrorKind::ProxyUnavailable));
        }
        #[cfg(unix)]
        {
            let mut stream = UnixStream::connect(&self.socket_path).map_err(|e| {
                if e.kind() == std::io::ErrorKind::TimedOut {
                    err(ErrorKind::ProxyTimeout)
                } else {
                    err(ErrorKind::ProxyUnavailable)
                }
            })?;
            stream
                .set_read_timeout(Some(Duration::from_millis(20).min(self.timeout)))
                .map_err(|_| err(ErrorKind::ProxyUnavailable))?;
            stream
                .set_write_timeout(Some(self.timeout))
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
                        if started.elapsed() >= self.timeout {
                            return Err(err(ErrorKind::ProxyTimeout));
                        }
                    }
                    Err(e) => return Err(transport_error(e)),
                }
            }
            parse_response(&response)
        }
    }
    /// Open or reuse the daemon session for an explicit source and external id.
    pub fn open_session(&mut self, identity: Identity) -> Result<Session, SocketError> {
        if identity.source.trim().is_empty() || identity.external_id.trim().is_empty() {
            return Err(err(ErrorKind::InvalidArgument));
        }
        if let Some(s) = self.sessions.get(&identity) {
            return Ok(s.clone());
        }
        let (status, v) = self.request("PUT", "/v1/session", "", Some(&json!({"schema":SESSION_SCHEMA,"source":identity.source,"external_id":identity.external_id})))?;
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
        let (status, body) = self.request("DELETE", "/v1/session", &s.session_id, None)?;
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
    /// Read the policy bundle status document.
    pub fn policy_bundle_status(&self) -> Result<Value, SocketError> {
        let (status, v) = self.request("GET", "/v1/policy-bundle/status", "", None)?;
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
        let (status, v) = self.request("GET", "/readyz", "", None)?;
        check_status(status, &v)?;
        Ok(v)
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

#[cfg(test)]
mod tests {
    use super::*;
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
