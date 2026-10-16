package llmcontext

import (
	"github.com/haikeilabs/agentware/go/llm"
	"github.com/haikeilabs/agentware/go/toolformat"
)

// ContextManager manages the durable conversation history for an agent run.
// The default implementation writes each entry to a file, making the run
// recoverable across process restarts.
type ContextManager interface {
	// AppendPrompt records an outbound prompt message.
	AppendPrompt(jobID string, msg llm.Message) error
	// AppendResponse records an inbound LLM response.
	AppendResponse(jobID string, msg llm.Message) error
	// AppendToolCalls records the parsed tool calls for this round.
	AppendToolCalls(jobID string, calls []toolformat.ParsedToolCall) error
	// AppendToolResults records the results of tool executions for this round.
	AppendToolResults(jobID string, results []ToolResultEntry) error
	// GetHistory reconstructs the full message history for a job.
	GetHistory(jobID string) ([]llm.Message, error)
	// Purge deletes all context for a job (call after completion in production).
	Purge(jobID string) error
}

// ToolResultEntry pairs a tool call with its result for context storage.
type ToolResultEntry struct {
	CallID   string
	ToolName string
	Args     map[string]any
	Output   string
	Success  bool
}
