package executor

import (
	"context"
	"errors"
	"fmt"

	"github.com/haikeilabs/agentware/go/llm"
	"github.com/haikeilabs/agentware/go/middleware"
	"github.com/haikeilabs/agentware/go/toolformat"
	"github.com/haikeilabs/agentware/go/tools"
)

// Execute runs the inference loop for a given task.
func (e *inferenceExecutor) Execute(ctx context.Context, req ExecuteRequest) (*ExecuteResult, error) {
	conversation := buildConversation(req)

	maxIterations := e.config.MaxIterations
	if req.MaxIterations > 0 {
		maxIterations = req.MaxIterations
	}

	iterations := 0
	toolCallsMade := 0

	for iterations < maxIterations {
		select {
		case <-ctx.Done():
			return &ExecuteResult{
				FinalResponse:     conversation[len(conversation)-1].Content,
				Iterations:        iterations,
				ToolCallsMade:     toolCallsMade,
				TerminationReason: TerminationCanceled,
				Conversation:      conversation,
			}, nil
		default:
		}

		toolsList := e.config.Registry.All()
		llmTools := make([]llm.ToolDefinition, 0, len(toolsList))
		for _, t := range toolsList {
			inputSchema := map[string]any{}
			if ext, ok := t.(interface{ InputSchema() map[string]any }); ok {
				inputSchema = ext.InputSchema()
			}
			llmTools = append(llmTools, llm.ToolDefinition{
				Name:        t.Name(),
				Description: t.Description(),
				InputSchema: inputSchema,
			})
		}

		llmReq := &llm.Request{
			Messages:    conversation,
			Temperature: 0.7,
			MaxTokens:   4096,
			Tools:       llmTools,
		}

		if req.Thinking {
			llmReq.Thinking = &llm.ThinkingConfig{
				Type:      "enabled",
				MaxTokens: 8192,
			}
		}

		resp, err := e.config.Backend.Complete(ctx, llmReq)
		if err != nil {
			return &ExecuteResult{
				FinalResponse:     conversation[len(conversation)-1].Content,
				Iterations:        iterations,
				ToolCallsMade:     toolCallsMade,
				TerminationReason: TerminationError,
				Conversation:      conversation,
			}, fmt.Errorf("llm completion failed: %w", err)
		}

		assistantMsg := llm.Message{
			Role:    llm.RoleAssistant,
			Content: resp.Content,
		}
		if len(resp.ToolCalls) > 0 {
			assistantMsg.ToolCalls = resp.ToolCalls
			if assistantMsg.Content == "" {
				assistantMsg.Content = ""
			}
		}
		conversation = append(conversation, assistantMsg)

		if isComplete(resp.Content, e.config.CompletionSignal) {
			return &ExecuteResult{
				FinalResponse:     resp.Content,
				Iterations:        iterations + 1,
				ToolCallsMade:     toolCallsMade,
				TerminationReason: TerminationComplete,
				Conversation:      conversation,
			}, nil
		}

		toolCalls, err := e.toolCalls(resp)
		if errors.Is(err, errFinalAnswer) {
			return &ExecuteResult{
				FinalResponse:     resp.Content,
				Iterations:        iterations + 1,
				ToolCallsMade:     toolCallsMade,
				TerminationReason: TerminationComplete,
				Conversation:      conversation,
			}, nil
		}
		if err != nil {
			return &ExecuteResult{
				FinalResponse:     resp.Content,
				Iterations:        iterations + 1,
				ToolCallsMade:     toolCallsMade,
				TerminationReason: TerminationError,
				Conversation:      conversation,
			}, fmt.Errorf("failed to parse tool calls: %w", err)
		}

		if len(toolCalls) == 0 {
			return &ExecuteResult{
				FinalResponse:     resp.Content,
				Iterations:        iterations + 1,
				ToolCallsMade:     toolCallsMade,
				TerminationReason: TerminationComplete,
				Conversation:      conversation,
			}, nil
		}

		for _, tc := range toolCalls {
			toolCtx := middleware.WithCallerContext(ctx, req.CallerCtx)
			result, err := e.config.ToolExec.Execute(toolCtx, tc.Name, tc.Args)
			if err != nil {
				result = &tools.Result{
					Success: false,
					Error:   err.Error(),
				}
			}

			formattedResult := e.config.Formatter.FormatToolResult(tc.Name, result)
			conversation = append(conversation, llm.Message{
				Role:       llm.RoleTool,
				Content:    formattedResult,
				ToolCallID: tc.ID,
			})
			toolCallsMade++
		}

		iterations++
	}

	return &ExecuteResult{
		FinalResponse:     conversation[len(conversation)-1].Content,
		Iterations:        iterations,
		ToolCallsMade:     toolCallsMade,
		TerminationReason: TerminationMaxIterations,
		Conversation:      conversation,
	}, nil
}

// errFinalAnswer marks a response from a native tool-calling backend whose
// content is prose rather than content-encoded tool calls.
var errFinalAnswer = errors.New("final answer")

// toolCalls returns the calls to run for resp. Native tool calls (OpenAI
// tool_calls) come first and keep their IDs so each tool message answers its
// call with tool_call_id. Otherwise the formatter parses content-encoded
// calls. For a native backend, content the formatter cannot parse is the
// final answer, not an error.
func (e *inferenceExecutor) toolCalls(resp *llm.Response) ([]toolformat.ParsedToolCall, error) {
	if len(resp.ToolCalls) > 0 {
		calls := make([]toolformat.ParsedToolCall, 0, len(resp.ToolCalls))
		for _, tc := range resp.ToolCalls {
			calls = append(calls, toolformat.ParsedToolCall{ID: tc.ID, Name: tc.Name, Args: tc.Args})
		}
		return calls, nil
	}
	calls, err := e.config.Formatter.ParseToolCalls(resp.Content)
	if err != nil && e.config.Backend.SupportsNativeToolCalling() {
		return nil, errFinalAnswer
	}
	return calls, err
}

func buildConversation(req ExecuteRequest) []llm.Message {
	conversation := make([]llm.Message, 0, len(req.History)+2)

	conversation = append(conversation, llm.Message{
		Role:    llm.RoleSystem,
		Content: req.SystemPrompt,
	})

	conversation = append(conversation, req.History...)

	conversation = append(conversation, llm.Message{
		Role:    llm.RoleUser,
		Content: req.UserMessage,
	})

	return conversation
}

func isComplete(response, signal string) bool {
	return len(response) >= len(signal) &&
		(response == signal ||
			(len(response) > len(signal) &&
				containsSubstring(response, signal)))
}

func containsSubstring(s, substr string) bool {
	for i := 0; i <= len(s)-len(substr); i++ {
		if s[i:i+len(substr)] == substr {
			return true
		}
	}
	return false
}
