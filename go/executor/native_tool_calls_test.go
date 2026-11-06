package executor

import (
	"context"
	"testing"

	"github.com/haikeilabs/agentware/go/llm"
	"github.com/haikeilabs/agentware/go/middleware"
	"github.com/haikeilabs/agentware/go/toolformat"
	"github.com/haikeilabs/agentware/go/tools"
)

// scriptedBackend replays canned responses and records every request.
type scriptedBackend struct {
	responses []*llm.Response
	requests  []*llm.Request
}

func (b *scriptedBackend) Complete(_ context.Context, req *llm.Request) (*llm.Response, error) {
	snapshot := *req
	snapshot.Messages = append([]llm.Message(nil), req.Messages...)
	b.requests = append(b.requests, &snapshot)
	resp := b.responses[0]
	b.responses = b.responses[1:]
	return resp, nil
}

func (b *scriptedBackend) SupportsNativeToolCalling() bool { return true }
func (b *scriptedBackend) ModelName() string               { return "gpt-4o-mini" }
func (b *scriptedBackend) ContextWindowSize() int          { return 0 }

type recordingTool struct {
	calls []map[string]any
}

func (t *recordingTool) Name() string        { return "hn_top_stories" }
func (t *recordingTool) Description() string { return "List top stories" }
func (t *recordingTool) Execute(_ context.Context, args map[string]any) (*tools.Result, error) {
	t.calls = append(t.calls, args)
	return &tools.Result{Success: true, Output: "1. Story one"}, nil
}

func newNativeExecutor(backend llm.Backend, tool tools.Tool) Executor {
	registry := tools.NewToolRegistry()
	registry.Register(tool)
	return NewInferenceExecutor(InferenceExecutorConfig{
		Backend:   backend,
		Registry:  registry,
		ToolExec:  middleware.NewMiddleware(tools.NewRegistryExecutor(registry)),
		Formatter: toolformat.GetFormatter(backend.ModelName()),
	})
}

// TestInferenceExecutor_ExecutesNativeToolCalls runs a tool call the backend
// returned on the native channel (empty content), keeps the assistant
// tool_calls message, and answers it with a tool message carrying the same
// tool_call_id.
func TestInferenceExecutor_ExecutesNativeToolCalls(t *testing.T) {
	backend := &scriptedBackend{responses: []*llm.Response{
		{ToolCalls: []llm.ToolCall{{ID: "call_1", Name: "hn_top_stories", Args: map[string]any{"limit": float64(5)}}}},
		{Content: "Story one leads today. TASK_COMPLETE"},
	}}
	tool := &recordingTool{}

	result, err := newNativeExecutor(backend, tool).Execute(context.Background(), ExecuteRequest{SystemPrompt: "sys", UserMessage: "summarize"})
	if err != nil {
		t.Fatalf("Execute: %v", err)
	}
	if result.TerminationReason != TerminationComplete {
		t.Errorf("expected complete, got %s", result.TerminationReason)
	}
	if result.ToolCallsMade != 1 || len(tool.calls) != 1 {
		t.Fatalf("expected the native tool call to run once, made=%d executed=%d", result.ToolCallsMade, len(tool.calls))
	}
	if tool.calls[0]["limit"] != float64(5) {
		t.Errorf("expected args limit=5, got %v", tool.calls[0])
	}

	if len(backend.requests) != 2 {
		t.Fatalf("expected 2 completions, got %d", len(backend.requests))
	}
	second := backend.requests[1].Messages
	if len(second) != 4 {
		t.Fatalf("expected system, user, assistant, tool messages on the second turn, got %d", len(second))
	}
	assistant, toolMsg := second[2], second[3]
	if assistant.Role != llm.RoleAssistant || len(assistant.ToolCalls) != 1 ||
		assistant.ToolCalls[0].ID != "call_1" || assistant.ToolCalls[0].Name != "hn_top_stories" {
		t.Errorf("expected assistant message with tool call call_1/hn_top_stories, got %+v", assistant)
	}
	if toolMsg.Role != llm.RoleTool || toolMsg.ToolCallID != "call_1" || toolMsg.Content != "1. Story one" {
		t.Errorf("expected tool message answering call_1, got %+v", toolMsg)
	}
}

// TestInferenceExecutor_NativePlainTextAnswerCompletes treats a prose answer
// from a native tool-calling backend as the final answer, not as a malformed
// content-encoded tool call.
func TestInferenceExecutor_NativePlainTextAnswerCompletes(t *testing.T) {
	backend := &scriptedBackend{responses: []*llm.Response{
		{Content: "Here are today's top stories."},
	}}

	result, err := newNativeExecutor(backend, &recordingTool{}).Execute(context.Background(), ExecuteRequest{SystemPrompt: "sys", UserMessage: "summarize"})
	if err != nil {
		t.Fatalf("Execute: %v", err)
	}
	if result.TerminationReason != TerminationComplete || result.FinalResponse != "Here are today's top stories." {
		t.Errorf("expected complete with the prose answer, got %s %q", result.TerminationReason, result.FinalResponse)
	}
}

// TestInferenceExecutor_ContentToolCallsStillParsed keeps the formatter path
// for models that encode tool calls in content.
func TestInferenceExecutor_ContentToolCallsStillParsed(t *testing.T) {
	backend := &scriptedBackend{responses: []*llm.Response{
		{Content: `[{"id":"c1","name":"hn_top_stories","arguments":{"limit":3}}]`},
		{Content: "done TASK_COMPLETE"},
	}}
	tool := &recordingTool{}

	result, err := newNativeExecutor(backend, tool).Execute(context.Background(), ExecuteRequest{SystemPrompt: "sys", UserMessage: "summarize"})
	if err != nil {
		t.Fatalf("Execute: %v", err)
	}
	if len(tool.calls) != 1 || tool.calls[0]["limit"] != float64(3) {
		t.Fatalf("expected the content-encoded call to run, got %v", tool.calls)
	}
	if got := backend.requests[1].Messages[3]; got.ToolCallID != "c1" {
		t.Errorf("expected tool message for c1, got %+v", got)
	}
	if result.ToolCallsMade != 1 {
		t.Errorf("expected 1 tool call, got %d", result.ToolCallsMade)
	}
}
