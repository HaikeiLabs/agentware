package llmcontext

import (
	"os"
	"path/filepath"
	"testing"

	"github.com/haikeilabs/agentware/go/llm"
	"github.com/haikeilabs/agentware/go/toolformat"
)

func TestFileContextManager(t *testing.T) {
	tmpDir := t.TempDir()
	mgr := NewFileContextManager(tmpDir)

	jobID := "test-job-123"

	err := mgr.AppendPrompt(jobID, llm.Message{
		Role:    llm.RoleUser,
		Content: "Hello, world",
	})
	if err != nil {
		t.Fatalf("AppendPrompt failed: %v", err)
	}

	err = mgr.AppendResponse(jobID, llm.Message{
		Role:    llm.RoleAssistant,
		Content: "Hi there!",
	})
	if err != nil {
		t.Fatalf("AppendResponse failed: %v", err)
	}

	err = mgr.AppendToolCalls(jobID, []toolformat.ParsedToolCall{
		{ID: "call-1", Name: "test_tool", Args: map[string]any{"arg1": "value1"}},
	})
	if err != nil {
		t.Fatalf("AppendToolCalls failed: %v", err)
	}

	err = mgr.AppendToolResults(jobID, []ToolResultEntry{
		{CallID: "call-1", ToolName: "test_tool", Output: "success", Success: true},
	})
	if err != nil {
		t.Fatalf("AppendToolResults failed: %v", err)
	}

	history, err := mgr.GetHistory(jobID)
	if err != nil {
		t.Fatalf("GetHistory failed: %v", err)
	}

	if len(history) != 3 {
		t.Errorf("expected 3 messages, got %d", len(history))
	}

	if history[0].Role != llm.RoleUser || history[0].Content != "Hello, world" {
		t.Errorf("first message mismatch")
	}

	err = mgr.Purge(jobID)
	if err != nil {
		t.Fatalf("Purge failed: %v", err)
	}

	if _, err := os.Stat(filepath.Join(tmpDir, jobID)); !os.IsNotExist(err) {
		t.Errorf("job directory was not purged")
	}
}

func TestGetHistoryEmptyJob(t *testing.T) {
	tmpDir := t.TempDir()
	mgr := NewFileContextManager(tmpDir)

	history, err := mgr.GetHistory("nonexistent-job")
	if err != nil {
		t.Fatalf("GetHistory failed: %v", err)
	}

	if len(history) != 0 {
		t.Errorf("expected empty history for nonexistent job, got %d messages", len(history))
	}
}
