package llmcontext

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"time"

	"github.com/haikeilabs/agentware/go/llm"
	"github.com/haikeilabs/agentware/go/toolformat"
)

// Entry represents a single entry in the context history.
type Entry struct {
	JobID    string
	Type     EntryType
	Content  string
	Metadata map[string]any
	Time     time.Time
}

// EntryType indicates the type of context entry.
type EntryType int

const (
	// EntryPrompt is an outbound prompt message.
	EntryPrompt EntryType = iota
	// EntryResponse is an inbound LLM response.
	EntryResponse
	// EntryToolCall is a tool call made during execution.
	EntryToolCall
	// EntryToolResult is a result from a tool execution.
	EntryToolResult
)

// FileContextManager is a file-based implementation of ContextManager.
type FileContextManager struct {
	baseDir string
}

// NewFileContextManager creates a new file-based ContextManager
// that stores context in the specified directory.
func NewFileContextManager(baseDir string) ContextManager {
	if err := os.MkdirAll(baseDir, 0755); err != nil {
		panic(fmt.Errorf("failed to create context directory: %w", err))
	}
	return &FileContextManager{baseDir: baseDir}
}

// AppendPrompt records an outbound prompt message.
func (m *FileContextManager) AppendPrompt(jobID string, msg llm.Message) error {
	return m.appendEntry(jobID, Entry{
		JobID:   jobID,
		Type:    EntryPrompt,
		Content: msg.Content,
		Time:    time.Now(),
	})
}

// AppendResponse records an inbound LLM response.
func (m *FileContextManager) AppendResponse(jobID string, msg llm.Message) error {
	return m.appendEntry(jobID, Entry{
		JobID:   jobID,
		Type:    EntryResponse,
		Content: msg.Content,
		Time:    time.Now(),
	})
}

// AppendToolCalls records the parsed tool calls for this round.
func (m *FileContextManager) AppendToolCalls(jobID string, calls []toolformat.ParsedToolCall) error {
	for _, c := range calls {
		metadata := map[string]any{"args": c.Args}
		if c.ID != "" {
			metadata["id"] = c.ID
		}
		if err := m.appendEntry(jobID, Entry{
			JobID:    jobID,
			Type:     EntryToolCall,
			Content:  c.Name,
			Metadata: metadata,
			Time:     time.Now(),
		}); err != nil {
			return err
		}
	}
	return nil
}

// AppendToolResults records the results of tool executions for this round.
func (m *FileContextManager) AppendToolResults(jobID string, results []ToolResultEntry) error {
	for _, r := range results {
		metadata := map[string]any{"tool_name": r.ToolName, "success": r.Success}
		if r.CallID != "" {
			metadata["call_id"] = r.CallID
		}
		if err := m.appendEntry(jobID, Entry{
			JobID:    jobID,
			Type:     EntryToolResult,
			Content:  r.Output,
			Metadata: metadata,
			Time:     time.Now(),
		}); err != nil {
			return err
		}
	}
	return nil
}

// GetHistory reconstructs the full message history for a job.
func (m *FileContextManager) GetHistory(jobID string) ([]llm.Message, error) {
	entries, err := m.readEntries(jobID)
	if err != nil {
		return nil, err
	}

	sort.Slice(entries, func(i, j int) bool {
		return entries[i].Time.Before(entries[j].Time)
	})

	messages := make([]llm.Message, 0, len(entries))
	for _, e := range entries {
		switch e.Type {
		case EntryPrompt:
			messages = append(messages, llm.Message{Role: llm.RoleUser, Content: e.Content})
		case EntryResponse:
			messages = append(messages, llm.Message{Role: llm.RoleAssistant, Content: e.Content})
		case EntryToolResult:
			messages = append(messages, llm.Message{Role: llm.RoleTool, Content: e.Content})
		}
	}
	return messages, nil
}

// Purge deletes all context files for a job.
func (m *FileContextManager) Purge(jobID string) error {
	dir := m.jobDir(jobID)
	return os.RemoveAll(dir)
}

func (m *FileContextManager) jobDir(jobID string) string {
	return filepath.Join(m.baseDir, jobID)
}

func (m *FileContextManager) entriesFile(jobID string) string {
	return filepath.Join(m.jobDir(jobID), "entries.json")
}

func (m *FileContextManager) appendEntry(jobID string, entry Entry) error {
	dir := m.jobDir(jobID)
	if err := os.MkdirAll(dir, 0755); err != nil {
		return fmt.Errorf("failed to create job directory: %w", err)
	}

	entries, err := m.readEntries(jobID)
	if err != nil {
		return fmt.Errorf("failed to read existing entries: %w", err)
	}
	entries = append(entries, entry)

	data, err := json.Marshal(entries)
	if err != nil {
		return fmt.Errorf("failed to marshal entries: %w", err)
	}

	return os.WriteFile(m.entriesFile(jobID), data, 0644)
}

func (m *FileContextManager) readEntries(jobID string) ([]Entry, error) {
	file := m.entriesFile(jobID)
	data, err := os.ReadFile(file)
	if err != nil {
		if os.IsNotExist(err) {
			return []Entry{}, nil
		}
		return nil, fmt.Errorf("failed to read entries: %w", err)
	}

	var entries []Entry
	if err := json.Unmarshal(data, &entries); err != nil {
		return nil, fmt.Errorf("failed to unmarshal entries: %w", err)
	}

	return entries, nil
}
