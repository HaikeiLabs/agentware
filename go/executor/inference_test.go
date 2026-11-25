package executor

import (
	"context"
	"testing"

	"github.com/haikeilabs/agentware/go/llm"
	"github.com/haikeilabs/agentware/go/middleware"
	"github.com/haikeilabs/agentware/go/toolformat"
	"github.com/haikeilabs/agentware/go/tools"
)

// TestInferenceExecutor_PassesCallIDToFormatter runs an Anthropic tool_use
// block returned as content and checks that the tool result the executor
// sends back is a tool_result block answering the same tool_use id.
func TestInferenceExecutor_PassesCallIDToFormatter(t *testing.T) {
	backend := &scriptedBackend{responses: []*llm.Response{
		{Content: `{"type":"tool_use","id":"toolu_01A","name":"hn_top_stories","input":{"limit":5}}`},
		{Content: "Story one leads today. TASK_COMPLETE"},
	}}
	tool := &recordingTool{}
	registry := tools.NewToolRegistry()
	registry.Register(tool)
	exec := NewInferenceExecutor(InferenceExecutorConfig{
		Backend:   backend,
		Registry:  registry,
		ToolExec:  middleware.NewMiddleware(tools.NewRegistryExecutor(registry)),
		Formatter: &toolformat.AnthropicFormatter{},
	})

	if _, err := exec.Execute(context.Background(), ExecuteRequest{SystemPrompt: "sys", UserMessage: "summarize"}); err != nil {
		t.Fatalf("Execute: %v", err)
	}
	if len(tool.calls) != 1 {
		t.Fatalf("expected the tool_use call to run once, ran %d", len(tool.calls))
	}
	if len(backend.requests) != 2 {
		t.Fatalf("expected 2 completions, got %d", len(backend.requests))
	}
	second := backend.requests[1].Messages
	toolMsg := second[len(second)-1]
	want := `{"type":"tool_result","tool_use_id":"toolu_01A","content":"1. Story one"}`
	if toolMsg.Role != llm.RoleTool || toolMsg.ToolCallID != "toolu_01A" || toolMsg.Content != want {
		t.Errorf("expected tool_result answering toolu_01A, got %+v", toolMsg)
	}
}
