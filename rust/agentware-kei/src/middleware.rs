//! Governed tool execution with metadata-only auditing.
//!
//! Policy evaluation happens before execution. Only an explicit `Allow` or
//! `Filter` decision executes a tool; denials and evaluator errors fail closed.
//! Audit events contain caller and policy metadata plus a digest of the
//! effective arguments, never arguments or tool output.

use crate::evaluator::{Action, CallerContext, Decision};
use serde_json::Value;
use sha2::{Digest, Sha256};
use std::sync::Mutex;
use std::time::{SystemTime, UNIX_EPOCH};

pub const REDACTED_PLACEHOLDER: &str = "[REDACTED]";

/// Evaluates one tool invocation. Errors are treated as a denial by middleware.
pub trait PolicyEvaluator {
    fn evaluate(
        &self,
        tool_name: &str,
        args: &Value,
        caller: &CallerContext,
    ) -> Result<Decision, String>;
}

/// Adapts the built-in Kei proxy evaluator to the middleware policy seam.
impl PolicyEvaluator for crate::evaluator::KeiProxyEvaluator {
    fn evaluate(
        &self,
        tool_name: &str,
        args: &Value,
        caller: &CallerContext,
    ) -> Result<Decision, String> {
        Ok(crate::evaluator::KeiProxyEvaluator::evaluate(
            self, tool_name, args, caller,
        ))
    }
}

/// Executes a tool with the arguments selected by policy.
pub trait ToolExecutor {
    fn execute(&self, tool_name: &str, args: &Value) -> Result<Value, String>;
}

/// Result of a tool call. Tool output is returned to the caller, never stored in audit.
#[derive(Debug, Clone, PartialEq)]
pub struct ToolResult {
    pub success: bool,
    pub output: Option<Value>,
    pub error: Option<String>,
}

/// Metadata-only audit event. This type intentionally has no arguments or output.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct AuditEvent {
    pub invoked_at_unix_ms: u128,
    pub tool_name: String,
    pub decision: Action,
    pub session_id: String,
    pub user_id: String,
    pub invoking_subject: String,
    pub parent_span: String,
    pub delegation_depth: i32,
    pub policy_id: String,
    pub bundle_id: String,
    pub argument_digest: String,
    pub success: bool,
    pub error_class: Option<String>,
}

/// Audit sink for metadata-only events.
pub trait Auditor: Send + Sync {
    fn record(&self, event: AuditEvent);
}

/// Thread-safe in-memory audit sink.
#[derive(Default)]
pub struct InMemoryAuditor {
    events: Mutex<Vec<AuditEvent>>,
}

impl InMemoryAuditor {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn events(&self) -> Vec<AuditEvent> {
        self.events.lock().expect("audit mutex poisoned").clone()
    }
}

impl Auditor for InMemoryAuditor {
    fn record(&self, event: AuditEvent) {
        self.events
            .lock()
            .expect("audit mutex poisoned")
            .push(event);
    }
}

/// Tool executor wrapped by policy evaluation and auditing.
pub struct Middleware<E, P, A> {
    executor: E,
    evaluator: P,
    auditor: A,
}

/// Small dependency-light client alias for harnesses that wrap one executor,
/// evaluator, and auditor together.
pub type AuditedToolClient<E, P, A> = Middleware<E, P, A>;

impl<E, P, A> Middleware<E, P, A>
where
    E: ToolExecutor,
    P: PolicyEvaluator,
    A: Auditor,
{
    pub fn new(executor: E, evaluator: P, auditor: A) -> Self {
        Self {
            executor,
            evaluator,
            auditor,
        }
    }

    pub fn execute(&self, tool_name: &str, args: &Value, caller: &CallerContext) -> ToolResult {
        let evaluation = self.evaluator.evaluate(tool_name, args, caller);
        let (decision, evaluator_error) = match evaluation {
            Ok(decision) => (decision, None),
            Err(error) => (
                Decision {
                    action: Action::Deny,
                    rule: "evaluator-error".into(),
                    reason: "policy evaluator failed".into(),
                    reason_code: Some("evaluator_error".into()),
                    hint: None,
                    enrollment: None,
                    connect: None,
                    redacted_args: None,
                },
                Some(error),
            ),
        };

        let effective_args = if decision.action == Action::Filter {
            apply_filter(args, &decision)
        } else {
            args.clone()
        };
        let digest = argument_digest(&effective_args);
        let bundle_id = caller
            .metadata
            .get("bundle_id")
            .cloned()
            .unwrap_or_default();

        if decision.action == Action::Deny {
            let error_class = if evaluator_error.is_some() {
                "evaluator_error"
            } else {
                "policy_denied"
            };
            self.auditor.record(event(
                tool_name,
                caller,
                &decision,
                digest,
                false,
                Some(error_class),
                bundle_id,
            ));
            return ToolResult {
                success: false,
                output: None,
                error: Some(evaluator_error.unwrap_or_else(|| decision.reason.clone())),
            };
        }

        let (result, success, error_class) = match self.executor.execute(tool_name, &effective_args)
        {
            Ok(output) => (
                ToolResult {
                    success: true,
                    output: Some(output),
                    error: None,
                },
                true,
                None,
            ),
            Err(error) => (
                ToolResult {
                    success: false,
                    output: None,
                    error: Some(error),
                },
                false,
                Some("tool_error"),
            ),
        };
        self.auditor.record(event(
            tool_name,
            caller,
            &decision,
            digest,
            success,
            error_class,
            bundle_id,
        ));
        result
    }
}

fn event(
    tool_name: &str,
    caller: &CallerContext,
    decision: &Decision,
    argument_digest: String,
    success: bool,
    error_class: Option<&str>,
    bundle_id: String,
) -> AuditEvent {
    AuditEvent {
        invoked_at_unix_ms: SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap_or_default()
            .as_millis(),
        tool_name: tool_name.into(),
        decision: decision.action,
        session_id: caller.session_id.clone(),
        user_id: caller.user_id.clone(),
        invoking_subject: caller.invoking_subject.clone(),
        parent_span: caller.parent_span.clone(),
        delegation_depth: caller.delegation_depth,
        policy_id: decision.rule.clone(),
        bundle_id,
        argument_digest,
        success,
        error_class: error_class.map(str::to_owned),
    }
}

fn apply_filter(args: &Value, decision: &Decision) -> Value {
    let Some(original) = args.as_object() else {
        return args.clone();
    };
    let Some(redactions) = decision.redacted_args.as_ref().and_then(Value::as_object) else {
        return args.clone();
    };
    let mut filtered = original.clone();
    for (key, value) in redactions {
        filtered.insert(key.clone(), value.clone());
    }
    Value::Object(filtered)
}

/// SHA-256 of the JSON representation of the effective arguments.
pub fn argument_digest(args: &Value) -> String {
    let canonical = if args.is_object() {
        args.to_string()
    } else {
        "{}".into()
    };
    format!("{:x}", Sha256::digest(canonical.as_bytes()))
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;
    use std::sync::atomic::{AtomicBool, Ordering};

    struct FakePolicy(Result<Decision, String>);
    impl PolicyEvaluator for FakePolicy {
        fn evaluate(&self, _: &str, _: &Value, _: &CallerContext) -> Result<Decision, String> {
            self.0.clone()
        }
    }
    struct FakeExecutor(AtomicBool);
    impl ToolExecutor for FakeExecutor {
        fn execute(&self, _: &str, _: &Value) -> Result<Value, String> {
            self.0.store(true, Ordering::SeqCst);
            Ok(json!({"ok": true}))
        }
    }
    fn decision(action: Action) -> Decision {
        Decision {
            action,
            rule: "policy-7".into(),
            reason: "blocked".into(),
            reason_code: None,
            hint: None,
            enrollment: None,
            connect: None,
            redacted_args: None,
        }
    }

    #[test]
    fn deny_and_evaluator_error_never_run_tool() {
        for policy in [
            FakePolicy(Ok(decision(Action::Deny))),
            FakePolicy(Err("unreachable".into())),
        ] {
            let executor = FakeExecutor(AtomicBool::new(false));
            let auditor = InMemoryAuditor::new();
            let middleware = Middleware::new(executor, policy, auditor);
            let result = middleware.execute(
                "write",
                &json!({"secret":"never-log"}),
                &CallerContext::default(),
            );
            assert!(!result.success);
            assert!(!middleware.executor.0.load(Ordering::SeqCst));
            assert_eq!(middleware.auditor.events().len(), 1);
        }
    }

    #[test]
    fn filter_replaces_arguments_before_execution_and_digesting() {
        let executor = FakeExecutor(AtomicBool::new(false));
        let mut filtered = decision(Action::Filter);
        filtered.redacted_args = Some(json!({"token":REDACTED_PLACEHOLDER}));
        let auditor = InMemoryAuditor::new();
        let middleware = Middleware::new(executor, FakePolicy(Ok(filtered)), auditor);
        let raw = json!({"token":"sensitive","keep":"yes"});
        let _ = middleware.execute("read", &raw, &CallerContext::default());
        assert_eq!(raw["token"], "sensitive");
        let expected = json!({"token":REDACTED_PLACEHOLDER,"keep":"yes"});
        assert_eq!(
            argument_digest(&expected),
            middleware.auditor.events()[0].argument_digest
        );
    }

    #[test]
    fn audit_contains_metadata_and_digest_without_raw_arguments() {
        let executor = FakeExecutor(AtomicBool::new(false));
        let auditor = InMemoryAuditor::new();
        let middleware =
            Middleware::new(executor, FakePolicy(Ok(decision(Action::Allow))), auditor);
        let mut caller = CallerContext {
            user_id: "user-1".into(),
            session_id: "session-1".into(),
            invoking_subject: "person@example.test".into(),
            ..Default::default()
        };
        caller
            .metadata
            .insert("bundle_id".into(), "bundle-3".into());
        let secret = "top-secret-argument";
        let _ = middleware.execute("read", &json!({"value":secret}), &caller);
        let audit = middleware.auditor.events();
        let serialized = format!("{:?}", audit[0]);
        assert!(!serialized.contains(secret));
        assert_eq!(audit[0].session_id, "session-1");
        assert_eq!(audit[0].invoking_subject, "person@example.test");
        assert_eq!(audit[0].bundle_id, "bundle-3");
        assert!(!audit[0].argument_digest.is_empty());
    }
}
