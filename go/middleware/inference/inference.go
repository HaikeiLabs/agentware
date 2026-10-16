package inference

import (
	"context"
	"errors"

	"github.com/haikeilabs/agentware/go/llm"
	"github.com/haikeilabs/agentware/go/middleware/guardrails"
	"github.com/haikeilabs/agentware/go/reasoning"
)

var ErrRetriesExhausted = errors.New("retries exhausted")

// defaultReasoningAdapter strips and normalizes reasoning in the inference
// loop. Callers can supply a tuned adapter (limits, registered model fields)
// via InferenceConfig.Reasoning.
var defaultReasoningAdapter = reasoning.New()

type InferenceResult struct {
	Response        llm.Response
	NewMessages     []llm.Message
	ToolCallCounter int
	Attempts        int
	// ReasoningTree is the normalized context tree for the final turn's
	// reasoning (native reasoning_content and/or thinking tags). It is nil
	// when the turn carried no reasoning. It holds bounded summaries only;
	// raw reasoning is never attached.
	ReasoningTree *reasoning.ContextTree
}

type InferenceConfig struct {
	Client         llm.Backend
	ContextManager *llm.ContextWindowManager
	Validator      *guardrails.ResponseValidator
	ErrorTracker   *guardrails.ErrorTracker
	StepEnforcer   *guardrails.StepEnforcer
	ToolSpecs      []llm.ToolDefinition
	MaxAttempts    int
	StepIndex      int
	// Reasoning optionally overrides the reasoning adapter used to strip
	// reasoning before tool-call parsing and to build the context tree.
	Reasoning *reasoning.Adapter
}

func RunInference(ctx context.Context, messages []llm.Message, cfg InferenceConfig) (*InferenceResult, error) {
	if cfg.MaxAttempts <= 0 {
		cfg.MaxAttempts = 3
	}

	currentMessages := make([]llm.Message, len(messages))
	copy(currentMessages, messages)

	var lastResponse llm.Response
	var toolCallCounter int

	for attempt := 1; attempt <= cfg.MaxAttempts; attempt++ {
		if cfg.ContextManager != nil {
			if cfg.ContextManager.ShouldCompact(currentMessages) {
				compacted, err := cfg.ContextManager.Compact(currentMessages)
				if err != nil {
					return nil, err
				}
				currentMessages = compacted
			}

			warning := cfg.ContextManager.CheckThresholds(ctx, currentMessages)
			if warning != "" {
				warningMsg := llm.Message{
					Role:    llm.RoleUser,
					Content: warning,
					Meta: llm.MessageMeta{
						Type: llm.MessageTypeContextWarning,
					},
				}
				currentMessages = append(currentMessages, warningMsg)
			}
		}

		req := &llm.Request{
			Messages:    currentMessages,
			Tools:       cfg.ToolSpecs,
			Temperature: 0.7,
			MaxTokens:   4096,
		}

		resp, err := cfg.Client.Complete(ctx, req)
		if err != nil {
			return nil, err
		}

		if cfg.ContextManager != nil && resp.UsageTokens.TotalTokens > 0 {
			cfg.ContextManager.UpdateTokenCount(resp.UsageTokens.TotalTokens)
		}

		var validationResult guardrails.ValidationResult

		// AR-1: normalize reasoning and strip it before ordinary tool-call
		// parsing. Reasoning can arrive as a native reasoning_content field or
		// as embedded thinking tags in content; both are combined into a
		// synthetic structured input so the adapter sees the full picture.
		// Malformed or unbounded reasoning fails closed: this turn is treated
		// as invalid and retried rather than parsed partially.
		var reasoningTree *reasoning.ContextTree
		cleanContent := resp.Content
		reasoningFailed := false
		if resp.Content != "" || resp.Reasoning != "" {
			in := map[string]any{"content": resp.Content}
			if resp.Reasoning != "" {
				in["reasoning_content"] = resp.Reasoning
			}
			rAdapter := defaultReasoningAdapter
			if cfg.Reasoning != nil {
				rAdapter = cfg.Reasoning
			}
			var rerr error
			reasoningTree, rerr = rAdapter.Extract(in, cfg.Client.ModelName(), "llm-backend")
			if rerr == nil {
				cleanContent, rerr = rAdapter.Strip(in)
			}
			if rerr != nil {
				reasoningFailed = true
				// Fail closed: never echo content that may carry reasoning
				// we could not parse.
				cleanContent = ""
				validationResult = guardrails.ValidationResult{
					ToolCalls:  nil,
					Nudge:      guardrails.RetryNudge("malformed reasoning output", getToolNames(cfg.ToolSpecs)),
					NeedsRetry: true,
				}
			}
		}

		if !reasoningFailed {
			if len(resp.ToolCalls) > 0 {
				toolCalls := make([]guardrails.ToolCall, len(resp.ToolCalls))
				for i, tc := range resp.ToolCalls {
					toolCalls[i] = guardrails.ToolCall{
						Tool: tc.Name,
						Args: tc.Args,
					}
				}
				validationResult = cfg.Validator.ValidateToolCalls(toolCalls)
			} else if cleanContent != "" {
				validationResult = cfg.Validator.ValidateTextResponse(cleanContent)
				if !validationResult.NeedsRetry && len(validationResult.ToolCalls) > 0 {
					resp.ToolCalls = make([]llm.ToolCall, len(validationResult.ToolCalls))
					for i, tc := range validationResult.ToolCalls {
						resp.ToolCalls[i] = llm.ToolCall{
							ID:   "",
							Name: tc.Tool,
							Args: tc.Args,
						}
					}
				}
			} else {
				validationResult = guardrails.ValidationResult{
					ToolCalls:  nil,
					Nudge:      guardrails.RetryNudge("empty response", getToolNames(cfg.ToolSpecs)),
					NeedsRetry: true,
				}
			}
		}

		lastResponse = *resp

		if !validationResult.NeedsRetry {
			if cfg.ErrorTracker != nil {
				cfg.ErrorTracker.ResetSession("")
			}

			if cfg.StepEnforcer != nil && len(resp.ToolCalls) > 0 {
				for _, tc := range resp.ToolCalls {
					allowed, missing := cfg.StepEnforcer.CanExecute("", tc.Name)
					if !allowed {
						nudge := guardrails.StepNudge(tc.Name, missing, 1)
						currentMessages = append(currentMessages, llm.Message{
							Role:    llm.RoleUser,
							Content: nudge.Content,
							Meta: llm.MessageMeta{
								Type: llm.MessageTypeStepNudge,
							},
						})
						continue
					}
				}
			}

			toolCallCounter += len(resp.ToolCalls)
			return &InferenceResult{
				Response:        lastResponse,
				NewMessages:     currentMessages,
				ToolCallCounter: toolCallCounter,
				Attempts:        attempt,
				ReasoningTree:   reasoningTree,
			}, nil
		}

		if cfg.ErrorTracker != nil {
			cfg.ErrorTracker.RecordError("", "", nil, errors.New("validation failed"), guardrails.ErrCategoryUnknown)
		}

		if attempt >= cfg.MaxAttempts {
			return nil, ErrRetriesExhausted
		}

		if validationResult.Nudge != nil {
			nudgeMsg := llm.Message{
				Role:    llm.RoleUser,
				Content: validationResult.Nudge.Content,
				Meta: llm.MessageMeta{
					Type: llm.MessageTypeRetryNudge,
				},
			}
			currentMessages = append(currentMessages, nudgeMsg)
		}

		failedMsg := llm.Message{
			Role:    llm.RoleAssistant,
			Content: cleanContent,
			Meta: llm.MessageMeta{
				Type: llm.MessageTypeTextResponse,
			},
		}
		currentMessages = append(currentMessages, failedMsg)
	}

	return nil, ErrRetriesExhausted
}

func getToolNames(specs []llm.ToolDefinition) []string {
	names := make([]string, len(specs))
	for i, spec := range specs {
		names[i] = spec.Name
	}
	return names
}
