//! Fail-closed policy evaluation through the Kei runtime socket.

use crate::runtimesocket::{Client, ErrorKind, Identity};
use serde_json::{Map, Value, json};
use sha2::{Digest, Sha256};
use std::{cell::RefCell, collections::HashMap};

pub const RULE: &str = "kei-proxy";

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

/// Stateful evaluator. Runtime identity is derived from caller context and the
/// session is opened over the socket for each previously unseen identity.
pub struct KeiProxyEvaluator {
    client: RefCell<Client>,
    default_action: String,
}

impl KeiProxyEvaluator {
    pub fn new(client: Client) -> Self {
        Self {
            client: RefCell::new(client),
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
        let source = if framework.is_empty() {
            "agentware"
        } else {
            framework
        };
        let identity = Identity {
            source: source.into(),
            external_id: user.clone(),
        };
        let result = (|| {
            let mut client = self.client.borrow_mut();
            let session = client.open_session(identity)?;
            let fields = request_fields(&req);
            client.authorize(&session, &fields)
        })();
        match result {
            Ok(value) => match parse_response(&value) {
                Some(response) => response_decision(response),
                None => deny("malformed_response", "authorize response is malformed"),
            },
            Err(error) => error_decision(error.kind),
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

fn parse_response(v: &Value) -> Option<AuthorizeResponse> {
    if v.get("schema").and_then(Value::as_str)
        != Some(crate::runtimesocket::AUTHORIZE_RESPONSE_SCHEMA)
    {
        return None;
    }
    Some(AuthorizeResponse {
        decision: v.get("decision")?.as_str()?.trim().to_ascii_lowercase(),
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
    })
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
    let (class, detail) = match r.decision.as_str() {
        "deny" => ("deny", r.reason.as_deref().unwrap_or("")),
        "enrollment_required" => (
            "enrollment_required",
            r.reason
                .as_deref()
                .filter(|s| !s.is_empty())
                .unwrap_or("enrollment is required before this call"),
        ),
        "" => ("no_decision", ""),
        other => {
            return Decision {
                action: Action::Deny,
                rule,
                reason: format!("kei-proxy unknown_decision: '{other}'"),
                reason_code: r.reason_code,
                hint: r.hint,
                enrollment: r.enrollment,
                connect: r.connect,
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
        enrollment: r.enrollment,
        connect: r.connect,
        redacted_args: None,
    }
}

fn error_decision(kind: ErrorKind) -> Decision {
    let class = match kind {
        ErrorKind::ProxyUnavailable => "proxy_unavailable",
        ErrorKind::ProxyTimeout => "proxy_timeout",
        ErrorKind::MissingToken => "missing_token",
        ErrorKind::MalformedResponse => "malformed_response",
        ErrorKind::ProxyError | ErrorKind::Cancelled | ErrorKind::InvalidArgument => "proxy_error",
    };
    deny(class, "")
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
    let canonical = args
        .as_object()
        .map(|_| args.to_string())
        .unwrap_or_else(|| "{}".into());
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

#[cfg(test)]
mod tests {
    use super::*;

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
            let d = error_decision(kind);
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
}
