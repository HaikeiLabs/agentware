package executor

import (
	"strings"

	"github.com/haikeilabs/agentware/go/llm"
	"github.com/haikeilabs/agentware/go/middleware"
	"github.com/haikeilabs/agentware/go/prompts"
	"github.com/haikeilabs/agentware/go/toolformat"
	"github.com/haikeilabs/agentware/go/tools"
)

// NewDispatchExecutor creates a fully wired executor with all components connected.
func NewDispatchExecutor(
	backend llm.Backend,
	registry *tools.ToolRegistry,
	policy middleware.PolicyEvaluator,
	auditor middleware.Auditor,
	modelName string,
) Executor {
	toolExec := tools.NewRegistryExecutor(registry)

	mw := middleware.NewMiddleware(toolExec)
	if policy != nil {
		mw = mw.WithPolicy(policy)
	}
	if auditor != nil {
		mw = mw.WithAuditor(auditor)
	}

	formatter := toolformat.GetFormatter(modelName)

	cfg := InferenceExecutorConfig{
		Backend:   backend,
		Registry:  registry,
		ToolExec:  mw,
		Formatter: formatter,
	}

	return NewInferenceExecutor(cfg)
}

// BuildSystemPrompt builds a system prompt from the tool registry.
func BuildSystemPrompt(registry *tools.ToolRegistry, additionalContext string) string {
	var sb strings.Builder

	sb.WriteString("You are an AI assistant with access to tools.\n\n")

	if additionalContext != "" {
		sb.WriteString(additionalContext)
		sb.WriteString("\n\n")
	}

	sb.WriteString(prompts.GenerateToolSection(registry))

	sb.WriteString("\nWhen you need to use a tool, make a tool call.\n")
	sb.WriteString("When you have completed the task, output TASK_COMPLETE.\n")

	return sb.String()
}

// BuildRequest creates an ExecuteRequest with the system prompt built from tools.
func BuildRequest(
	registry *tools.ToolRegistry,
	userMessage string,
	callerCtx middleware.CallerContext,
	jobID string,
	additionalContext string,
) ExecuteRequest {
	return ExecuteRequest{
		SystemPrompt: BuildSystemPrompt(registry, additionalContext),
		UserMessage:  userMessage,
		CallerCtx:    callerCtx,
		JobID:        jobID,
	}
}
