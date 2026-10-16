package inference

import (
	"context"
	"errors"
	"testing"

	"github.com/haikeilabs/agentware/go/llm"
	"github.com/haikeilabs/agentware/go/middleware/guardrails"
)

type mockBackend struct {
	resp        *llm.Response
	respOnRetry *llm.Response
	callCount   int
	err         error
}

func (m *mockBackend) Complete(ctx context.Context, req *llm.Request) (*llm.Response, error) {
	m.callCount++
	if m.err != nil {
		return nil, m.err
	}
	if m.respOnRetry != nil && m.callCount > 1 {
		return m.respOnRetry, nil
	}
	return m.resp, nil
}

func (m *mockBackend) SupportsNativeToolCalling() bool {
	return true
}

func (m *mockBackend) ModelName() string {
	return "test-model"
}

func (m *mockBackend) ContextWindowSize() int {
	return 8192
}

func TestRunInference_Success(t *testing.T) {
	backend := &mockBackend{
		resp: &llm.Response{
			Content:      "",
			ToolCalls:    []llm.ToolCall{{ID: "1", Name: "test_tool", Args: map[string]any{"key": "value"}}},
			FinishReason: "tool_calls",
			UsageTokens:  llm.TokenUsage{PromptTokens: 100, CompletionTokens: 50, TotalTokens: 150},
		},
	}

	validator := guardrails.NewResponseValidator([]string{"test_tool"}, false)
	tracker := guardrails.NewErrorTracker()

	cfg := InferenceConfig{
		Client:       backend,
		Validator:    validator,
		ErrorTracker: tracker,
		ToolSpecs:    []llm.ToolDefinition{{Name: "test_tool", Description: "A test tool", InputSchema: map[string]any{}}},
		MaxAttempts:  3,
		StepIndex:    0,
	}

	messages := []llm.Message{
		{Role: llm.RoleUser, Content: "Hello", Meta: llm.MessageMeta{Type: llm.MessageTypeUserInput}},
	}

	result, err := RunInference(context.Background(), messages, cfg)
	if err != nil {
		t.Fatalf("expected no error, got %v", err)
	}

	if result.Attempts != 1 {
		t.Errorf("expected 1 attempt, got %d", result.Attempts)
	}

	if len(result.Response.ToolCalls) != 1 {
		t.Errorf("expected 1 tool call, got %d", len(result.Response.ToolCalls))
	}

	if backend.callCount != 1 {
		t.Errorf("expected backend called once, got %d", backend.callCount)
	}
}

func TestRunInference_Retry_NeedsRetry(t *testing.T) {
	backend := &mockBackend{
		resp: &llm.Response{
			Content:      "This is just text, not a tool call",
			ToolCalls:    nil,
			FinishReason: "stop",
			UsageTokens:  llm.TokenUsage{PromptTokens: 100, CompletionTokens: 50, TotalTokens: 150},
		},
		respOnRetry: &llm.Response{
			Content:      "",
			ToolCalls:    []llm.ToolCall{{ID: "1", Name: "test_tool", Args: map[string]any{"key": "value"}}},
			FinishReason: "tool_calls",
			UsageTokens:  llm.TokenUsage{PromptTokens: 100, CompletionTokens: 50, TotalTokens: 150},
		},
	}

	validator := guardrails.NewResponseValidator([]string{"test_tool"}, false)
	tracker := guardrails.NewErrorTracker()

	cfg := InferenceConfig{
		Client:       backend,
		Validator:    validator,
		ErrorTracker: tracker,
		ToolSpecs:    []llm.ToolDefinition{{Name: "test_tool", Description: "A test tool", InputSchema: map[string]any{}}},
		MaxAttempts:  3,
		StepIndex:    0,
	}

	messages := []llm.Message{
		{Role: llm.RoleUser, Content: "Hello", Meta: llm.MessageMeta{Type: llm.MessageTypeUserInput}},
	}

	result, err := RunInference(context.Background(), messages, cfg)
	if err != nil {
		t.Fatalf("expected no error on retry, got %v", err)
	}

	if backend.callCount != 2 {
		t.Errorf("expected 2 backend calls (original + retry), got %d", backend.callCount)
	}

	if len(result.NewMessages) <= len(messages) {
		t.Errorf("expected messages to be appended with nudge, got %d messages", len(result.NewMessages))
	}
}

func TestRunInference_RetriesExhausted(t *testing.T) {
	backend := &mockBackend{
		resp: &llm.Response{
			Content:      "This is just text, not a tool call",
			ToolCalls:    nil,
			FinishReason: "stop",
			UsageTokens:  llm.TokenUsage{PromptTokens: 100, CompletionTokens: 50, TotalTokens: 150},
		},
	}

	validator := guardrails.NewResponseValidator([]string{"test_tool"}, false)
	tracker := guardrails.NewErrorTracker()

	cfg := InferenceConfig{
		Client:       backend,
		Validator:    validator,
		ErrorTracker: tracker,
		ToolSpecs:    []llm.ToolDefinition{{Name: "test_tool", Description: "A test tool", InputSchema: map[string]any{}}},
		MaxAttempts:  2,
		StepIndex:    0,
	}

	messages := []llm.Message{
		{Role: llm.RoleUser, Content: "Hello", Meta: llm.MessageMeta{Type: llm.MessageTypeUserInput}},
	}

	_, err := RunInference(context.Background(), messages, cfg)
	if !errors.Is(err, ErrRetriesExhausted) {
		t.Errorf("expected ErrRetriesExhausted, got %v", err)
	}

	if backend.callCount != 2 {
		t.Errorf("expected 2 backend calls before exhaustion, got %d", backend.callCount)
	}
}

func TestRunInference_Rescue(t *testing.T) {
	backend := &mockBackend{
		resp: &llm.Response{
			Content:      "{\"tool\": \"test_tool\", \"args\": {\"key\": \"value\"}}",
			ToolCalls:    nil,
			FinishReason: "stop",
			UsageTokens:  llm.TokenUsage{PromptTokens: 100, CompletionTokens: 50, TotalTokens: 150},
		},
	}

	validator := guardrails.NewResponseValidator([]string{"test_tool"}, true)
	tracker := guardrails.NewErrorTracker()

	cfg := InferenceConfig{
		Client:       backend,
		Validator:    validator,
		ErrorTracker: tracker,
		ToolSpecs:    []llm.ToolDefinition{{Name: "test_tool", Description: "A test tool", InputSchema: map[string]any{}}},
		MaxAttempts:  3,
		StepIndex:    0,
	}

	messages := []llm.Message{
		{Role: llm.RoleUser, Content: "Hello", Meta: llm.MessageMeta{Type: llm.MessageTypeUserInput}},
	}

	result, err := RunInference(context.Background(), messages, cfg)
	if err != nil {
		t.Fatalf("expected no error with rescue, got %v", err)
	}

	if len(result.Response.ToolCalls) == 0 {
		t.Error("expected rescued tool calls")
	}

	if backend.callCount != 1 {
		t.Errorf("expected 1 backend call (rescue succeeded), got %d", backend.callCount)
	}
}

func TestRunInference_DefaultMaxAttempts(t *testing.T) {
	backend := &mockBackend{
		resp: &llm.Response{
			Content:      "This is just text, not a tool call",
			ToolCalls:    nil,
			FinishReason: "stop",
			UsageTokens:  llm.TokenUsage{PromptTokens: 100, CompletionTokens: 50, TotalTokens: 150},
		},
	}

	validator := guardrails.NewResponseValidator([]string{"test_tool"}, false)
	tracker := guardrails.NewErrorTracker()

	cfg := InferenceConfig{
		Client:       backend,
		Validator:    validator,
		ErrorTracker: tracker,
		ToolSpecs:    []llm.ToolDefinition{{Name: "test_tool", Description: "A test tool", InputSchema: map[string]any{}}},
		MaxAttempts:  0,
		StepIndex:    0,
	}

	messages := []llm.Message{
		{Role: llm.RoleUser, Content: "Hello", Meta: llm.MessageMeta{Type: llm.MessageTypeUserInput}},
	}

	_, err := RunInference(context.Background(), messages, cfg)
	if !errors.Is(err, ErrRetriesExhausted) {
		t.Errorf("expected ErrRetriesExhausted with default max attempts, got %v", err)
	}

	if backend.callCount != 3 {
		t.Errorf("expected 3 backend calls with default max attempts (3), got %d", backend.callCount)
	}
}

func TestRunInference_StripsReasoningBeforeToolCallParsing(t *testing.T) {
	backend := &mockBackend{
		resp: &llm.Response{
			// Reasoning embedded as thinking tags must be stripped before the
			// validator sees the content; otherwise the JSON rescue never runs.
			Content:      "<thinking>goal: answer\n\nobservation: the index is fresh</thinking>\n{\"tool\": \"test_tool\", \"args\": {\"key\": \"value\"}}",
			ToolCalls:    nil,
			FinishReason: "stop",
			UsageTokens:  llm.TokenUsage{PromptTokens: 100, CompletionTokens: 50, TotalTokens: 150},
		},
	}

	validator := guardrails.NewResponseValidator([]string{"test_tool"}, true)
	tracker := guardrails.NewErrorTracker()

	cfg := InferenceConfig{
		Client:       backend,
		Validator:    validator,
		ErrorTracker: tracker,
		ToolSpecs:    []llm.ToolDefinition{{Name: "test_tool", Description: "A test tool", InputSchema: map[string]any{}}},
		MaxAttempts:  3,
		StepIndex:    0,
	}

	messages := []llm.Message{
		{Role: llm.RoleUser, Content: "Hello", Meta: llm.MessageMeta{Type: llm.MessageTypeUserInput}},
	}

	result, err := RunInference(context.Background(), messages, cfg)
	if err != nil {
		t.Fatalf("expected no error, got %v", err)
	}

	if len(result.Response.ToolCalls) != 1 {
		t.Fatalf("expected 1 tool call parsed from stripped content, got %d", len(result.Response.ToolCalls))
	}
	if result.Response.ToolCalls[0].Name != "test_tool" {
		t.Errorf("expected tool test_tool, got %q", result.Response.ToolCalls[0].Name)
	}

	// The reasoning tree must be attached with bounded summaries, never the
	// raw reasoning text.
	if result.ReasoningTree == nil {
		t.Fatal("expected a reasoning tree")
	}
	if len(result.ReasoningTree.Nodes) != 2 {
		t.Fatalf("expected 2 reasoning nodes, got %d", len(result.ReasoningTree.Nodes))
	}
	for _, n := range result.ReasoningTree.Nodes {
		if len(n.Summary) > 512 {
			t.Errorf("summary not bounded: %q", n.Summary)
		}
		if n.Summary == "goal: answer\n\nobservation: the index is fresh" {
			t.Error("raw reasoning leaked into the tree")
		}
	}
}

func TestRunInference_NativeReasoningField(t *testing.T) {
	backend := &mockBackend{
		resp: &llm.Response{
			Content:      `{"tool": "test_tool", "args": {"key": "value"}}`,
			Reasoning:    "observation: the index is fresh\n\ndecision: call test_tool",
			ToolCalls:    nil,
			FinishReason: "stop",
			UsageTokens:  llm.TokenUsage{PromptTokens: 100, CompletionTokens: 50, TotalTokens: 150},
		},
	}

	validator := guardrails.NewResponseValidator([]string{"test_tool"}, true)
	cfg := InferenceConfig{
		Client:      backend,
		Validator:   validator,
		ToolSpecs:   []llm.ToolDefinition{{Name: "test_tool", Description: "A test tool", InputSchema: map[string]any{}}},
		MaxAttempts: 3,
		StepIndex:   0,
	}

	messages := []llm.Message{
		{Role: llm.RoleUser, Content: "Hello", Meta: llm.MessageMeta{Type: llm.MessageTypeUserInput}},
	}

	result, err := RunInference(context.Background(), messages, cfg)
	if err != nil {
		t.Fatalf("expected no error, got %v", err)
	}

	if result.ReasoningTree == nil || result.ReasoningTree.Format != "native" {
		t.Fatalf("expected a native-format reasoning tree, got %+v", result.ReasoningTree)
	}
	if len(result.Response.ToolCalls) != 1 {
		t.Fatalf("expected 1 tool call, got %d", len(result.Response.ToolCalls))
	}
}

func TestRunInference_FailsClosedOnUnbalancedReasoning(t *testing.T) {
	backend := &mockBackend{
		resp: &llm.Response{
			Content:      "<thinking>never closed\n{\"tool\": \"test_tool\", \"args\": {}}",
			ToolCalls:    nil,
			FinishReason: "stop",
			UsageTokens:  llm.TokenUsage{PromptTokens: 100, CompletionTokens: 50, TotalTokens: 150},
		},
	}

	validator := guardrails.NewResponseValidator([]string{"test_tool"}, true)
	cfg := InferenceConfig{
		Client:      backend,
		Validator:   validator,
		ToolSpecs:   []llm.ToolDefinition{{Name: "test_tool", Description: "A test tool", InputSchema: map[string]any{}}},
		MaxAttempts: 3,
		StepIndex:   0,
	}

	messages := []llm.Message{
		{Role: llm.RoleUser, Content: "Hello", Meta: llm.MessageMeta{Type: llm.MessageTypeUserInput}},
	}

	// Malformed reasoning never produces a tool call from a partial parse.
	if _, err := RunInference(context.Background(), messages, cfg); err == nil {
		t.Fatal("expected an error (fail closed on unbalanced reasoning)")
	}
}
