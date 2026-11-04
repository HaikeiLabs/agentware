//! Fully-wired executor construction (port of `go/executor/dispatcher.go`)
//! plus the minimal `go/prompts` pieces it needs.

use super::{ExecuteRequest, Executor, InferenceExecutor, InferenceExecutorConfig};
use crate::evaluator::{CallerContext, Decision};
use crate::llm::Backend;
use crate::middleware::{Auditor, Middleware, PolicyEvaluator};
use crate::prompts::generate_tool_section;
use crate::tools::{RegistryExecutor, ToolRegistry};
use serde_json::Value;
use std::sync::Arc;

/// Go boxes any concrete value behind its interfaces automatically; Rust
/// needs explicit forwarding impls for `Arc<T>` so boxed shared components
/// satisfy the middleware seams.
impl<T> PolicyEvaluator for Arc<T>
where
    T: PolicyEvaluator + Send + Sync + ?Sized,
{
    fn evaluate(
        &self,
        tool_name: &str,
        args: &Value,
        caller: &CallerContext,
    ) -> Result<Decision, String> {
        (**self).evaluate(tool_name, args, caller)
    }
}

impl<T> Auditor for Arc<T>
where
    T: Auditor + Send + Sync + ?Sized,
{
    fn record(&self, event: crate::middleware::AuditEvent) {
        (**self).record(event)
    }
}

impl<T> crate::middleware::ToolExecutor for Arc<T>
where
    T: crate::middleware::ToolExecutor + Send + Sync + ?Sized,
{
    fn execute(&self, tool_name: &str, args: &Value) -> Result<Value, String> {
        (**self).execute(tool_name, args)
    }
}
/// Creates a fully wired executor with all components connected (Go
/// `NewDispatchExecutor`): registry dispatch wrapped in the crate middleware
/// (policy/auditor optional, as in Go's nil checks) behind the model's tool
/// formatter.
///
/// Go wires `middleware.NewMiddleware(toolExec).WithPolicy(p).WithAuditor(a)`
/// and the middleware itself satisfies `middleware.ToolExecutor`; the Rust
/// port boxes the middleware (with an allow-all policy when `policy` is
/// `None` — Go's nil evaluator allows with reason "no policy configured",
/// and a no-op auditor when `auditor` is `None` — Go's nil auditor skips
/// recording) and adapts it to the dispatch seam in `executor::mod`.
pub fn new_dispatch_executor(
    backend: Arc<dyn Backend>,
    registry: Arc<ToolRegistry>,
    policy: Option<Arc<dyn PolicyEvaluator + Send + Sync>>,
    auditor: Option<Arc<dyn Auditor>>,
    model_name: &str,
) -> Box<dyn Executor> {
    let tool_exec = Arc::new(RegistryExecutor::new(Arc::clone(&registry)));

    struct NoPolicy;
    impl PolicyEvaluator for NoPolicy {
        fn evaluate(
            &self,
            _tool_name: &str,
            _args: &Value,
            _caller: &CallerContext,
        ) -> Result<Decision, String> {
            // Go middleware.go:48-50: nil evaluator allows.
            Ok(Decision {
                action: crate::evaluator::Action::Allow,
                rule: String::new(),
                reason: "no policy configured".into(),
                reason_code: None,
                hint: None,
                enrollment: None,
                connect: None,
                redacted_args: None,
            })
        }
    }
    struct NoAuditor;
    impl Auditor for NoAuditor {
        fn record(&self, _event: crate::middleware::AuditEvent) {}
    }

    let middleware = Middleware::new(
        tool_exec,
        policy.unwrap_or_else(|| Arc::new(NoPolicy)),
        auditor.unwrap_or_else(|| Arc::new(NoAuditor)),
    );

    let formatter = crate::toolformat::for_model(model_name);

    Box::new(InferenceExecutor::new(InferenceExecutorConfig {
        backend,
        registry,
        tool_exec: Arc::new(middleware),
        formatter,
        max_iterations: 0,
        completion_signal: String::new(),
    }))
}

/// Builds a system prompt from the tool registry (Go `BuildSystemPrompt`).
pub fn build_system_prompt(registry: &ToolRegistry, additional_context: &str) -> String {
    let mut sb = String::from("You are an AI assistant with access to tools.\n\n");

    if !additional_context.is_empty() {
        sb.push_str(additional_context);
        sb.push_str("\n\n");
    }

    sb.push_str(&generate_tool_section(registry));

    sb.push_str("\nWhen you need to use a tool, make a tool call.\n");
    sb.push_str("When you have completed the task, output TASK_COMPLETE.\n");

    sb
}

/// Creates an [`ExecuteRequest`] with the system prompt built from tools (Go
/// `BuildRequest`).
pub fn build_request(
    registry: &ToolRegistry,
    user_message: &str,
    caller_ctx: CallerContext,
    job_id: &str,
    additional_context: &str,
) -> ExecuteRequest {
    ExecuteRequest {
        system_prompt: build_system_prompt(registry, additional_context),
        user_message: user_message.to_owned(),
        caller_ctx,
        job_id: job_id.to_owned(),
        ..Default::default()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::llm::Role;
    use crate::middleware::InMemoryAuditor;
    use crate::tools::Tool;
    use serde_json::Map;

    struct EchoTool;
    impl Tool for EchoTool {
        fn name(&self) -> &str {
            "echo"
        }
        fn description(&self) -> &str {
            "Echoes its input"
        }
        fn execute(&self, _args: &Map<String, Value>) -> Result<crate::tools::ToolResult, String> {
            Ok(crate::tools::ToolResult {
                success: true,
                output: "echoed".into(),
                ..Default::default()
            })
        }
    }

    fn registry_with_tool() -> Arc<ToolRegistry> {
        let mut registry = ToolRegistry::new();
        registry.register(Arc::new(EchoTool), None);
        Arc::new(registry)
    }

    #[test]
    fn build_system_prompt_shape() {
        let prompt = build_system_prompt(&registry_with_tool(), "");
        assert_eq!(
            prompt,
            "You are an AI assistant with access to tools.\n\n\
             ## Available Tools\n\n### echo\nEchoes its input\n\n\
             \nWhen you need to use a tool, make a tool call.\n\
             When you have completed the task, output TASK_COMPLETE.\n"
        );
    }

    #[test]
    fn build_system_prompt_with_additional_context() {
        let prompt = build_system_prompt(&registry_with_tool(), "Extra context");
        assert!(
            prompt
                .starts_with("You are an AI assistant with access to tools.\n\nExtra context\n\n")
        );
    }

    #[test]
    fn build_request_wraps_prompt() {
        let request = build_request(
            &registry_with_tool(),
            "do a thing",
            CallerContext {
                user_id: "u1".into(),
                ..Default::default()
            },
            "job-1",
            "",
        );
        assert_eq!(request.user_message, "do a thing");
        assert_eq!(request.job_id, "job-1");
        assert_eq!(request.caller_ctx.user_id, "u1");
        assert!(request.system_prompt.contains("## Available Tools"));
    }

    #[test]
    fn dispatch_executor_runs_one_full_loop() {
        // Two-phase backend: first call returns the tool call, second the
        // completion signal.
        struct TwoPhaseBackend(std::sync::atomic::AtomicUsize);
        impl Backend for TwoPhaseBackend {
            fn complete(&self, _req: &crate::llm::Request) -> Result<crate::llm::Response, String> {
                if self.0.fetch_add(1, std::sync::atomic::Ordering::SeqCst) == 0 {
                    Ok(crate::llm::Response {
                        content: r#"[{"name": "echo", "arguments": {"text": "hi"}}]"#.into(),
                        ..Default::default()
                    })
                } else {
                    Ok(crate::llm::Response {
                        content: "TASK_COMPLETE".into(),
                        ..Default::default()
                    })
                }
            }
            fn supports_native_tool_calling(&self) -> bool {
                true
            }
            fn model_name(&self) -> String {
                "fake".into()
            }
            fn context_window_size(&self) -> i64 {
                0
            }
        }

        let auditor = InMemoryAuditor::new();
        let executor = new_dispatch_executor(
            Arc::new(TwoPhaseBackend(std::sync::atomic::AtomicUsize::new(0))),
            registry_with_tool(),
            None,
            Some(Arc::new(auditor)),
            "fake",
        );

        let request = build_request(
            &registry_with_tool(),
            "say hi",
            CallerContext {
                user_id: "u1".into(),
                session_id: "s1".into(),
                ..Default::default()
            },
            "job-1",
            "",
        );
        let result = executor.execute(&request).expect("execute");
        assert_eq!(
            result.termination_reason,
            super::super::TerminationReason::Complete
        );
        assert_eq!(result.iterations, 2);
        assert_eq!(result.tool_calls_made, 1);
        // The tool result came through the middleware and formatting.
        assert!(
            result
                .conversation
                .iter()
                .any(|message| message.role == Role::Tool && message.content == "echoed")
        );
    }
}
