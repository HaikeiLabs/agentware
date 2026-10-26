//! Runtime tool registration with Kei.
use crate::{
    runtimesocket::{Client, SocketError},
    toolspec::{ToolSpec, marshal_manifest_v3},
};
use serde_json::{Map, Value};
use std::{collections::HashMap, time::Duration};

pub const TOOLS_PATH: &str = "/api/v1/runtime/tools";
pub const MANIFEST_MEDIA_TYPE: &str = "application/vnd.kei.tool-manifest.v3+json";
pub const CONTROL_PLANE_URL_ENV: &str = "KEI_RUNTIME_CONTROL_PLANE_URL";
pub const TOKEN_ENV: &str = "KEI_RUNTIME_TOKEN";
pub const EGRESS_PROXY_ENV: &str = "KEI_PROXY_EGRESS_PROXY";
pub const NO_PROXY_ENV: &str = "KEI_PROXY_NO_PROXY";
pub const DEFAULT_ATTEMPTS: usize = 5;
pub const DEFAULT_BACKOFF: Duration = Duration::from_secs(1);
pub const MAX_BACKOFF: Duration = Duration::from_secs(16);
pub const DEFAULT_TIMEOUT: Duration = Duration::from_secs(30);
pub const ERROR_BODY_LIMIT: usize = 300;
pub const REDACTION: &str = "[redacted]";

#[derive(Debug, Clone, PartialEq)]
pub struct SyncResult {
    pub sent: bool,
    pub tool_count: usize,
    pub counts: Map<String, Value>,
}
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SyncError {
    pub kind: &'static str,
    pub message: String,
}
impl std::fmt::Display for SyncError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(&self.message)
    }
}
impl std::error::Error for SyncError {}
fn failure(kind: &'static str, message: impl Into<String>) -> SyncError {
    SyncError {
        kind,
        message: message.into(),
    }
}

/// Validate, serialize and send one non-empty v3 manifest through kei-proxy.
pub fn register(client: &Client, specs: &[ToolSpec]) -> Result<(), RegisterError> {
    if specs.is_empty() {
        return Err(RegisterError::EmptyManifest);
    }
    let manifest = marshal_manifest_v3(specs).map_err(RegisterError::InvalidManifest)?;
    client.send_tools(&manifest).map_err(RegisterError::Socket)
}
#[derive(Debug)]
pub enum RegisterError {
    EmptyManifest,
    InvalidManifest(String),
    Socket(SocketError),
}
impl std::fmt::Display for RegisterError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::EmptyManifest => {
                f.write_str("refusing to send an empty manifest to /v1/tools:register")
            }
            Self::InvalidManifest(s) => f.write_str(s),
            Self::Socket(e) => write!(f, "{e}"),
        }
    }
}
impl std::error::Error for RegisterError {}

/// Count entries in a manifest's tools array.
pub fn manifest_tool_count(manifest: &Value) -> usize {
    manifest
        .get("tools")
        .and_then(Value::as_array)
        .map_or(0, Vec::len)
}

#[derive(Debug, Clone)]
pub struct Config {
    pub control_plane_url: String,
    pub token: String,
    pub egress_proxy: String,
    pub no_proxy: String,
    pub env: HashMap<String, String>,
    pub attempts: usize,
    pub backoff: Duration,
    pub timeout: Duration,
}
impl Config {
    pub fn from_env(env: &HashMap<String, String>) -> std::result::Result<Self, SyncError> {
        let get = |k: &str| env.get(k).map(|v| v.trim().to_owned()).unwrap_or_default();
        let url = get(CONTROL_PLANE_URL_ENV);
        let token = get(TOKEN_ENV);
        if url.is_empty() {
            return Err(failure(
                "invalid_config",
                format!("{CONTROL_PLANE_URL_ENV} is not set"),
            ));
        }
        if token.is_empty() {
            return Err(failure("invalid_config", format!("{TOKEN_ENV} is not set")));
        }
        if !(url.starts_with("http://") || url.starts_with("https://")) {
            return Err(failure(
                "invalid_config",
                format!("{CONTROL_PLANE_URL_ENV} is not an http(s) URL"),
            ));
        }
        Ok(Self {
            control_plane_url: url,
            token,
            egress_proxy: get(EGRESS_PROXY_ENV),
            no_proxy: get(NO_PROXY_ENV),
            env: env.clone(),
            attempts: DEFAULT_ATTEMPTS,
            backoff: DEFAULT_BACKOFF,
            timeout: DEFAULT_TIMEOUT,
        })
    }
    pub fn tools_url(&self) -> String {
        format!(
            "{}{}",
            self.control_plane_url.trim_end_matches('/'),
            TOOLS_PATH
        )
    }
    pub fn proxy_url(&self, target: &str) -> Option<String> {
        let host = target
            .split_once("://")?
            .1
            .split('/')
            .next()?
            .split(':')
            .next()?
            .trim_matches(['[', ']'])
            .to_ascii_lowercase();
        let bypass = |list: &str| {
            list.split(',').any(|e| {
                let e = e.trim().trim_start_matches('.').to_ascii_lowercase();
                e == "*" || host == e || host.ends_with(&format!(".{e}"))
            })
        };
        if !self.egress_proxy.is_empty() {
            return if bypass(&self.no_proxy) {
                None
            } else {
                Some(self.egress_proxy.clone())
            };
        }
        let key = if target.starts_with("http://") {
            "HTTP_PROXY"
        } else {
            "HTTPS_PROXY"
        };
        let proxy = self
            .env
            .get(key)
            .or_else(|| self.env.get(&key.to_ascii_lowercase()))?;
        let no = self
            .env
            .get("NO_PROXY")
            .or_else(|| self.env.get("no_proxy"))
            .map(String::as_str)
            .unwrap_or("");
        if bypass(no) {
            None
        } else {
            Some(proxy.clone())
        }
    }
}

/// Send a v3 document to the control-plane replacement endpoint. Empty manifests are skipped unless tools were expected.
pub fn sync(
    manifest_text: &str,
    config: &Config,
    expect_tools: bool,
) -> std::result::Result<SyncResult, SyncError> {
    let value: Value = serde_json::from_str(manifest_text)
        .map_err(|_| failure("invalid_manifest", "the tool manifest is not JSON"))?;
    if !value.is_object() {
        return Err(failure(
            "invalid_manifest",
            "the tool manifest is not a JSON object",
        ));
    }
    let count = manifest_tool_count(&value);
    if count == 0 {
        if expect_tools {
            return Err(failure(
                "empty_manifest",
                "refusing to PUT an empty manifest: it would unregister every tool",
            ));
        }
        return Ok(SyncResult {
            sent: false,
            tool_count: 0,
            counts: Map::new(),
        });
    }
    let body = serde_json::to_vec(&value)
        .map_err(|_| failure("invalid_manifest", "the tool manifest is not JSON"))?;
    let attempts = if config.attempts == 0 {
        DEFAULT_ATTEMPTS
    } else {
        config.attempts
    };
    let mut last = String::from("no attempt made");
    for attempt in 0..attempts {
        let response = ureq::put(&config.tools_url())
            .header("Authorization", format!("Bearer {}", config.token))
            .header("Content-Type", MANIFEST_MEDIA_TYPE)
            .header("Accept", MANIFEST_MEDIA_TYPE)
            .send(&body);
        match response {
            Ok(res) => {
                let mut counts = Map::new();
                if let Ok(v) = res.into_body().read_json::<Value>() {
                    if let Some(m) = v.as_object() {
                        counts = m.clone();
                    }
                }
                return Ok(SyncResult {
                    sent: true,
                    tool_count: count,
                    counts,
                });
            }
            Err(e) => {
                last = e.to_string().replace(&config.token, REDACTION);
                if attempt + 1 < attempts {
                    let d = config
                        .backoff
                        .saturating_mul(1u32 << attempt.min(30))
                        .min(MAX_BACKOFF);
                    std::thread::sleep(d);
                }
            }
        }
    }
    Err(failure(
        "sync_failed",
        format!("PUT {TOOLS_PATH} failed after {attempts} attempt(s): {last}"),
    ))
}
