package executor

import (
	"testing"

	"github.com/haikeilabs/agentware/go/llm"
	"github.com/haikeilabs/agentware/go/middleware"
)

func TestTerminationReasonConstants(t *testing.T) {
	if TerminationComplete != "complete" {
		t.Errorf("expected 'complete', got '%s'", TerminationComplete)
	}
	if TerminationMaxIterations != "max_iterations" {
		t.Errorf("expected 'max_iterations', got '%s'", TerminationMaxIterations)
	}
	if TerminationError != "error" {
		t.Errorf("expected 'error', got '%s'", TerminationError)
	}
	if TerminationCanceled != "canceled" {
		t.Errorf("expected 'canceled', got '%s'", TerminationCanceled)
	}
}

func TestExecuteRequest(t *testing.T) {
	req := ExecuteRequest{
		SystemPrompt:  "You are a helpful assistant",
		UserMessage:   "Do something useful",
		History:       []llm.Message{{Role: llm.RoleUser, Content: "hello"}},
		MaxIterations: 10,
		CallerCtx:     middleware.CallerContext{UserID: "user123"},
		JobID:         "job_abc",
	}

	if req.SystemPrompt != "You are a helpful assistant" {
		t.Errorf("expected system prompt, got '%s'", req.SystemPrompt)
	}
	if req.UserMessage != "Do something useful" {
		t.Errorf("expected user message, got '%s'", req.UserMessage)
	}
	if len(req.History) != 1 {
		t.Errorf("expected 1 history message, got %d", len(req.History))
	}
	if req.MaxIterations != 10 {
		t.Errorf("expected 10, got %d", req.MaxIterations)
	}
	if req.CallerCtx.UserID != "user123" {
		t.Errorf("expected UserID 'user123', got '%s'", req.CallerCtx.UserID)
	}
	if req.JobID != "job_abc" {
		t.Errorf("expected JobID 'job_abc', got '%s'", req.JobID)
	}
}

func TestExecuteResult(t *testing.T) {
	result := ExecuteResult{
		FinalResponse:     "Task completed successfully",
		Iterations:        5,
		ToolCallsMade:     3,
		TerminationReason: TerminationComplete,
		Conversation:      []llm.Message{{Role: llm.RoleAssistant, Content: "Done"}},
	}

	if result.FinalResponse != "Task completed successfully" {
		t.Errorf("expected final response, got '%s'", result.FinalResponse)
	}
	if result.Iterations != 5 {
		t.Errorf("expected 5, got %d", result.Iterations)
	}
	if result.ToolCallsMade != 3 {
		t.Errorf("expected 3, got %d", result.ToolCallsMade)
	}
	if result.TerminationReason != TerminationComplete {
		t.Errorf("expected TerminationComplete, got '%s'", result.TerminationReason)
	}
	if len(result.Conversation) != 1 {
		t.Errorf("expected 1 conversation message, got %d", len(result.Conversation))
	}
}

func TestBuildConversation_SystemLeadsHistory(t *testing.T) {
	req := ExecuteRequest{
		SystemPrompt: "sys",
		UserMessage:  "user",
		History: []llm.Message{
			{Role: llm.RoleUser, Content: "h1"},
			{Role: llm.RoleAssistant, Content: "h2"},
		},
	}

	conversation := buildConversation(req)

	if len(conversation) != 4 {
		t.Fatalf("expected 4 messages, got %d", len(conversation))
	}
	if conversation[0].Role != llm.RoleSystem || conversation[0].Content != "sys" {
		t.Errorf("expected leading system message, got %s/%q", conversation[0].Role, conversation[0].Content)
	}
	if conversation[1].Content != "h1" || conversation[2].Content != "h2" {
		t.Errorf("expected history after the system message, got %q, %q", conversation[1].Content, conversation[2].Content)
	}
	if conversation[3].Role != llm.RoleUser || conversation[3].Content != "user" {
		t.Errorf("expected trailing user message, got %s/%q", conversation[3].Role, conversation[3].Content)
	}
}

func TestInferenceExecutorConfig(t *testing.T) {
	cfg := InferenceExecutorConfig{
		MaxIterations:    15,
		CompletionSignal: "DONE",
	}

	if cfg.MaxIterations != 15 {
		t.Errorf("expected 15, got %d", cfg.MaxIterations)
	}
	if cfg.CompletionSignal != "DONE" {
		t.Errorf("expected 'DONE', got '%s'", cfg.CompletionSignal)
	}
}

func TestNewInferenceExecutor_Defaults(t *testing.T) {
	cfg := InferenceExecutorConfig{
		MaxIterations: 0,
	}

	exec := NewInferenceExecutor(cfg)
	if exec == nil {
		t.Fatal("expected non-nil executor")
	}
}

func TestNewInferenceExecutor_SetsDefaults(t *testing.T) {
	cfg := InferenceExecutorConfig{}
	exec := NewInferenceExecutor(cfg)

	if exec == nil {
		t.Fatal("expected non-nil executor")
	}
}
