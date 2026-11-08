//! Fail-closed policy evaluation through the Kei runtime socket or the
//! `kei-proxy authorize` CLI fallback.

mod cli;

use crate::runtimesocket::{Client, ErrorKind, Identity, SOCKET_PATH_ENV};
use serde_json::{Map, Value, json};
use sha2::{Digest, Sha256};
use std::{cell::RefCell, collections::HashMap, path::PathBuf, time::Duration};

pub use self::cli::{
    AUTHORIZE_CHILD_ENV_ALLOWLIST, CliClient, RUNTIME_TOKEN_ENV, authorize_argv,
    parse_authorize_output,
};

pub const RULE: &str = "kei-proxy";

/// Reason classes shared with go/kei/evaluator (the fixture's reason_classes).
pub const REASON_MISSING_TOKEN: &str = "missing_token";
pub const REASON_PROXY_UNAVAILABLE: &str = "proxy_unavailable";
pub const REASON_PROXY_TIMEOUT: &str = "proxy_timeout";
pub const REASON_PROXY_ERROR: &str = "proxy_error";
pub const REASON_MALFORMED_RESPONSE: &str = "malformed_response";
pub const REASON_EMPTY_RESPONSE: &str = "empty_response";
pub const REASON_EXIT_MISMATCH: &str = "exit_mismatch";
pub const REASON_PIN_MISMATCH: &str = "pin_mismatch";

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Action {
    Allow,
    Deny,
    Filter,
}

#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct CallerContext {
    pub user_id: String,
    pub session_id: String,
    pub role: String,
    pub source: String,
    pub trusted: bool,
    pub metadata: HashMap<String, String>,
    pub invoking_subject: String,
    pub parent_span: String,
    pub delegation_depth: i32,
}

impl CallerContext {
    /// Create a child context while preserving the original human subject.
    pub fn delegate(&self, parent_span: &str) -> Self {
        let mut child = self.clone();
        if !parent_span.is_empty() {
            child.parent_span = parent_span.into();
        }
        child.delegation_depth += 1;
        child
    }
}

#[derive(Debug, Clone, PartialEq)]
pub struct Decision {
    pub action: Action,
    pub rule: String,
    pub reason: String,
    pub reason_code: Option<String>,
    pub hint: Option<String>,
    pub enrollment: Option<Value>,
    pub connect: Option<Value>,
    /// Replacement values applied to tool arguments for a FILTER decision.
    pub redacted_args: Option<Value>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct AuthorizeRequest {
    pub user_id: String,
    pub tool: String,
    pub action: String,
    pub resource: String,
    pub span_id: String,
    pub invoking_subject: String,
    pub parent_span: String,
    pub delegation_depth: i32,
    pub agent_version: String,
    pub framework: String,
    pub tool_args_digest: String,
    pub resources: Vec<String>,
    pub workspace_id: String,
}

#[derive(Debug, Clone, PartialEq)]
pub struct AuthorizeResponse {
    pub decision: String,
    pub reason: Option<String>,
    pub policy_id: Option<String>,
    pub reason_code: Option<String>,
    pub hint: Option<String>,
    pub enrollment: Option<Value>,
    pub connect: Option<Value>,
}

/// One authorize transport: request in, JSON response out (Go: the
/// evaluator.Client interface). Socket clients manage sessions internally;
/// the CLI client spawns `kei-proxy authorize` once per call.
pub trait AuthorizationClient {
    fn authorize(&mut self, req: &AuthorizeRequest) -> Result<Value, ClientError>;
}

impl<T: AuthorizationClient + ?Sized> AuthorizationClient for Box<T> {
    fn authorize(&mut self, req: &AuthorizeRequest) -> Result<Value, ClientError> {
        (**self).authorize(req)
    }
}

/// A transport-level authorize failure: the reason class and a detail safe for
/// reasons (Go: AuthorizeError). The runtime token and any child stderr never
/// land in either field.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ClientError {
    pub class: String,
    pub detail: String,
}

impl ClientError {
    pub fn new(class: &str, detail: impl Into<String>) -> Self {
        Self {
            class: class.into(),
            detail: detail.into(),
        }
    }

    /// Socket errors map to the Go reason classes; the socket's own message is
    /// dropped — daemon response bodies and messages are never retained.
    pub fn from_kind(kind: ErrorKind) -> Self {
        Self::new(kind_class(kind), "")
    }
}

fn kind_class(kind: ErrorKind) -> &'static str {
    match kind {
        ErrorKind::ProxyUnavailable => REASON_PROXY_UNAVAILABLE,
        ErrorKind::ProxyTimeout => REASON_PROXY_TIMEOUT,
        ErrorKind::MissingToken => REASON_MISSING_TOKEN,
        ErrorKind::MalformedResponse => REASON_MALFORMED_RESPONSE,
        ErrorKind::ProxyError | ErrorKind::Cancelled | ErrorKind::InvalidArgument => {
            REASON_PROXY_ERROR
        }
    }
}

impl AuthorizationClient for Client {
    fn authorize(&mut self, req: &AuthorizeRequest) -> Result<Value, ClientError> {
        let source = if req.framework.is_empty() {
            "agentware"
        } else {
            req.framework.as_str()
        };
        let identity = Identity {
            source: source.into(),
            external_id: req.user_id.clone(),
        };
        let session = self
            .open_session(identity)
            .map_err(|error| ClientError::from_kind(error.kind))?;
        let fields = request_fields(req);
        self.authorize(&session, &fields)
            .map_err(|error| ClientError::from_kind(error.kind))
    }
}

/// The socket client as an authorization client (Go:
/// runtimesocket.AuthorizationClient). `source` is the session source for
/// every subject: the identity namespace the daemon resolves the user in.
pub struct SocketAuthorizationClient {
    pub source: String,
    pub socket: Client,
}

impl AuthorizationClient for SocketAuthorizationClient {
    fn authorize(&mut self, req: &AuthorizeRequest) -> Result<Value, ClientError> {
        if req.user_id.is_empty() {
            return Err(ClientError::new(
                REASON_PROXY_ERROR,
                "no invoking subject to open a session for",
            ));
        }
        let identity = Identity {
            source: self.source.clone(),
            external_id: req.user_id.clone(),
        };
        let session = self
            .socket
            .open_session(identity)
            .map_err(|error| ClientError::from_kind(error.kind))?;
        let fields = request_fields(req);
        self.socket
            .authorize(&session, &fields)
            .map_err(|error| ClientError::from_kind(error.kind))
    }
}

/// SelectOptions configures [`select_authorization_client`].
#[derive(Debug, Clone, Default)]
pub struct SelectOptions {
    /// Selects the socket client explicitly.
    pub socket_path: String,
    /// Where KEI_RUNTIME_SOCKET_PATH and KEI_RUNTIME_TOKEN are read (by
    /// either client). None means the process environment.
    pub env: Option<HashMap<String, String>>,
    /// Bounds one authorize call. Zero means each client's default.
    pub timeout: Duration,
    /// The subprocess client's executable; empty means "kei-proxy".
    pub executable: String,
    /// Pins the subprocess executable to this SHA-256 (64 hex digits).
    pub expected_sha256: String,
}

/// Selects the authorization client for [`KeiProxyEvaluator`]: the socket
/// client when a socket is configured (`socket_path`, or
/// `KEI_RUNTIME_SOCKET_PATH` in `env`), otherwise the `kei-proxy authorize`
/// subprocess client. The choice is made once, here: a configured socket that
/// is down is a refusal, never a fall back to the subprocess. An empty source
/// is [`InvalidSourceError`] (Go: runtimesocket.SelectAuthorizationClient).
pub fn select_authorization_client(
    source: &str,
    opts: &SelectOptions,
) -> Result<Box<dyn AuthorizationClient>, InvalidSourceError> {
    if source.trim().is_empty() {
        return Err(InvalidSourceError);
    }
    let mut path = opts.socket_path.clone();
    if path.is_empty() {
        path = match &opts.env {
            Some(env) => env.get(SOCKET_PATH_ENV).cloned().unwrap_or_default(),
            None => std::env::var(SOCKET_PATH_ENV).unwrap_or_default(),
        };
    }
    if !path.is_empty() {
        let mut socket = Client::new(path);
        socket.timeout = opts.timeout;
        socket.env = opts.env.clone().unwrap_or_default();
        return Ok(Box::new(SocketAuthorizationClient {
            source: source.to_string(),
            socket,
        }));
    }
    Ok(Box::new(CliClient {
        executable: PathBuf::from(opts.executable.as_str()),
        expected_sha256: (!opts.expected_sha256.is_empty()).then(|| opts.expected_sha256.clone()),
        timeout: opts.timeout,
        env: opts.env.clone(),
    }))
}

/// An empty session source for [`select_authorization_client`] (Go:
/// runtimesocket.ErrInvalidSource).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct InvalidSourceError;

impl std::fmt::Display for InvalidSourceError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "select authorization client: source must be non-empty")
    }
}

impl std::error::Error for InvalidSourceError {}

/// Stateful evaluator. Runtime identity is derived from caller context and the
/// session is opened over the socket for each previously unseen identity.
pub struct KeiProxyEvaluator {
    client: RefCell<Box<dyn AuthorizationClient>>,
    default_action: String,
}

impl KeiProxyEvaluator {
    pub fn new(client: Client) -> Self {
        Self::with_client(client)
    }

    /// Evaluate through any authorization client — the socket client or the
    /// `kei-proxy authorize` CLI fallback.
    pub fn with_client(client: impl AuthorizationClient + 'static) -> Self {
        Self {
            client: RefCell::new(Box::new(client)),
            default_action: "execute".into(),
        }
    }

    pub fn with_default_action(mut self, action: impl Into<String>) -> Self {
        self.default_action = action.into();
        self
    }

    pub fn evaluate(&self, tool: &str, args: &Value, caller: &CallerContext) -> Decision {
        let user = if caller.invoking_subject.is_empty() {
            &caller.user_id
        } else {
            &caller.invoking_subject
        };
        let metadata = &caller.metadata;
        let framework = metadata
            .get("framework")
            .filter(|s| !s.is_empty())
            .map(String::as_str)
            .unwrap_or(&caller.source);
        let action = args
            .get("action")
            .filter(|v| !v.is_null())
            .map(value_text)
            .filter(|s| !s.is_empty())
            .unwrap_or_else(|| self.default_action.clone());
        let resources = resources_touched(tool, args);
        let req = AuthorizeRequest {
            user_id: user.clone(),
            tool: tool.into(),
            action,
            resource: resources.first().cloned().unwrap_or_else(|| tool.into()),
            span_id: metadata.get("span_id").cloned().unwrap_or_default(),
            invoking_subject: user.clone(),
            parent_span: caller.parent_span.clone(),
            delegation_depth: caller.delegation_depth,
            agent_version: metadata.get("agent_version").cloned().unwrap_or_default(),
            framework: framework.into(),
            tool_args_digest: tool_args_digest(args),
            resources,
            workspace_id: metadata.get("workspace_id").cloned().unwrap_or_default(),
        };
        let result = self.client.borrow_mut().authorize(&req);
        match result {
            Ok(value) => response_decision(parse_response(&value)),
            Err(error) => error_decision(error),
        }
    }
}

fn request_fields(r: &AuthorizeRequest) -> Map<String, Value> {
    let mut fields = Map::new();
    for (key, value) in [
        ("tool", json!(r.tool)),
        ("action", json!(r.action)),
        ("resource", json!(r.resource)),
        ("span_id", json!(r.span_id)),
        ("parent_span", json!(r.parent_span)),
        ("delegation_depth", json!(r.delegation_depth)),
        ("framework", json!(r.framework)),
        ("tool_args_digest", json!(r.tool_args_digest)),
    ] {
        fields.insert(key.into(), value);
    }
    fields
}

fn parse_response(v: &Value) -> AuthorizeResponse {
    // The socket client validates the schema itself; the evaluator reads the
    // decision from whatever object the transport returned (Go:
    // responseDecision). A missing or non-string decision means no decision.
    AuthorizeResponse {
        decision: v
            .get("decision")
            .filter(|d| !d.is_null())
            .map(stringify)
            .unwrap_or_default()
            .trim()
            .to_ascii_lowercase(),
        reason: v.get("reason").and_then(Value::as_str).map(str::to_owned),
        policy_id: v
            .get("policy_id")
            .or_else(|| v.get("policy"))
            .and_then(Value::as_str)
            .map(str::to_owned),
        reason_code: v
            .get("reason_code")
            .and_then(Value::as_str)
            .map(str::to_owned),
        hint: v.get("hint").and_then(Value::as_str).map(str::to_owned),
        enrollment: v.get("enrollment").cloned(),
        connect: v.get("connect").cloned(),
    }
}

/// Go: stringify — renders a decoded JSON scalar the way Python's str() would
/// for the values kei-proxy emits: integral floats without a fraction.
fn stringify(v: &Value) -> String {
    match v {
        Value::String(s) => s.clone(),
        Value::Number(number) => {
            if let Some(float) = number.as_f64() {
                if float.is_finite() && float.fract() == 0.0 {
                    return format!("{float:.0}");
                }
            }
            number.to_string()
        }
        _ => v.to_string(),
    }
}

fn response_decision(r: AuthorizeResponse) -> Decision {
    let rule = r
        .policy_id
        .filter(|s| !s.is_empty())
        .unwrap_or_else(|| RULE.into());
    if r.decision == "allow" || r.decision == "permit" {
        return Decision {
            action: Action::Allow,
            rule,
            reason: reason("allow", r.reason.as_deref()),
            reason_code: r.reason_code,
            hint: r.hint,
            enrollment: None,
            connect: None,
            redacted_args: None,
        };
    }
    // Enrollment and connect payloads are kept only when they are JSON
    // objects (Go: .(map[string]any)), and only for deny and
    // enrollment_required.
    let carried_enrollment = r.enrollment.clone().filter(|v| v.is_object());
    let carried_connect = r.connect.clone().filter(|v| v.is_object());
    let (class, detail, enrollment, connect) = match r.decision.as_str() {
        "deny" => (
            "deny",
            r.reason.as_deref().unwrap_or(""),
            carried_enrollment,
            carried_connect,
        ),
        "enrollment_required" => (
            "enrollment_required",
            r.reason
                .as_deref()
                .filter(|s| !s.is_empty())
                .unwrap_or("enrollment is required before this call"),
            carried_enrollment,
            carried_connect,
        ),
        "" => ("no_decision", "", None, None),
        other => {
            let mut unknown = format!("'{other}'");
            if let Some(proxy_reason) = r.reason.as_deref().filter(|s| !s.is_empty()) {
                unknown.push_str(&format!(" ({proxy_reason})"));
            }
            return Decision {
                action: Action::Deny,
                rule,
                reason: reason("unknown_decision", Some(&unknown)),
                reason_code: r.reason_code,
                hint: r.hint,
                enrollment: None,
                connect: None,
                redacted_args: None,
            };
        }
    };
    Decision {
        action: Action::Deny,
        rule,
        reason: reason(class, Some(detail)),
        hint: r.hint.or_else(|| hint_for(r.reason_code.as_deref())),
        reason_code: r.reason_code,
        enrollment,
        connect,
        redacted_args: None,
    }
}

fn error_decision(error: ClientError) -> Decision {
    deny(&error.class, &error.detail)
}
fn deny(class: &str, detail: &str) -> Decision {
    Decision {
        action: Action::Deny,
        rule: RULE.into(),
        reason: reason(class, Some(detail)),
        reason_code: None,
        hint: None,
        enrollment: None,
        connect: None,
        redacted_args: None,
    }
}
fn reason(class: &str, detail: Option<&str>) -> String {
    match detail.filter(|d| !d.is_empty()) {
        Some(d) => format!("kei-proxy {class}: {d}"),
        None => format!("kei-proxy {class}"),
    }
}
/// Known remediation text from docs/deny-hints.md. Unknown codes get no hint.
pub fn hint_for(code: Option<&str>) -> Option<String> {
    let hint = match code? {
        "agent_not_bound_to_connector" => {
            "Bind the agent to the connector on the console Connectors page (kei-cli has no bind command)."
        }
        "agent_not_assigned" => {
            "Assign an agent to this runtime installation on the console Agents page so the runtime identity has a default agent."
        }
        "credential_unavailable" => {
            "Connect the provider on the console Connectors page (or deliver the credential) so the runtime can resolve the connector credential."
        }
        "descriptor_invalid" => {
            "The connector metadata is invalid. Check the connector setup on the console Connectors page."
        }
        "enrollment_required" => {
            "Link the provider identity to a Kei user on the console Users page to enable connector access."
        }
        "insecure_transport" => {
            "OAuth credentials are only returned over TLS. Use HTTPS when connecting to the catalog."
        }
        "no_matching_policy" => {
            "Add a policy for this source and target: kei policies create --name <name> --src-pattern <src> --dst-pattern <dst> --effect permit"
        }
        "oauth_binding_mismatch" => {
            "The OAuth connection is bound to another scope. Reconnect the provider in the correct workspace on the console Connectors page."
        }
        "oauth_bridge_unavailable" => {
            "The OAuth bridge is unavailable. Check the bridge configuration and ensure the service is reachable."
        }
        "oauth_not_connected" => {
            "Connect the provider account on the console Connectors page to enable OAuth-based access."
        }
        "oauth_reconnect_required" => {
            "Reconnect the provider account on the console Connectors page; the connection needs to be refreshed."
        }
        "policy_deny" => {
            "Add a permit policy for this subject and connector: kei policies create --name <name> --src-pattern <src> --dst-pattern <dst> --effect permit"
        }
        "preflight_required" => {
            "Register the tool so the active policy bundle lists it (POST /v1/tools:register on the runtime socket, or kei harness sync --harness custom --manifest tools.json), then wait for the next bundle refresh."
        }
        "subject_missing" => {
            "Link the invoking user to this installation on the console Users page so a subject can be resolved."
        }
        "subject_unlinked" => {
            "Link this identity to the installation: open the enrollment link when the response carries one, or link the user on the console Users page."
        }
        "tool_not_registered" => {
            "Register the harness tools: kei harness sync --harness custom --manifest tools.json"
        }
        _ => return None,
    };
    Some(hint.into())
}

fn resources_touched(tool: &str, args: &Value) -> Vec<String> {
    let head = tool
        .split('.')
        .next()
        .unwrap_or("")
        .split('_')
        .next()
        .unwrap_or("")
        .to_ascii_lowercase();
    if head.is_empty() {
        return Vec::new();
    }
    let get = |keys: &[&str]| {
        keys.iter().find_map(|k| {
            args.get(*k)
                .filter(|v| !v.is_null() && *v != "" && *v != 0)
                .map(value_text)
                .filter(|s| !s.trim().is_empty())
        })
    };
    let mut out = Vec::new();
    let owner = get(&["owner"]);
    let repo = get(&["repo", "repository"]);
    if let Some(repo) = repo {
        out.push(format!(
            "{head}:repo:{}",
            owner.map(|o| format!("{o}/{repo}")).unwrap_or(repo)
        ));
    }
    for (kind, keys) in [
        ("issue", &["issue_number"][..]),
        ("pull_request", &["pull_number", "pr_number"]),
        ("branch", &["branch", "ref"]),
        ("file", &["path", "file_path"]),
    ] {
        if let Some(v) = get(keys) {
            let r = format!("{head}:{kind}:{v}");
            if !out.contains(&r) {
                out.push(r);
            }
        }
    }
    out
}
fn value_text(v: &Value) -> String {
    v.as_str()
        .map(str::to_owned)
        .unwrap_or_else(|| v.to_string())
}
fn tool_args_digest(args: &Value) -> String {
    // Go takes map[string]any (nil -> {}): a non-object digests as "{}".
    let canonical = if args.is_object() {
        canonical_json(args)
    } else {
        "{}".to_string()
    };
    let mut ascii = String::with_capacity(canonical.len());
    for c in canonical.chars() {
        if c.is_ascii() {
            ascii.push(c);
        } else {
            for unit in c.encode_utf16(&mut [0; 2]) {
                ascii.push_str(&format!(r"\u{unit:04x}"));
            }
        }
    }
    format!("{:x}", Sha256::digest(ascii.as_bytes()))
}

/// Compact JSON with object keys sorted at every nesting level — byte for byte
/// Go's ToolArgsDigest canonical form (encoding/json sorts map keys, Python's
/// json.dumps(sort_keys=True, separators=(",", ":"))). Arrays keep their order.
fn canonical_json(value: &Value) -> String {
    match value {
        Value::Object(entries) => {
            let mut entries: Vec<(&String, &Value)> = entries.iter().collect();
            entries.sort_by(|left, right| left.0.cmp(right.0));
            let mut out = String::from("{");
            for (index, (key, value)) in entries.into_iter().enumerate() {
                if index > 0 {
                    out.push(',');
                }
                out.push_str(&serde_json::to_string(key).unwrap_or_default());
                out.push(':');
                out.push_str(&canonical_json(value));
            }
            out.push('}');
            out
        }
        _ => value.to_string(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::os::unix::fs::PermissionsExt;

    #[test]
    fn response_decisions_follow_the_fail_closed_table() {
        let cases = [
            ("allow", Action::Allow, "kei-proxy allow"),
            ("permit", Action::Allow, "kei-proxy allow"),
            ("deny", Action::Deny, "kei-proxy deny"),
            (
                "enrollment_required",
                Action::Deny,
                "kei-proxy enrollment_required: enrollment is required before this call",
            ),
            ("", Action::Deny, "kei-proxy no_decision"),
            (
                "filter",
                Action::Deny,
                "kei-proxy unknown_decision: 'filter'",
            ),
        ];
        for (proxy_decision, action, reason) in cases {
            let d = response_decision(AuthorizeResponse {
                decision: proxy_decision.into(),
                reason: None,
                policy_id: None,
                reason_code: None,
                hint: None,
                enrollment: None,
                connect: None,
            });
            assert_eq!(d.action, action, "{proxy_decision}");
            assert_eq!(d.reason, reason, "{proxy_decision}");
            assert_eq!(d.rule, RULE);
        }
    }

    #[test]
    fn every_socket_error_denies_with_the_go_reason_class() {
        let cases = [
            (ErrorKind::ProxyUnavailable, "proxy_unavailable"),
            (ErrorKind::ProxyTimeout, "proxy_timeout"),
            (ErrorKind::ProxyError, "proxy_error"),
            (ErrorKind::MalformedResponse, "malformed_response"),
            (ErrorKind::MissingToken, "missing_token"),
            (ErrorKind::Cancelled, "proxy_error"),
            (ErrorKind::InvalidArgument, "proxy_error"),
        ];
        for (kind, class) in cases {
            let d = error_decision(ClientError::from_kind(kind));
            assert_eq!(d.action, Action::Deny);
            assert_eq!(d.reason, format!("kei-proxy {class}"));
        }
    }

    #[test]
    fn denial_hints_are_known_code_only_and_metadata_is_preserved() {
        let d = response_decision(AuthorizeResponse {
            decision: "deny".into(),
            reason: Some("blocked".into()),
            policy_id: Some("policy-1".into()),
            reason_code: Some("preflight_required".into()),
            hint: None,
            enrollment: Some(json!({"claim_url":"https://example.test"})),
            connect: None,
        });
        assert_eq!(d.rule, "policy-1");
        assert_eq!(d.reason_code.as_deref(), Some("preflight_required"));
        assert_eq!(d.hint, hint_for(Some("preflight_required")));
        assert_eq!(
            d.enrollment,
            Some(json!({"claim_url":"https://example.test"}))
        );
        assert_eq!(hint_for(Some("not_a_known_code")), None);
    }

    #[test]
    fn resources_are_derived_deduplicated_and_argument_digest_is_sha256() {
        let args = json!({"owner":"acme", "repo":"widgets", "issue_number": 7});
        assert_eq!(
            resources_touched("github.get_issue", &args),
            ["github:repo:acme/widgets", "github:issue:7"]
        );
        assert_eq!(
            tool_args_digest(&json!({})),
            "44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a"
        );
    }

    #[test]
    fn tool_args_digest_sorts_object_keys_at_every_depth() {
        // preserve_order keeps insertion order, so only canonicalization makes
        // these two digests equal.
        let args = json!({"b": 1, "a": {"d": 2, "c": 3}});
        let reordered = json!({"a": {"c": 3, "d": 2}, "b": 1});
        assert_eq!(tool_args_digest(&args), tool_args_digest(&reordered));
    }

    fn select_caller() -> CallerContext {
        CallerContext {
            user_id: "user-1".into(),
            source: "tests".into(),
            ..CallerContext::default()
        }
    }

    fn select_env(entries: &[(&str, &str)]) -> HashMap<String, String> {
        entries
            .iter()
            .map(|(key, value)| (key.to_string(), value.to_string()))
            .collect()
    }

    fn select_dir(name: &str) -> PathBuf {
        let nanos = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .expect("system clock")
            .as_nanos();
        let dir =
            std::env::temp_dir().join(format!("aw-select-{name}-{}-{nanos}", std::process::id()));
        std::fs::create_dir_all(&dir).expect("create temp dir");
        std::fs::canonicalize(&dir).expect("resolve temp dir")
    }

    #[test]
    fn select_prefers_the_explicit_socket_path() {
        let dir = select_dir("explicit");
        let empty_bin = dir.join("empty-bin");
        std::fs::create_dir_all(&empty_bin).expect("create empty bin");
        let plain = dir.join("plain-file");
        std::fs::write(&plain, "not a socket").expect("write plain file");
        let evaluator = KeiProxyEvaluator::with_client(
            select_authorization_client(
                "tests",
                &SelectOptions {
                    socket_path: plain.display().to_string(),
                    env: Some(select_env(&[
                        ("KEI_RUNTIME_TOKEN", "token"),
                        ("PATH", empty_bin.to_str().expect("utf-8 path")),
                    ])),
                    ..SelectOptions::default()
                },
            )
            .expect("select"),
        );
        // A socket connect failure carries no detail; a CLI resolution
        // failure would say where it looked.
        let decision = evaluator.evaluate("read_file", &json!({"path": "x"}), &select_caller());
        assert_eq!(decision.reason, "kei-proxy proxy_unavailable");
    }

    #[test]
    fn select_reads_the_socket_path_from_the_options_env() {
        let dir = select_dir("env");
        let empty_bin = dir.join("empty-bin");
        std::fs::create_dir_all(&empty_bin).expect("create empty bin");
        let plain = dir.join("plain-file");
        std::fs::write(&plain, "not a socket").expect("write plain file");
        let evaluator = KeiProxyEvaluator::with_client(
            select_authorization_client(
                "tests",
                &SelectOptions {
                    env: Some(select_env(&[
                        (
                            "KEI_RUNTIME_SOCKET_PATH",
                            plain.to_str().expect("utf-8 path"),
                        ),
                        ("KEI_RUNTIME_TOKEN", "token"),
                        ("PATH", empty_bin.to_str().expect("utf-8 path")),
                    ])),
                    ..SelectOptions::default()
                },
            )
            .expect("select"),
        );
        let decision = evaluator.evaluate("read_file", &json!({"path": "x"}), &select_caller());
        assert_eq!(decision.reason, "kei-proxy proxy_unavailable");
    }

    #[test]
    fn select_falls_back_to_the_cli_client_without_a_socket() {
        let dir = select_dir("cli");
        let empty_bin = dir.join("empty-bin");
        std::fs::create_dir_all(&empty_bin).expect("create empty bin");
        let evaluator = KeiProxyEvaluator::with_client(
            select_authorization_client(
                "tests",
                &SelectOptions {
                    env: Some(select_env(&[
                        ("KEI_RUNTIME_TOKEN", "token"),
                        ("PATH", empty_bin.to_str().expect("utf-8 path")),
                    ])),
                    ..SelectOptions::default()
                },
            )
            .expect("select"),
        );
        let decision = evaluator.evaluate("read_file", &json!({"path": "x"}), &select_caller());
        assert_eq!(decision.action, Action::Deny);
        assert!(
            decision
                .reason
                .starts_with("kei-proxy proxy_unavailable: kei-proxy was not found on PATH"),
            "reason: {}",
            decision.reason
        );
    }

    #[test]
    fn select_refuses_a_down_socket_instead_of_falling_back() {
        let dir = select_dir("down");
        let empty_bin = dir.join("empty-bin");
        std::fs::create_dir_all(&empty_bin).expect("create empty bin");
        // A CLI executable that would allow the call if the selector fell
        // back to it.
        let fake = dir.join("kei-proxy");
        std::fs::write(
            &fake,
            format!(
                "#!/bin/sh\necho ran > {}\necho '{{\"decision\":\"allow\",\"policy_id\":\"p\"}}'\n",
                dir.join("ran").display()
            ),
        )
        .expect("write fake");
        let mut permissions = std::fs::metadata(&fake).expect("metadata").permissions();
        permissions.set_mode(0o755);
        std::fs::set_permissions(&fake, permissions).expect("chmod fake");
        let evaluator = KeiProxyEvaluator::with_client(
            select_authorization_client(
                "tests",
                &SelectOptions {
                    socket_path: dir.join("missing.sock").display().to_string(),
                    executable: fake.display().to_string(),
                    env: Some(select_env(&[
                        ("KEI_RUNTIME_TOKEN", "token"),
                        ("PATH", empty_bin.to_str().expect("utf-8 path")),
                    ])),
                    ..SelectOptions::default()
                },
            )
            .expect("select"),
        );
        let decision = evaluator.evaluate("read_file", &json!({"path": "x"}), &select_caller());
        assert_eq!(decision.action, Action::Deny);
        assert_eq!(decision.reason, "kei-proxy proxy_unavailable");
        assert!(!dir.join("ran").exists(), "the CLI fallback must not run");
    }

    #[test]
    fn select_rejects_an_empty_source() {
        let error = select_authorization_client("  ", &SelectOptions::default())
            .err()
            .expect("an empty source is rejected");
        assert_eq!(
            error.to_string(),
            "select authorization client: source must be non-empty"
        );
    }

    #[test]
    fn the_socket_client_needs_an_invoking_subject() {
        let mut client = SocketAuthorizationClient {
            source: "tests".into(),
            socket: Client::new("unused.sock"),
        };
        let req = AuthorizeRequest {
            user_id: String::new(),
            tool: String::new(),
            action: String::new(),
            resource: String::new(),
            span_id: String::new(),
            invoking_subject: String::new(),
            parent_span: String::new(),
            delegation_depth: 0,
            agent_version: String::new(),
            framework: String::new(),
            tool_args_digest: String::new(),
            resources: Vec::new(),
            workspace_id: String::new(),
        };
        let error = client.authorize(&req).unwrap_err();
        assert_eq!(error.class, REASON_PROXY_ERROR);
        assert_eq!(error.detail, "no invoking subject to open a session for");
    }
}
