//! The agentic inference loop and its wiring (port of `go/executor`).
//!
//! The loop sends the conversation to the backend, parses tool calls from
//! the response TEXT with the model formatter (Go attaches native
//! `resp.ToolCalls` to the conversation but executes only text-parsed
//! calls), executes them through the governed executor seam, and repeats
//! until completion, an error, or the iteration cap.

pub mod dispatcher;
pub mod inference;

use crate::evaluator::CallerContext;
use crate::llm::Message;
use crate::tools::ToolResult;
use serde_json::Value;

pub use dispatcher::{build_request, build_system_prompt, new_dispatch_executor};
pub use inference::{InferenceExecutor, build_conversation, is_complete};

/// Runs the agentic inference loop (Go `Executor`).
///
/// No `Send`/`Sync` supertraits: Go's interface carries none either, and the
/// formatter object is single-threaded in this port.
pub trait Executor {
    /// Runs the inference loop for a given task. Like Go, the returned
    /// result carries the termination state even alongside an error.
    fn execute(&self, req: &ExecuteRequest) -> Result<ExecuteResult, String>;
}

/// The input to a single agent run (Go `ExecuteRequest`).
#[derive(Debug, Clone, Default)]
pub struct ExecuteRequest {
    /// The agent's instructions.
    pub system_prompt: String,
    /// The task description.
    pub user_message: String,
    /// Any prior conversation to include.
    pub history: Vec<Message>,
    /// Caps the number of tool-call rounds. 0 means use the executor's
    /// default (20).
    pub max_iterations: i64,
    /// Passed to the middleware policy evaluator on each tool call.
    pub caller_ctx: CallerContext,
    /// Optional identifier for correlating logs and context files.
    pub job_id: String,
    /// Enables extended thinking/reasoning for the model.
    pub thinking: bool,
}

/// The output of a completed (or terminated) agent run (Go `ExecuteResult`).
#[derive(Debug, Clone, Default)]
pub struct ExecuteResult {
    /// The last LLM text response (after all tool calls).
    pub final_response: String,
    /// The number of inference rounds that ran.
    pub iterations: i64,
    /// The total number of individual tool calls executed.
    pub tool_calls_made: i64,
    /// Why the loop stopped.
    pub termination_reason: TerminationReason,
    /// The full message history of the run.
    pub conversation: Vec<Message>,
}

/// Explains why the inference loop stopped (Go `TerminationReason`).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub enum TerminationReason {
    /// Go `TerminationComplete`: the LLM signaled task completion.
    #[default]
    Complete,
    /// Go `TerminationMaxIterations`: the loop hit its iteration cap.
    MaxIterations,
    /// Go `TerminationError`: an unrecoverable error occurred.
    Error,
    /// Go `TerminationCanceled`. Go produces it from `ctx.Done()`; the Rust
    /// port has no cancellation seam, so it is defined for parity but never
    /// produced.
    Canceled,
}

impl TerminationReason {
    /// The Go string spelling.
    pub fn as_str(&self) -> &'static str {
        match self {
            TerminationReason::Complete => "complete",
            TerminationReason::MaxIterations => "max_iterations",
            TerminationReason::Error => "error",
            TerminationReason::Canceled => "canceled",
        }
    }
}

/// Configures an [`InferenceExecutor`] (Go `InferenceExecutorConfig`).
pub struct InferenceExecutorConfig {
    /// The LLM backend to use.
    pub backend: std::sync::Arc<dyn crate::llm::Backend>,
    /// The tool registry.
    pub registry: std::sync::Arc<crate::tools::ToolRegistry>,
    /// The tool executor (typically policy-wrapped registry dispatch; Go
    /// `middleware.ToolExecutor`).
    pub tool_exec: std::sync::Arc<dyn crate::tools::ToolExecutor>,
    /// The tool formatter for parsing model output.
    pub formatter: Box<dyn crate::toolformat::Formatter>,
    /// The maximum number of tool-call rounds.
    pub max_iterations: i64,
    /// The string the LLM must output to signal task done. Defaults to
    /// "TASK_COMPLETE".
    pub completion_signal: String,
}

/// Adapter: the crate middleware (metadata-only port) satisfies the
/// executor's dispatch seam, the way Go's `middlewareImpl` satisfies
/// `middleware.ToolExecutor`. On denial the crate middleware returns a
/// failed result carrying the bare reason; Go's middleware returns
/// `(Result{Success: false, Error: "denied by policy: " + reason}, nil)` —
/// the loop treats both as a failed tool result, but the Go "denied by
/// policy: " prefix is not reproduced here.
impl<E, P, A> crate::tools::ToolExecutor for crate::middleware::Middleware<E, P, A>
where
    E: crate::middleware::ToolExecutor + Send + Sync,
    P: crate::middleware::PolicyEvaluator + Send + Sync,
    A: crate::middleware::Auditor,
{
    fn execute(
        &self,
        tool_name: &str,
        args: &Value,
        caller: &CallerContext,
    ) -> Result<ToolResult, String> {
        let result = crate::middleware::Middleware::execute(self, tool_name, args, caller);
        if !result.success {
            return Err(result.error.unwrap_or_default());
        }
        // The Value seam carries Go's default marshaling of tools.Result
        // (see `tools::executor`); recover the output string from it.
        let output = match result.output {
            Some(Value::Object(map)) => map
                .get("Output")
                .and_then(Value::as_str)
                .unwrap_or_default()
                .to_owned(),
            Some(Value::String(text)) => text,
            Some(other) => other.to_string(),
            None => String::new(),
        };
        Ok(ToolResult {
            success: true,
            output,
            ..Default::default()
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn termination_reason_constants() {
        assert_eq!(TerminationReason::Complete.as_str(), "complete");
        assert_eq!(TerminationReason::MaxIterations.as_str(), "max_iterations");
        assert_eq!(TerminationReason::Error.as_str(), "error");
        assert_eq!(TerminationReason::Canceled.as_str(), "canceled");
    }

    #[test]
    fn request_and_result_shapes() {
        let req = ExecuteRequest {
            system_prompt: "You are a helpful assistant".into(),
            user_message: "Do something useful".into(),
            history: vec![Message {
                role: crate::llm::Role::User,
                content: "hello".into(),
                ..Default::default()
            }],
            max_iterations: 10,
            caller_ctx: CallerContext {
                user_id: "user123".into(),
                ..Default::default()
            },
            job_id: "job_abc".into(),
            thinking: false,
        };

        assert_eq!(req.system_prompt, "You are a helpful assistant");
        assert_eq!(req.user_message, "Do something useful");
        assert_eq!(req.history.len(), 1);
        assert_eq!(req.max_iterations, 10);
        assert_eq!(req.caller_ctx.user_id, "user123");
        assert_eq!(req.job_id, "job_abc");

        let result = ExecuteResult {
            final_response: "Task completed successfully".into(),
            iterations: 5,
            tool_calls_made: 3,
            termination_reason: TerminationReason::Complete,
            conversation: vec![Message {
                role: crate::llm::Role::Assistant,
                content: "Done".into(),
                ..Default::default()
            }],
        };

        assert_eq!(result.final_response, "Task completed successfully");
        assert_eq!(result.iterations, 5);
        assert_eq!(result.tool_calls_made, 3);
        assert_eq!(result.termination_reason, TerminationReason::Complete);
        assert_eq!(result.conversation.len(), 1);
    }
}
