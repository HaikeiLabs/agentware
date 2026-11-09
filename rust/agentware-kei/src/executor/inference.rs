//! The standard inference executor (port of `go/executor/executor.go` and
//! `go/executor/inference.go`).

use super::{ExecuteRequest, ExecuteResult, Executor, InferenceExecutorConfig, TerminationReason};
use crate::llm::{Message, Request, Role, ThinkingConfig, ToolDefinition};
use crate::toolformat::ParsedToolCall;
use crate::tools::ToolResult;
use serde_json::{Map, Value};

/// The standard implementation of [`Executor`] (Go `inferenceExecutor`).
pub struct InferenceExecutor {
    config: InferenceExecutorConfig,
}

impl InferenceExecutor {
    /// Go `NewInferenceExecutor`: defaults 20 iterations and the
    /// "TASK_COMPLETE" completion signal.
    pub fn new(mut config: InferenceExecutorConfig) -> Self {
        if config.max_iterations == 0 {
            config.max_iterations = 20;
        }
        if config.completion_signal.is_empty() {
            config.completion_signal = "TASK_COMPLETE".into();
        }
        Self { config }
    }
}

impl Executor for InferenceExecutor {
    /// Runs the inference loop for a given task (Go `Execute`).
    fn execute(&self, req: &ExecuteRequest) -> Result<ExecuteResult, String> {
        let mut conversation = build_conversation(req);

        let mut max_iterations = self.config.max_iterations;
        if req.max_iterations > 0 {
            max_iterations = req.max_iterations;
        }

        let mut iterations = 0i64;
        let mut tool_calls_made = 0i64;

        while iterations < max_iterations {
            let tools_list = self.config.registry.all();
            let mut llm_tools: Vec<ToolDefinition> = Vec::with_capacity(tools_list.len());
            for tool in &tools_list {
                // Go: non-extended tools get an empty object schema.
                let input_schema = match tool.as_extended() {
                    Some(extended) => extended.input_schema(),
                    None => Some(Map::new()),
                };
                llm_tools.push(ToolDefinition {
                    name: tool.name().to_owned(),
                    description: tool.description().to_owned(),
                    input_schema,
                });
            }

            let mut llm_req = Request {
                messages: conversation.clone(),
                temperature: 0.7,
                max_tokens: 4096,
                tools: llm_tools,
                ..Default::default()
            };
            if req.thinking {
                llm_req.thinking = Some(ThinkingConfig {
                    r#type: "enabled".into(),
                    max_tokens: 8192,
                });
            }

            let response = match self.config.backend.complete(&llm_req) {
                Ok(response) => response,
                // Go returns (result, error) together: the result carries
                // TerminationError and the conversation so far; the Rust Err
                // surfaces the error string (see the PR name map).
                Err(error) => return Err(format!("llm completion failed: {error}")),
            };

            let mut assistant_message = Message {
                role: Role::Assistant,
                content: response.content.clone(),
                ..Default::default()
            };
            if !response.tool_calls.is_empty() {
                assistant_message.tool_calls = response.tool_calls.clone();
            }
            conversation.push(assistant_message);

            if is_complete(&response.content, &self.config.completion_signal) {
                return Ok(ExecuteResult {
                    final_response: response.content,
                    iterations: iterations + 1,
                    tool_calls_made,
                    termination_reason: TerminationReason::Complete,
                    conversation,
                });
            }

            let tool_calls = if !response.tool_calls.is_empty() {
                response
                    .tool_calls
                    .iter()
                    .map(|call| ParsedToolCall {
                        id: call.id.clone(),
                        name: call.name.clone(),
                        args: call.args.clone(),
                        raw: String::new(),
                    })
                    .collect()
            } else {
                match self.config.formatter.parse_tool_calls(&response.content) {
                    Ok(tool_calls) => tool_calls,
                    Err(error) => return Err(format!("failed to parse tool calls: {error}")),
                }
            };

            if tool_calls.is_empty() {
                return Ok(ExecuteResult {
                    final_response: response.content,
                    iterations: iterations + 1,
                    tool_calls_made,
                    termination_reason: TerminationReason::Complete,
                    conversation,
                });
            }

            for tool_call in &tool_calls {
                let args = Value::Object(tool_call.args.clone());
                let result =
                    match self
                        .config
                        .tool_exec
                        .execute(&tool_call.name, &args, &req.caller_ctx)
                    {
                        Ok(result) => result,
                        Err(error) => ToolResult {
                            success: false,
                            error,
                            ..Default::default()
                        },
                    };

                let formatted_result = self.config.formatter.format_tool_result(
                    &tool_call.name,
                    &crate::toolformat::ToolResult {
                        success: result.success,
                        output: result.output.clone(),
                        error: result.error.clone(),
                    },
                );
                conversation.push(Message {
                    role: Role::Tool,
                    content: formatted_result,
                    tool_call_id: tool_call.id.clone(),
                    ..Default::default()
                });
                tool_calls_made += 1;
            }

            iterations += 1;
        }

        Ok(ExecuteResult {
            final_response: conversation
                .last()
                .map(|message| message.content.clone())
                .unwrap_or_default(),
            iterations,
            tool_calls_made,
            termination_reason: TerminationReason::MaxIterations,
            conversation,
        })
    }
}

/// Builds the opening conversation: system prompt, then history, then the
/// user message (Go `buildConversation`).
pub fn build_conversation(req: &ExecuteRequest) -> Vec<Message> {
    let mut conversation = Vec::with_capacity(req.history.len() + 2);

    conversation.push(Message {
        role: Role::System,
        content: req.system_prompt.clone(),
        ..Default::default()
    });

    conversation.extend(req.history.iter().cloned());

    conversation.push(Message {
        role: Role::User,
        content: req.user_message.clone(),
        ..Default::default()
    });

    conversation
}

/// Whether the response signals completion (Go `isComplete`): byte-length
/// aware substring/equality check.
pub fn is_complete(response: &str, signal: &str) -> bool {
    response.len() >= signal.len()
        && (response == signal || (response.len() > signal.len() && response.contains(signal)))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::evaluator::CallerContext;
    use crate::llm::{Backend, Request, Response};
    use crate::tools::ToolExecutor as DispatchExecutor;
    use std::sync::Arc;
    use std::sync::Mutex;

    // ---- fakes ------------------------------------------------------------

    struct FakeBackend {
        responses: Mutex<Vec<Response>>,
    }
    impl Backend for FakeBackend {
        fn complete(&self, _req: &Request) -> Result<Response, String> {
            let mut responses = self.responses.lock().unwrap();
            if responses.is_empty() {
                return Err("no scripted response".into());
            }
            Ok(responses.remove(0))
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

    struct FakeExecutor {
        calls: Mutex<Vec<String>>,
    }
    impl DispatchExecutor for FakeExecutor {
        fn execute(
            &self,
            tool_name: &str,
            _args: &Value,
            _caller: &CallerContext,
        ) -> Result<ToolResult, String> {
            self.calls.lock().unwrap().push(tool_name.to_owned());
            Ok(ToolResult {
                success: true,
                output: format!("{tool_name} ran"),
                ..Default::default()
            })
        }
    }

    struct FailingExecutor;
    impl DispatchExecutor for FailingExecutor {
        fn execute(
            &self,
            _tool_name: &str,
            _args: &Value,
            _caller: &CallerContext,
        ) -> Result<ToolResult, String> {
            Err("tool exploded".into())
        }
    }

    // ---- unit tests (Go executor_test.go) ---------------------------------

    #[test]
    fn build_conversation_system_leads_history() {
        let req = ExecuteRequest {
            system_prompt: "sys".into(),
            user_message: "user".into(),
            history: vec![
                Message {
                    role: Role::User,
                    content: "h1".into(),
                    ..Default::default()
                },
                Message {
                    role: Role::Assistant,
                    content: "h2".into(),
                    ..Default::default()
                },
            ],
            ..Default::default()
        };

        let conversation = build_conversation(&req);

        assert_eq!(conversation.len(), 4);
        assert_eq!(conversation[0].role, Role::System);
        assert_eq!(conversation[0].content, "sys");
        assert_eq!(conversation[1].content, "h1");
        assert_eq!(conversation[2].content, "h2");
        assert_eq!(conversation[3].role, Role::User);
        assert_eq!(conversation[3].content, "user");
    }

    #[test]
    fn new_inference_executor_applies_defaults() {
        let config = InferenceExecutorConfig {
            backend: Arc::new(FakeBackend {
                responses: Mutex::new(Vec::new()),
            }),
            registry: Arc::new(crate::tools::ToolRegistry::new()),
            tool_exec: Arc::new(FakeExecutor {
                calls: Mutex::new(Vec::new()),
            }),
            formatter: Box::new(crate::toolformat::GenericFormatter),
            max_iterations: 0,
            completion_signal: String::new(),
        };
        let executor = InferenceExecutor::new(config);
        assert_eq!(executor.config.max_iterations, 20);
        assert_eq!(executor.config.completion_signal, "TASK_COMPLETE");

        let custom = InferenceExecutorConfig {
            max_iterations: 15,
            completion_signal: "DONE".into(),
            ..test_config()
        };
        let executor = InferenceExecutor::new(custom);
        assert_eq!(executor.config.max_iterations, 15);
        assert_eq!(executor.config.completion_signal, "DONE");
    }

    fn test_config() -> InferenceExecutorConfig {
        InferenceExecutorConfig {
            backend: Arc::new(FakeBackend {
                responses: Mutex::new(Vec::new()),
            }),
            registry: Arc::new(crate::tools::ToolRegistry::new()),
            tool_exec: Arc::new(FakeExecutor {
                calls: Mutex::new(Vec::new()),
            }),
            formatter: Box::new(crate::toolformat::GenericFormatter),
            max_iterations: 0,
            completion_signal: String::new(),
        }
    }

    #[test]
    fn is_complete_matches_go() {
        assert!(is_complete("TASK_COMPLETE", "TASK_COMPLETE"));
        assert!(is_complete("done TASK_COMPLETE now", "TASK_COMPLETE"));
        assert!(!is_complete("TASK", "TASK_COMPLETE"));
        assert!(!is_complete("partial TASK_COMP", "TASK_COMPLETE"));
    }

    // ---- loop tests --------------------------------------------------------

    #[test]
    fn native_tool_call_metadata_stays_in_history() {
        let metadata = serde_json::json!({"google": {"thought_signature": "fixture-signature"}});
        let config = InferenceExecutorConfig {
            backend: Arc::new(FakeBackend {
                responses: Mutex::new(vec![
                    Response {
                        tool_calls: vec![crate::llm::ToolCall {
                            id: "call-1".into(),
                            name: "echo".into(),
                            metadata: Some(metadata.clone()),
                            ..Default::default()
                        }],
                        ..Default::default()
                    },
                    Response {
                        content: "TASK_COMPLETE".into(),
                        ..Default::default()
                    },
                ]),
            }),
            tool_exec: Arc::new(FakeExecutor {
                calls: Mutex::new(Vec::new()),
            }),
            ..test_config()
        };
        let result = InferenceExecutor::new(config)
            .execute(&ExecuteRequest::default())
            .unwrap();
        assert_eq!(result.tool_calls_made, 1);
        let assistant = result
            .conversation
            .iter()
            .find(|m| !m.tool_calls.is_empty())
            .unwrap();
        assert_eq!(assistant.tool_calls[0].metadata.as_ref(), Some(&metadata));
        assert!(
            result
                .conversation
                .iter()
                .any(|m| m.tool_call_id == "call-1")
        );
        assert!(!format!("{:?}", assistant.tool_calls[0]).contains("fixture-signature"));
    }

    #[test]
    fn loop_completes_on_signal() {
        let config = InferenceExecutorConfig {
            backend: Arc::new(FakeBackend {
                responses: Mutex::new(vec![Response {
                    content: "All done TASK_COMPLETE".into(),
                    ..Default::default()
                }]),
            }),
            tool_exec: Arc::new(FakeExecutor {
                calls: Mutex::new(Vec::new()),
            }),
            ..test_config()
        };
        let executor = InferenceExecutor::new(config);

        let result = executor.execute(&ExecuteRequest::default()).unwrap();
        assert_eq!(result.termination_reason, TerminationReason::Complete);
        assert_eq!(result.iterations, 1);
        assert_eq!(result.tool_calls_made, 0);
        assert_eq!(result.final_response, "All done TASK_COMPLETE");
        // Conversation: system + user + assistant.
        assert_eq!(result.conversation.len(), 3);
    }

    #[test]
    fn loop_executes_text_parsed_tool_calls() {
        // The generic formatter parses a JSON array of tool calls.
        let content = r#"[{"name": "echo", "arguments": {"text": "hi"}}]"#;
        let config = InferenceExecutorConfig {
            backend: Arc::new(FakeBackend {
                responses: Mutex::new(vec![
                    Response {
                        content: content.into(),
                        ..Default::default()
                    },
                    Response {
                        content: "TASK_COMPLETE".into(),
                        ..Default::default()
                    },
                ]),
            }),
            registry: Arc::new({
                let mut registry = crate::tools::ToolRegistry::new();
                registry.register(Arc::new(EchoTool), None);
                registry
            }),
            tool_exec: Arc::new(FakeExecutor {
                calls: Mutex::new(Vec::new()),
            }),
            ..test_config()
        };
        let executor = InferenceExecutor::new(config);

        let result = executor.execute(&ExecuteRequest::default()).unwrap();
        assert_eq!(result.termination_reason, TerminationReason::Complete);
        assert_eq!(result.iterations, 2);
        assert_eq!(result.tool_calls_made, 1);
        // The tool result message carries the parsed call id (empty here).
        let tool_message = result
            .conversation
            .iter()
            .find(|message| message.role == Role::Tool)
            .expect("tool message");
        assert_eq!(tool_message.content, "echo ran");
    }

    struct EchoTool;
    impl crate::tools::Tool for EchoTool {
        fn name(&self) -> &str {
            "echo"
        }
        fn description(&self) -> &str {
            "Echoes"
        }
        fn execute(&self, _args: &Map<String, Value>) -> Result<ToolResult, String> {
            Ok(ToolResult {
                success: true,
                output: "echoed".into(),
                ..Default::default()
            })
        }
    }

    #[test]
    fn executor_error_becomes_failed_result() {
        let content = r#"[{"name": "echo", "arguments": {}}]"#;
        let config = InferenceExecutorConfig {
            backend: Arc::new(FakeBackend {
                responses: Mutex::new(vec![
                    Response {
                        content: content.into(),
                        ..Default::default()
                    },
                    Response {
                        content: "TASK_COMPLETE".into(),
                        ..Default::default()
                    },
                ]),
            }),
            tool_exec: Arc::new(FailingExecutor),
            ..test_config()
        };
        let executor = InferenceExecutor::new(config);

        let result = executor.execute(&ExecuteRequest::default()).unwrap();
        assert_eq!(result.termination_reason, TerminationReason::Complete);
        assert_eq!(result.tool_calls_made, 1);
        let tool_message = result
            .conversation
            .iter()
            .find(|message| message.role == Role::Tool)
            .expect("tool message");
        assert_eq!(tool_message.content, "Error: tool exploded");
    }

    #[test]
    fn backend_error_terminates_with_error() {
        let config = InferenceExecutorConfig {
            backend: Arc::new(FakeBackend {
                responses: Mutex::new(Vec::new()),
            }),
            ..test_config()
        };
        let executor = InferenceExecutor::new(config);

        let error = executor.execute(&ExecuteRequest::default()).unwrap_err();
        assert_eq!(error, "llm completion failed: no scripted response");
    }

    #[test]
    fn parse_error_terminates_with_error() {
        let config = InferenceExecutorConfig {
            backend: Arc::new(FakeBackend {
                responses: Mutex::new(vec![Response {
                    // Malformed tool-call arguments fail closed in the
                    // tag-based formatters.
                    content: "<tool_call>{\"name\": \"echo\"}</tool_call>".into(),
                    ..Default::default()
                }]),
            }),
            ..test_config()
        };
        let executor = InferenceExecutor::new(config);

        let error = executor.execute(&ExecuteRequest::default()).unwrap_err();
        assert!(error.starts_with("failed to parse tool calls:"), "{error}");
    }

    #[test]
    fn max_iterations_terminates_the_loop() {
        let content = r#"[{"name": "echo", "arguments": {}}]"#;
        let config = InferenceExecutorConfig {
            backend: Arc::new(FakeBackend {
                responses: Mutex::new(
                    (0..25)
                        .map(|_| Response {
                            content: content.into(),
                            ..Default::default()
                        })
                        .collect(),
                ),
            }),
            tool_exec: Arc::new(FakeExecutor {
                calls: Mutex::new(Vec::new()),
            }),
            max_iterations: 3,
            ..test_config()
        };
        let executor = InferenceExecutor::new(config);

        let result = executor.execute(&ExecuteRequest::default()).unwrap();
        assert_eq!(result.termination_reason, TerminationReason::MaxIterations);
        assert_eq!(result.iterations, 3);
        assert_eq!(result.tool_calls_made, 3);
    }

    #[test]
    fn thinking_request_enables_reasoning_config() {
        use std::sync::atomic::{AtomicUsize, Ordering};
        struct CaptureBackend(AtomicUsize);
        impl Backend for CaptureBackend {
            fn complete(&self, req: &Request) -> Result<Response, String> {
                assert!(req.thinking.is_some());
                assert_eq!(req.thinking.as_ref().unwrap().r#type, "enabled");
                assert_eq!(req.thinking.as_ref().unwrap().max_tokens, 8192);
                self.0.fetch_add(1, Ordering::SeqCst);
                Ok(Response {
                    content: "TASK_COMPLETE".into(),
                    ..Default::default()
                })
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
        let config = InferenceExecutorConfig {
            backend: Arc::new(CaptureBackend(AtomicUsize::new(0))),
            ..test_config()
        };
        let executor = InferenceExecutor::new(config);

        let result = executor
            .execute(&ExecuteRequest {
                thinking: true,
                ..Default::default()
            })
            .unwrap();
        assert_eq!(result.termination_reason, TerminationReason::Complete);
    }

    #[test]
    fn request_carries_registry_tools_with_default_schema() {
        use std::sync::atomic::{AtomicUsize, Ordering};
        struct CaptureBackend(AtomicUsize);
        impl Backend for CaptureBackend {
            fn complete(&self, req: &Request) -> Result<Response, String> {
                assert_eq!(req.temperature, 0.7);
                assert_eq!(req.max_tokens, 4096);
                assert_eq!(req.tools.len(), 1);
                assert_eq!(req.tools[0].name, "echo");
                // Non-extended tools get an empty object schema.
                assert_eq!(req.tools[0].input_schema, Some(Map::new()));
                self.0.fetch_add(1, Ordering::SeqCst);
                Ok(Response {
                    // An empty array parses as zero tool calls → Complete.
                    content: "[]".into(),
                    ..Default::default()
                })
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
        let config = InferenceExecutorConfig {
            backend: Arc::new(CaptureBackend(AtomicUsize::new(0))),
            registry: Arc::new({
                let mut registry = crate::tools::ToolRegistry::new();
                registry.register(Arc::new(EchoTool), None);
                registry
            }),
            ..test_config()
        };
        let executor = InferenceExecutor::new(config);

        // No tool calls parsed from the JSON dummy content → Complete.
        let result = executor.execute(&ExecuteRequest::default()).unwrap();
        assert_eq!(result.termination_reason, TerminationReason::Complete);
    }
}
