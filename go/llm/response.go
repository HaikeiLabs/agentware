package llm

import "encoding/json"

// Response is the output from a completion.
type Response struct {
	Content string
	// Reasoning is the raw reasoning the backend returned on a separate
	// channel (e.g. reasoning_content for DeepSeek-style models) when the
	// backend captures it. It is never written into Content, never returned to
	// the end user, and never recorded on a default audit record; consumers
	// normalize it with the reasoning adapter (go/reasoning).
	Reasoning    string
	ToolCalls    []ToolCall
	FinishReason string
	UsageTokens  TokenUsage
}

// ToolCall is a structured tool invocation from the LLM.
type ToolCall struct {
	// Metadata is opaque model-family tool-call metadata; do not log or display it.
	Metadata json.RawMessage

	ID   string
	Name string
	Args map[string]any
}

// TokenUsage tracks prompt and completion token counts.
type TokenUsage struct {
	PromptTokens     int
	CompletionTokens int
	TotalTokens      int
	// CachedTokens is the subset of PromptTokens served from the provider's
	// prompt cache. Zero when the backend does not report cache usage.
	CachedTokens int
}
