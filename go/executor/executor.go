package executor

import (
	"context"

	"github.com/haikeilabs/agentware/go/llm"
	"github.com/haikeilabs/agentware/go/middleware"
	"github.com/haikeilabs/agentware/go/toolformat"
	"github.com/haikeilabs/agentware/go/tools"
)

// Executor runs the agentic inference loop:
// 1. Send prompt to LLM
// 2. Parse tool calls from response
// 3. Execute tools (via middleware)
// 4. Append results to conversation
// 5. Repeat until completion or max iterations.
type Executor interface {
	// Execute runs the inference loop for a given task.
	Execute(ctx context.Context, req ExecuteRequest) (*ExecuteResult, error)
}

// ExecuteRequest is the input to a single agent run.
type ExecuteRequest struct {
	// SystemPrompt is the agent's instructions.
	SystemPrompt string
	// UserMessage is the task description.
	UserMessage string
	// History is any prior conversation to include.
	History []llm.Message
	// MaxIterations caps the number of tool-call rounds.
	// 0 means use the executor's default (typically 20).
	MaxIterations int
	// CallerCtx is passed to the middleware policy evaluator on each tool call.
	CallerCtx middleware.CallerContext
	// JobID is an optional identifier for correlating logs and context files.
	JobID string
	// Thinking enables extended thinking/reasoning for the model.
	Thinking bool
}

// ExecuteResult is the output of a completed (or terminated) agent run.
type ExecuteResult struct {
	// FinalResponse is the last LLM text response (after all tool calls).
	FinalResponse string
	// Iterations is the number of inference rounds that ran.
	Iterations int
	// ToolCallsMade is the total number of individual tool calls executed.
	ToolCallsMade int
	// TerminationReason explains why the loop stopped.
	TerminationReason TerminationReason
	// Conversation is the full message history of the run.
	Conversation []llm.Message
}

// TerminationReason explains why the inference loop stopped.
type TerminationReason string

const (
	// TerminationComplete means the LLM signaled task completion.
	TerminationComplete TerminationReason = "complete"
	// TerminationMaxIterations means the loop hit its iteration cap.
	TerminationMaxIterations TerminationReason = "max_iterations"
	// TerminationError means an unrecoverable error occurred.
	TerminationError TerminationReason = "error"
	// TerminationCanceled means the context was canceled.
	TerminationCanceled TerminationReason = "canceled"
)

// InferenceExecutorConfig configures an InferenceExecutor.
type InferenceExecutorConfig struct {
	// Backend is the LLM backend to use.
	Backend llm.Backend
	// Registry is the tool registry.
	Registry *tools.ToolRegistry
	// ToolExec is the tool executor (typically a Middleware wrapping a registry dispatch).
	ToolExec middleware.ToolExecutor
	// Formatter is the tool formatter for parsing model output.
	Formatter toolformat.ToolFormatter
	// MaxIterations is the maximum number of tool-call rounds.
	MaxIterations int
	// CompletionSignal is the string the LLM must output to signal task done.
	// Defaults to "TASK_COMPLETE".
	CompletionSignal string
}

// NewInferenceExecutor constructs the standard implementation.
func NewInferenceExecutor(cfg InferenceExecutorConfig) Executor {
	if cfg.MaxIterations == 0 {
		cfg.MaxIterations = 20
	}
	if cfg.CompletionSignal == "" {
		cfg.CompletionSignal = "TASK_COMPLETE"
	}
	return &inferenceExecutor{
		config: cfg,
	}
}

// inferenceExecutor is the standard implementation of Executor.
type inferenceExecutor struct {
	config InferenceExecutorConfig
}
