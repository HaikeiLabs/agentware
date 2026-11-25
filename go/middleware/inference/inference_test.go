package inference

import (
	"context"
	"errors"
	"strings"
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

// recordingBackend replays scripted responses in order and records the
// messages of every request, so tests can assert on the retry history.
type recordingBackend struct {
	responses []*llm.Response
	callCount int
	requests  [][]llm.Message
}

func (m *recordingBackend) Complete(ctx context.Context, req *llm.Request) (*llm.Response, error) {
	msgs := make([]llm.Message, len(req.Messages))
	copy(msgs, req.Messages)
	m.requests = append(m.requests, msgs)
	m.callCount++
	if m.callCount > len(m.responses) {
		return m.responses[len(m.responses)-1], nil
	}
	return m.responses[m.callCount-1], nil
}

func (m *recordingBackend) SupportsNativeToolCalling() bool { return true }

func (m *recordingBackend) ModelName() string { return "test-model" }

func (m *recordingBackend) ContextWindowSize() int { return 8192 }

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

func TestRunInference_RetryOrder_AssistantThenNudge(t *testing.T) {
	backend := &recordingBackend{
		responses: []*llm.Response{
			{
				Content:      "Tokyo is warmer at 22 degrees today.",
				ToolCalls:    nil,
				FinishReason: "stop",
				UsageTokens:  llm.TokenUsage{PromptTokens: 100, CompletionTokens: 50, TotalTokens: 150},
			},
			{
				Content:      "",
				ToolCalls:    []llm.ToolCall{{ID: "1", Name: "test_tool", Args: map[string]any{}}},
				FinishReason: "tool_calls",
				UsageTokens:  llm.TokenUsage{PromptTokens: 100, CompletionTokens: 50, TotalTokens: 150},
			},
		},
	}

	validator := guardrails.NewResponseValidator([]string{"test_tool"}, false)
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
		t.Fatalf("expected no error on retry, got %v", err)
	}
	if result.Attempts != 2 {
		t.Errorf("expected 2 attempts, got %d", result.Attempts)
	}
	if len(backend.requests) != 2 {
		t.Fatalf("expected 2 recorded requests, got %d", len(backend.requests))
	}

	second := backend.requests[1]
	tail := second[len(second)-2:]
	if tail[0].Role != llm.RoleAssistant || tail[0].Content != "Tokyo is warmer at 22 degrees today." {
		t.Errorf("expected the failed assistant turn before the nudge, got role %v content %q", tail[0].Role, tail[0].Content)
	}
	if tail[0].Meta.Type != llm.MessageTypeTextResponse {
		t.Errorf("expected text_response meta on the failed turn, got %v", tail[0].Meta.Type)
	}
	if tail[1].Role != llm.RoleUser {
		t.Errorf("expected the retry to end with a user message, got %v", tail[1].Role)
	}
	if tail[1].Meta.Type != llm.MessageTypeRetryNudge {
		t.Errorf("expected retry_nudge meta on the last message, got %v", tail[1].Meta.Type)
	}

	// The returned history carries the same order.
	resultTail := result.NewMessages[len(result.NewMessages)-2:]
	if resultTail[0].Role != llm.RoleAssistant || resultTail[1].Role != llm.RoleUser {
		t.Errorf("expected result.NewMessages tail [assistant, user], got [%v, %v]", resultTail[0].Role, resultTail[1].Role)
	}
}

func TestRunInference_RetrySkipsEmptyAssistantTurn(t *testing.T) {
	backend := &recordingBackend{
		responses: []*llm.Response{
			{
				Content:      "",
				ToolCalls:    []llm.ToolCall{{ID: "1", Name: "weather_lookup", Args: map[string]any{}}},
				FinishReason: "tool_calls",
				UsageTokens:  llm.TokenUsage{PromptTokens: 100, CompletionTokens: 50, TotalTokens: 150},
			},
			{
				Content:      "",
				ToolCalls:    []llm.ToolCall{{ID: "2", Name: "test_tool", Args: map[string]any{}}},
				FinishReason: "tool_calls",
				UsageTokens:  llm.TokenUsage{PromptTokens: 100, CompletionTokens: 50, TotalTokens: 150},
			},
		},
	}

	validator := guardrails.NewResponseValidator([]string{"test_tool"}, false)
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
		t.Fatalf("expected no error on retry, got %v", err)
	}
	if len(backend.requests) != 2 {
		t.Fatalf("expected 2 recorded requests, got %d", len(backend.requests))
	}

	second := backend.requests[1]
	for _, msg := range second {
		if msg.Role == llm.RoleAssistant && msg.Content == "" {
			t.Error("retry history must not contain an empty assistant message")
		}
	}
	last := second[len(second)-1]
	if last.Role != llm.RoleUser {
		t.Errorf("expected the retry to end with a user message, got %v", last.Role)
	}
	// The nudge names the failed call, the only record of it left.
	if !strings.Contains(last.Content, "weather_lookup") {
		t.Errorf("expected the nudge to name the failed call, got %q", last.Content)
	}
	if result.NewMessages[len(result.NewMessages)-1].Role != llm.RoleUser {
		t.Errorf("expected result.NewMessages to end with a user message")
	}
}

func TestRunInference_RetrySkipsWhitespaceOnlyAssistantTurn(t *testing.T) {
	backend := &recordingBackend{
		responses: []*llm.Response{
			{
				Content:      "   \n\t  ",
				ToolCalls:    nil,
				FinishReason: "stop",
				UsageTokens:  llm.TokenUsage{PromptTokens: 100, CompletionTokens: 50, TotalTokens: 150},
			},
			{
				Content:      "",
				ToolCalls:    []llm.ToolCall{{ID: "1", Name: "test_tool", Args: map[string]any{}}},
				FinishReason: "tool_calls",
				UsageTokens:  llm.TokenUsage{PromptTokens: 100, CompletionTokens: 50, TotalTokens: 150},
			},
		},
	}

	validator := guardrails.NewResponseValidator([]string{"test_tool"}, false)
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
		t.Fatalf("expected no error on retry, got %v", err)
	}
	if len(backend.requests) != 2 {
		t.Fatalf("expected 2 recorded requests, got %d", len(backend.requests))
	}

	second := backend.requests[1]
	for _, msg := range second {
		if msg.Role == llm.RoleAssistant && strings.TrimSpace(msg.Content) == "" {
			t.Error("retry history must not contain a whitespace-only assistant message")
		}
	}
	if second[len(second)-1].Role != llm.RoleUser {
		t.Errorf("expected the retry to end with a user message, got %v", second[len(second)-1].Role)
	}
	if result.NewMessages[len(result.NewMessages)-1].Role != llm.RoleUser {
		t.Errorf("expected result.NewMessages to end with a user message")
	}
}

func TestRunInference_RetryHistoryHasNoOrphanToolCalls(t *testing.T) {
	backend := &recordingBackend{
		responses: []*llm.Response{
			{
				// Text response with content AND a native tool call to an
				// unknown tool: the turn is kept for its text but the
				// tool_calls are dropped.
				Content:      "Let me check the weather first.",
				ToolCalls:    []llm.ToolCall{{ID: "1", Name: "weather_lookup", Args: map[string]any{}}},
				FinishReason: "tool_calls",
				UsageTokens:  llm.TokenUsage{PromptTokens: 100, CompletionTokens: 50, TotalTokens: 150},
			},
			{
				Content:      "",
				ToolCalls:    []llm.ToolCall{{ID: "2", Name: "test_tool", Args: map[string]any{}}},
				FinishReason: "tool_calls",
				UsageTokens:  llm.TokenUsage{PromptTokens: 100, CompletionTokens: 50, TotalTokens: 150},
			},
		},
	}

	validator := guardrails.NewResponseValidator([]string{"test_tool"}, false)
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
		t.Fatalf("expected no error on retry, got %v", err)
	}
	if len(backend.requests) != 2 {
		t.Fatalf("expected 2 recorded requests, got %d", len(backend.requests))
	}

	second := backend.requests[1]
	for _, msg := range second {
		if msg.Role == llm.RoleAssistant && len(msg.ToolCalls) > 0 {
			t.Error("retry history must not carry unanswered tool_calls")
		}
	}
	last := second[len(second)-1]
	if last.Role != llm.RoleUser || !strings.Contains(last.Content, "weather_lookup") {
		t.Errorf("expected the retry to end with a nudge naming the failed call, got %v %q", last.Role, last.Content)
	}
	previous := second[len(second)-2]
	if previous.Role != llm.RoleAssistant || previous.Content != "Let me check the weather first." {
		t.Errorf("expected the failed assistant turn (text kept, calls dropped), got role %v content %q", previous.Role, previous.Content)
	}
	resultTail := result.NewMessages[len(result.NewMessages)-2:]
	if resultTail[0].Role != llm.RoleAssistant || resultTail[1].Role != llm.RoleUser {
		t.Errorf("expected result.NewMessages tail [assistant, user], got [%v, %v]", resultTail[0].Role, resultTail[1].Role)
	}
}
