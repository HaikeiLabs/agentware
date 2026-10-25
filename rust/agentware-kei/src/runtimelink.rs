//! Runtime link configuration, child command construction, and shared contract helpers.
//! Child processes receive only the documented environment allowlist.
use serde_json::{Map, Value, json};
use std::{collections::BTreeMap, env, process::Command, time::Duration};

pub const CONTRACT_VERSION: u64 = 1;
pub const MAX_CHILD_LINE_BYTES: usize = 4096;
pub const EVENT_BUFFER_SIZE: usize = 64;
pub const DEFAULT_BINARY_PATH: &str = "kei-proxy";
pub const CHILD_ENV_ALLOWLIST: &[&str] = &[
    "KEI_RUNTIME_TOKEN",
    "KEI_RUNTIME_CONTROL_PLANE_URL",
    "KEI_HARNESS_KIND",
    "KEI_HARNESS_VERSION",
    "KEI_DEPLOYMENT_ENV",
    "KEI_AGENTWARE_SDK_LANG",
    "KEI_AGENTWARE_SDK_VERSION",
    "KEI_HEARTBEAT_RUN_ID",
    "PATH",
    "HOME",
    "TZ",
];
pub const HARNESS_KINDS: &[&str] = &[
    "assistant",
    "pde",
    "chat-discord",
    "chat-slack",
    "chat-teams",
    "cli",
];
pub const LINK_STATES: &[&str] = &[
    "disabled",
    "starting",
    "connected",
    "degraded",
    "reconnecting",
    "terminal",
    "stopped",
];
pub const FAILURE_CLASSES: &[&str] = &[
    "config_invalid",
    "runtime_unavailable",
    "runtime_crashloop",
    "runtime_unresponsive",
    "contract_mismatch",
    "catalog_unreachable",
    "catalog_timeout",
    "catalog_error",
    "catalog_backpressure",
    "installation_unauthorized",
    "installation_stale",
    "installation_offline",
    "audit_backlog",
    "legacy_runtime_unverified",
];
pub const BEAT_OUTCOMES: &[&str] = &[
    "ok",
    "catalog_unreachable",
    "catalog_timeout",
    "catalog_error",
    "catalog_backpressure",
    "catalog_rejected",
    "unauthorized",
];
pub const LIFECYCLE_EVENTS: &[&str] = &[
    "runtime.link.started",
    "runtime.link.connected",
    "runtime.link.degraded",
    "runtime.link.reconnecting",
    "runtime.link.terminal",
    "runtime.link.stopped",
];

#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct HarnessEnvelope {
    pub kind: String,
    pub version: String,
    pub deployment_env: String,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Config {
    pub enabled: bool,
    pub token_env: String,
    pub control_plane_url: String,
    pub binary_path: String,
    pub binary_sha256: String,
    pub interval_s: u64,
    pub beat_timeout_s: u64,
    pub grace_s: u64,
    pub restart_min_s: u64,
    pub restart_max_s: u64,
    pub stable_reset_s: u64,
    pub stop_timeout_s: u64,
    pub log_count: u64,
    pub harness: HarnessEnvelope,
}
impl Default for Config {
    fn default() -> Self {
        Self {
            enabled: false,
            token_env: "KEI_RUNTIME_TOKEN".into(),
            control_plane_url: String::new(),
            binary_path: DEFAULT_BINARY_PATH.into(),
            binary_sha256: String::new(),
            interval_s: 60,
            beat_timeout_s: 10,
            grace_s: 15,
            restart_min_s: 1,
            restart_max_s: 300,
            stable_reset_s: 300,
            stop_timeout_s: 10,
            log_count: 3,
            harness: HarnessEnvelope::default(),
        }
    }
}
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ConfigError {
    pub code: &'static str,
    pub field: &'static str,
}
impl std::fmt::Display for ConfigError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(
            f,
            "runtimelink: invalid config: {}: {}",
            self.field, self.code
        )
    }
}
impl std::error::Error for ConfigError {}
fn error(code: &'static str, field: &'static str) -> ConfigError {
    ConfigError { code, field }
}
fn pattern(s: &str, allowed: impl Fn(char) -> bool, min: usize, max: usize) -> bool {
    let n = s.len();
    n >= min && n <= max && s.chars().all(allowed)
}
fn valid_id(s: &str) -> bool {
    pattern(
        s,
        |c| c.is_ascii_alphanumeric() || "._:-".contains(c),
        1,
        128,
    )
}
fn valid_version(s: &str) -> bool {
    pattern(
        s,
        |c| c.is_ascii_alphanumeric() || "._+-".contains(c),
        1,
        64,
    )
}
fn valid_envname(s: &str) -> bool {
    pattern(s, |c| c.is_ascii_alphanumeric() || "._-".contains(c), 1, 32)
}

pub fn normalize_config(mut c: Config) -> Result<Config, ConfigError> {
    if !c.harness.kind.is_empty() && !HARNESS_KINDS.contains(&c.harness.kind.as_str()) {
        return Err(error("harness_kind", "harness.kind"));
    }
    if c.enabled && c.harness.kind.is_empty() {
        return Err(error("harness_kind", "harness.kind"));
    }
    if !c.harness.version.is_empty() && !valid_version(&c.harness.version)
        || !c.harness.deployment_env.is_empty() && !valid_envname(&c.harness.deployment_env)
    {
        return Err(error("envelope", "harness"));
    }
    if c.enabled {
        if c.token_env != "KEI_RUNTIME_TOKEN" {
            return Err(error("invalid_value", "token"));
        }
        if !valid_url(&c.control_plane_url) {
            return Err(error("control_plane_url", "control_plane_url"));
        }
        if c.binary_path.is_empty() || c.binary_path.contains('\0') {
            return Err(error("binary", "binary.path"));
        }
        if c.binary_sha256.is_empty() && c.harness.deployment_env == "prod" {
            return Err(error("binary", "binary.sha256"));
        }
    }
    if !c.binary_sha256.is_empty()
        && !pattern(
            &c.binary_sha256,
            |x| x.is_ascii_digit() || ('a'..='f').contains(&x),
            64,
            64,
        )
    {
        return Err(error("binary", "binary.sha256"));
    }
    c.interval_s = c.interval_s.clamp(15, 300);
    if c.beat_timeout_s == 0 || c.beat_timeout_s.saturating_mul(2) >= c.interval_s {
        return Err(error("beat_timeout", "beat_timeout"));
    }
    if c.grace_s > 300
        || c.restart_min_s < 1
        || c.restart_max_s > 3600
        || c.restart_min_s > c.restart_max_s
        || c.stop_timeout_s < 1
        || c.stop_timeout_s > 60
        || c.log_count > 100
    {
        return Err(error("out_of_range", "config"));
    }
    if !(1..=3600).contains(&c.stable_reset_s) {
        return Err(error("out_of_range", "restart.stable_reset"));
    }
    Ok(c)
}
fn valid_url(s: &str) -> bool {
    let Some((scheme, rest)) = s.split_once("://") else {
        return false;
    };
    (scheme == "http" || scheme == "https")
        && !rest.is_empty()
        && !rest.starts_with('/')
        && !rest.contains('@')
        && rest.split('/').next().is_some_and(|h| !h.is_empty())
}
fn int_env(raw: &str) -> Result<u64, ConfigError> {
    if raw.is_empty() || raw.len() > 9 || !raw.bytes().all(|b| b.is_ascii_digit()) {
        return Err(error("invalid_value", "environment"));
    }
    raw.parse()
        .map_err(|_| error("invalid_value", "environment"))
}
pub fn config_from_env(
    envs: &BTreeMap<String, String>,
    harness: HarnessEnvelope,
) -> Result<Config, ConfigError> {
    let get = |k: &str| envs.get(k).map(|x| x.trim().to_owned()).unwrap_or_default();
    let mut c = Config::default();
    for (key, dst) in [
        ("KEI_HEARTBEAT_INTERVAL", &mut c.interval_s),
        ("KEI_HEARTBEAT_TIMEOUT", &mut c.beat_timeout_s),
        ("KEI_HEARTBEAT_RESTART_MIN", &mut c.restart_min_s),
        ("KEI_HEARTBEAT_RESTART_MAX", &mut c.restart_max_s),
        ("KEI_HEARTBEAT_STABLE_SECONDS", &mut c.stable_reset_s),
    ] {
        let x = get(key);
        if !x.is_empty() {
            *dst = int_env(&x).map_err(|_| error("invalid_value", env_name_field(key)))?
        }
    }
    let x = get("KEI_HEARTBEAT_LOG_COUNT");
    if !x.is_empty() {
        c.log_count = int_env(&x).map_err(|_| error("invalid_value", "KEI_HEARTBEAT_LOG_COUNT"))?
    }
    let token = !get("KEI_RUNTIME_TOKEN").is_empty();
    c.enabled = token;
    let enabled = get("KEI_RUNTIME_ENABLED");
    if !enabled.is_empty() {
        let b = match enabled.to_ascii_lowercase().as_str() {
            "true" | "1" => true,
            "false" | "0" => false,
            _ => return Err(error("invalid_value", "KEI_RUNTIME_ENABLED")),
        };
        if b && !token {
            return Err(error("token_missing", "KEI_RUNTIME_TOKEN"));
        }
        c.enabled = b
    }
    c.control_plane_url = get("KEI_RUNTIME_CONTROL_PLANE_URL");
    let path = get("KEI_PROXY_PATH");
    if !path.is_empty() {
        c.binary_path = path
    }
    c.binary_sha256 = get("KEI_PROXY_SHA");
    c.harness = HarnessEnvelope {
        kind: if harness.kind.is_empty() {
            get("KEI_HARNESS_KIND")
        } else {
            harness.kind
        },
        version: if harness.version.is_empty() {
            get("KEI_HARNESS_VERSION")
        } else {
            harness.version
        },
        deployment_env: if harness.deployment_env.is_empty() {
            get("KEI_DEPLOYMENT_ENV")
        } else {
            harness.deployment_env
        },
    };
    normalize_config(c)
}
fn env_name_field(s: &str) -> &'static str {
    match s {
        "KEI_HEARTBEAT_INTERVAL" => "KEI_HEARTBEAT_INTERVAL",
        "KEI_HEARTBEAT_TIMEOUT" => "KEI_HEARTBEAT_TIMEOUT",
        "KEI_HEARTBEAT_RESTART_MIN" => "KEI_HEARTBEAT_RESTART_MIN",
        "KEI_HEARTBEAT_RESTART_MAX" => "KEI_HEARTBEAT_RESTART_MAX",
        _ => "KEI_HEARTBEAT_STABLE_SECONDS",
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Backoff {
    pub min: Duration,
    pub max: Duration,
    pub stable_reset: Duration,
}
impl Backoff {
    pub fn delay_ms(self, attempt: i64, r: f64) -> u64 {
        let n = attempt.clamp(0, 30) as u32;
        let cap = self
            .max
            .as_millis()
            .min(self.min.as_millis().saturating_mul(1u128 << n));
        let rand = if r.is_nan() {
            0.0
        } else {
            r.clamp(0.0, f64::from_bits(1.0f64.to_bits() - 1))
        };
        (rand * (cap as f64)).floor() as u64
    }
}
pub fn backoff_delay_ms(min_ms: u64, max_ms: u64, attempt: i64, r: f64) -> u64 {
    Backoff {
        min: Duration::from_millis(min_ms),
        max: Duration::from_millis(max_ms),
        stable_reset: Duration::ZERO,
    }
    .delay_ms(attempt, r)
}

/// Passes allowlisted process variables only and overwrites both SDK identity values.
pub fn child_environment(
    source: &BTreeMap<String, String>,
    sdk_version: &str,
    harness: &HarnessEnvelope,
) -> BTreeMap<String, String> {
    let mut out = BTreeMap::new();
    for key in CHILD_ENV_ALLOWLIST {
        if *key != "KEI_AGENTWARE_SDK_LANG" && *key != "KEI_AGENTWARE_SDK_VERSION" {
            if let Some(v) = source.get(*key) {
                out.insert((*key).into(), v.clone());
            }
        }
    }
    out.insert("KEI_AGENTWARE_SDK_LANG".into(), "rust".into());
    out.insert("KEI_AGENTWARE_SDK_VERSION".into(), sdk_version.into());
    if !harness.kind.is_empty() {
        out.insert("KEI_HARNESS_KIND".into(), harness.kind.clone());
    }
    if !harness.version.is_empty() {
        out.insert("KEI_HARNESS_VERSION".into(), harness.version.clone());
    }
    if !harness.deployment_env.is_empty() {
        out.insert("KEI_DEPLOYMENT_ENV".into(), harness.deployment_env.clone());
    }
    out
}
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ChildKind {
    Heartbeat,
    AuditCollector,
}
/// Construct argv without a shell. Heartbeat is the JSONL child; collector is independently supervised.
pub fn build_command(binary: &str, kind: ChildKind, cfg: &Config) -> Command {
    let mut c = Command::new(binary);
    match kind {
        ChildKind::Heartbeat => {
            c.args(["runtime", "heartbeat", "--interval"])
                .arg(format!("{}s", cfg.interval_s))
                .arg("--timeout")
                .arg(format!("{}s", cfg.beat_timeout_s))
                .args(["--output", "jsonl", "--parent-stdin"]);
        }
        ChildKind::AuditCollector => {
            c.arg("collector");
        }
    }
    c
}
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ExitAction {
    Clean,
    Restart(&'static str),
    Terminal(&'static str),
}
pub fn handle_child_exit(code: i32) -> ExitAction {
    match code {
        0 => ExitAction::Clean,
        1 => ExitAction::Restart("runtime_unavailable"),
        2 => ExitAction::Terminal("contract_mismatch"),
        3 => ExitAction::Terminal("installation_unauthorized"),
        _ => ExitAction::Terminal("config_invalid"),
    }
}

/// Returns a redacted lifecycle JSON record, validating closed sets and fields first.
pub fn redact_lifecycle(input: &Value) -> Result<Value, &'static str> {
    let obj = input.as_object().ok_or("invalid")?;
    let get_s = strval;
    let name = get_s(obj, "name");
    let state = get_s(obj, "state");
    if !LIFECYCLE_EVENTS.contains(&name) || !LINK_STATES.contains(&state) {
        return Err("invalid");
    }
    let h = obj
        .get("harness")
        .and_then(Value::as_object)
        .ok_or("invalid")?;
    let kind = get_s(h, "kind");
    let lang = get_s(h, "sdk_lang");
    if !HARNESS_KINDS.contains(&kind) || !["go", "python", "typescript"].contains(&lang) {
        return Err("invalid");
    }
    let reason = get_s(obj, "reason");
    if !reason.is_empty() && !FAILURE_CLASSES.contains(&reason) {
        return Err("invalid");
    }
    let run = get_s(obj, "run_id");
    let sdk = get_s(obj, "sdk_instance_id");
    if !run.is_empty() && !valid_id(run) || !valid_id(sdk) {
        return Err("invalid");
    }
    for x in [get_s(h, "version"), get_s(h, "sdk_version")] {
        if !x.is_empty() && !valid_version(x) {
            return Err("invalid");
        }
    }
    let de = get_s(h, "deployment_env");
    if !de.is_empty() && !valid_envname(de) {
        return Err("invalid");
    }
    let at = get_s(obj, "at");
    if !timestamp(at) {
        return Err("invalid");
    }
    for key in ["seq", "restarts", "consecutive_fails"] {
        if obj.get(key).is_some_and(|v| {
            v.as_i64()
                .is_none_or(|n| !(0..=9_007_199_254_740_991).contains(&n))
        }) {
            return Err("invalid");
        }
    }
    let harness = json!({"kind":kind,"version":get_s(h,"version"),"deployment_env":de,"sdk_lang":lang,"sdk_version":get_s(h,"sdk_version")});
    Ok(
        json!({"name":name,"at":normalize_time(at),"state":state,"reason":reason,"run_id":run,"seq":obj.get("seq").and_then(Value::as_u64).unwrap_or(0),"sdk_instance_id":sdk,"harness":harness,"restarts":obj.get("restarts").and_then(Value::as_u64).unwrap_or(0),"consecutive_fails":obj.get("consecutive_fails").and_then(Value::as_u64).unwrap_or(0)}),
    )
}
fn strval<'a>(o: &'a Map<String, Value>, k: &str) -> &'a str {
    o.get(k).and_then(Value::as_str).unwrap_or("")
}
fn timestamp(s: &str) -> bool {
    let b = s.as_bytes();
    if b.len() < 20
        || ![4, 7].iter().all(|i| b[*i] == b'-')
        || b[10] != b'T'
        || b[13] != b':'
        || b[16] != b':'
    {
        return false;
    }
    for range in [0..4, 5..7, 8..10, 11..13, 14..16, 17..19] {
        if !b[range].iter().all(u8::is_ascii_digit) {
            return false;
        }
    }
    let mut i = 19;
    if b.get(i) == Some(&b'.') {
        i += 1;
        let start = i;
        while b.get(i).is_some_and(u8::is_ascii_digit) {
            i += 1;
        }
        if i - start == 0 || i - start > 9 {
            return false;
        }
    }
    if b.get(i) == Some(&b'Z') {
        return i + 1 == b.len();
    }
    b.len() == i + 6
        && matches!(b.get(i), Some(b'+') | Some(b'-'))
        && b[i + 3] == b':'
        && b[i + 1..i + 3].iter().all(u8::is_ascii_digit)
        && b[i + 4..i + 6].iter().all(u8::is_ascii_digit)
}
fn normalize_time(s: &str) -> String {
    if s.ends_with('Z') && s.len() == 20 {
        format!("{}.000Z", &s[..19])
    } else if s.ends_with('Z') && s.contains('.') {
        let (a, b) = s.split_once('.').unwrap();
        let frac = b.trim_end_matches('Z');
        format!("{}.{:0<3}Z", a, &frac[..frac.len().min(3)])
    } else {
        s.to_owned()
    }
}

#[derive(Clone, Debug, PartialEq)]
pub enum ChildEvent {
    Identity(Value),
    Beat(Value),
    Terminal(Value),
    Ignored,
}
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum WireError {
    LineTooLong,
    ContractMismatch,
    Malformed,
}
pub fn parse_child_line(bytes: &[u8]) -> Result<ChildEvent, WireError> {
    let mut line = bytes.strip_suffix(b"\n").unwrap_or(bytes);
    line = line.strip_suffix(b"\r").unwrap_or(line);
    if line.len() > MAX_CHILD_LINE_BYTES {
        return Err(WireError::LineTooLong);
    }
    let v: Value = serde_json::from_slice(line).map_err(|_| WireError::Malformed)?;
    let o = v.as_object().ok_or(WireError::Malformed)?;
    if o.get("v").and_then(Value::as_u64) != Some(1) {
        return Err(WireError::ContractMismatch);
    }
    let e = o
        .get("event")
        .and_then(Value::as_str)
        .ok_or(WireError::Malformed)?;
    let mut out = Map::new();
    let copy = |src: &Map<String, Value>,
                dst: &mut Map<String, Value>,
                key: &str,
                required: bool|
     -> Result<(), WireError> {
        match src.get(key) {
            Some(v) => {
                dst.insert(key.into(), v.clone());
                Ok(())
            }
            None if required => Err(WireError::Malformed),
            None => Ok(()),
        }
    };
    match e {
        "identity" => {
            for k in ["run_id", "installation_id", "org_id"] {
                copy(o, &mut out, k, true)?
            }
            for k in [
                "workspace_id",
                "platform",
                "status",
                "binding_status",
                "runtime_version",
            ] {
                copy(o, &mut out, k, false)?;
                out.entry(k).or_insert(json!(""));
            }
            for k in [
                "run_id",
                "installation_id",
                "org_id",
                "workspace_id",
                "platform",
                "status",
                "binding_status",
                "runtime_version",
            ] {
                if let Some(v) = out.get(k).and_then(Value::as_str) {
                    if !v.is_empty() && !valid_id(v) {
                        return Err(WireError::Malformed);
                    }
                }
            }
            let agent = o
                .get("agent_id")
                .and_then(Value::as_str)
                .filter(|x| valid_id(x))
                .unwrap_or("");
            let mut dropped = usize::from(o.contains_key("agent_id") && agent.is_empty());
            let mut agents = Vec::new();
            if let Some(raw) = o.get("agents") {
                if let Some(arr) = raw.as_array() {
                    for a in arr {
                        if let Some(a) = a.as_object() {
                            let id = a.get("agent_id").and_then(Value::as_str).unwrap_or("");
                            let def = a.get("is_default");
                            if valid_id(id) && def.is_none_or(Value::is_boolean) {
                                agents.push(json!({"agent_id":id,"is_default":def.and_then(Value::as_bool).unwrap_or(false)}));
                            } else {
                                dropped += 1
                            }
                        } else {
                            dropped += 1
                        }
                    }
                } else {
                    dropped += 1
                }
            }
            let default = if agent.is_empty() {
                agents
                    .iter()
                    .find(|a| a["is_default"] == true)
                    .and_then(|a| a["agent_id"].as_str())
                    .unwrap_or("")
            } else {
                agent
            };
            out.insert("agent_id".into(), json!(default));
            out.insert("agents".into(), json!(agents));
            Ok(ChildEvent::Identity(
                json!({"fields":out,"dropped_agents":dropped}),
            ))
        }
        "beat" => {
            copy(o, &mut out, "run_id", true)?;
            let run = out["run_id"].as_str().unwrap_or("");
            if !valid_id(run) {
                return Err(WireError::Malformed);
            }
            for k in ["seq", "latency_ms", "next_in_ms"] {
                copy(o, &mut out, k, false)?;
            }
            for k in ["seq", "latency_ms", "next_in_ms"] {
                if let Some(x) = out.get(k) {
                    let n = x.as_u64().ok_or(WireError::Malformed)?;
                    if (k == "seq" && n > 9_007_199_254_740_991) || (k != "seq" && n > 3_600_000) {
                        return Err(WireError::Malformed);
                    }
                }
            }
            copy(o, &mut out, "at", true)?;
            if !timestamp(out["at"].as_str().unwrap_or("")) {
                return Err(WireError::Malformed);
            }
            copy(o, &mut out, "outcome", true)?;
            if !BEAT_OUTCOMES.contains(&out["outcome"].as_str().unwrap_or("")) {
                return Err(WireError::ContractMismatch);
            }
            copy(o, &mut out, "http_status", false)?;
            if let Some(n) = out.get("http_status") {
                let n = n.as_u64().ok_or(WireError::Malformed)?;
                if n != 0 && !(100..=599).contains(&n) {
                    return Err(WireError::Malformed);
                }
            }
            for k in ["seq", "http_status", "latency_ms", "next_in_ms"] {
                out.entry(k).or_insert(json!(0));
            }
            Ok(ChildEvent::Beat(Value::Object(out)))
        }
        "terminal" => {
            copy(o, &mut out, "run_id", true)?;
            copy(o, &mut out, "reason", true)?;
            if !valid_id(out["run_id"].as_str().unwrap_or("")) {
                return Err(WireError::Malformed);
            }
            let r = out["reason"].as_str().unwrap_or("");
            if !pattern(r, |c| c.is_ascii_lowercase() || c == '_', 1, 64) {
                return Err(WireError::Malformed);
            }
            Ok(ChildEvent::Terminal(Value::Object(out)))
        }
        _ => Ok(ChildEvent::Ignored),
    }
}

/// Spawns a child with an explicit environment map; no inherited variables leak through.
pub fn spawn_child(
    binary: &str,
    kind: ChildKind,
    cfg: &Config,
    source: &BTreeMap<String, String>,
    sdk_version: &str,
) -> std::io::Result<std::process::Child> {
    let mut cmd = build_command(binary, kind, cfg);
    cmd.stdin(std::process::Stdio::piped())
        .stdout(std::process::Stdio::null())
        .stderr(std::process::Stdio::null());
    cmd.env_clear();
    for (k, v) in child_environment(source, sdk_version, &cfg.harness) {
        cmd.env(k, v);
    }
    cmd.spawn()
}
/// Reads the current process environment as key/value pairs for use with spawn_child.
pub fn process_environment() -> BTreeMap<String, String> {
    env::vars().collect()
}

/// Owns heartbeat and audit collector child supervisors. `stop` kills both
/// children and joins their supervisor threads; each child has independent retry state.
pub struct RuntimeLink {
    stop: std::sync::Arc<std::sync::atomic::AtomicBool>,
    threads: Vec<std::thread::JoinHandle<()>>,
}
impl RuntimeLink {
    /// Starts both `kei-proxy runtime heartbeat` and `kei-proxy audit collector`.
    pub fn start(
        config: Config,
        source_env: BTreeMap<String, String>,
        sdk_version: impl Into<String>,
    ) -> Result<Self, ConfigError> {
        let config = normalize_config(config)?;
        let stop = std::sync::Arc::new(std::sync::atomic::AtomicBool::new(false));
        if !config.enabled {
            return Ok(Self {
                stop,
                threads: Vec::new(),
            });
        }
        let sdk_version = sdk_version.into();
        let mut threads = Vec::new();
        for kind in [ChildKind::Heartbeat, ChildKind::AuditCollector] {
            let stop_flag = stop.clone();
            let cfg = config.clone();
            let envs = source_env.clone();
            let version = sdk_version.clone();
            threads.push(std::thread::spawn(move || {
                supervise_child(kind, cfg, envs, version, stop_flag)
            }));
        }
        Ok(Self { stop, threads })
    }
    /// Requests cancellation and waits for supervisors to reap their child processes.
    pub fn stop(mut self) {
        self.stop.store(true, std::sync::atomic::Ordering::SeqCst);
        for thread in self.threads.drain(..) {
            let _ = thread.join();
        }
    }
}
fn supervise_child(
    kind: ChildKind,
    cfg: Config,
    envs: BTreeMap<String, String>,
    version: String,
    stop: std::sync::Arc<std::sync::atomic::AtomicBool>,
) {
    use std::sync::atomic::Ordering;
    let mut attempt = 0i64;
    while !stop.load(Ordering::SeqCst) {
        let started = std::time::Instant::now();
        let Ok(mut child) = spawn_child(&cfg.binary_path, kind, &cfg, &envs, &version) else {
            attempt += 1;
            if sleep_backoff(&cfg, attempt - 1, &stop) {
                break;
            }
            continue;
        };
        let code = loop {
            if stop.load(Ordering::SeqCst) {
                let _ = child.kill();
                let _ = child.wait();
                return;
            }
            match child.try_wait() {
                Ok(Some(status)) => break status.code().unwrap_or(1),
                Ok(None) => std::thread::sleep(Duration::from_millis(50)),
                Err(_) => {
                    let _ = child.kill();
                    let _ = child.wait();
                    break 1;
                }
            }
        };
        if stop.load(Ordering::SeqCst) {
            break;
        }
        if started.elapsed() >= Duration::from_secs(cfg.stable_reset_s) {
            attempt = 0;
        }
        match handle_child_exit(code) {
            ExitAction::Clean => return,
            ExitAction::Terminal(_) => return,
            ExitAction::Restart(_) => {
                attempt += 1;
                if sleep_backoff(&cfg, attempt - 1, &stop) {
                    break;
                }
            }
        }
    }
}
fn sleep_backoff(cfg: &Config, attempt: i64, stop: &std::sync::atomic::AtomicBool) -> bool {
    use std::sync::atomic::Ordering;
    let jitter = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap_or_default()
        .subsec_nanos() as f64
        / 1_000_000_000.0;
    let ms = backoff_delay_ms(
        cfg.restart_min_s * 1000,
        cfg.restart_max_s * 1000,
        attempt,
        jitter,
    );
    let deadline = std::time::Instant::now() + Duration::from_millis(ms);
    while std::time::Instant::now() < deadline {
        if stop.load(Ordering::SeqCst) {
            return true;
        }
        std::thread::sleep(
            Duration::from_millis(20)
                .min(deadline.saturating_duration_since(std::time::Instant::now())),
        );
    }
    false
}

impl Drop for RuntimeLink {
    fn drop(&mut self) {
        self.stop.store(true, std::sync::atomic::Ordering::SeqCst);
        for thread in self.threads.drain(..) {
            let _ = thread.join();
        }
    }
}
