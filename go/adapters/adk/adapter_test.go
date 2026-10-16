package adk

import (
	"context"
	"errors"
	"testing"

	"github.com/haikeilabs/agentware/go/middleware"
	"github.com/haikeilabs/agentware/go/tools"
)

type mockExecutor struct {
	executeFunc func(ctx context.Context, toolName string, args map[string]any) (*tools.Result, error)
}

func (e *mockExecutor) Execute(ctx context.Context, toolName string, args map[string]any) (*tools.Result, error) {
	if e.executeFunc != nil {
		return e.executeFunc(ctx, toolName, args)
	}
	return &tools.Result{Success: true, Output: "ok"}, nil
}

type mockPolicy struct {
	decision middleware.Decision
}

func (p *mockPolicy) Evaluate(toolName string, args map[string]any, caller middleware.CallerContext) middleware.Decision {
	return p.decision
}

type mockAuditor struct {
	records []middleware.AuditRecord
}

func (a *mockAuditor) Record(record middleware.AuditRecord) {
	a.records = append(a.records, record)
}

func (a *mockAuditor) Query(filter middleware.AuditFilter) []middleware.AuditRecord {
	return a.records
}

func TestNewAdapter(t *testing.T) {
	exec := &mockExecutor{}
	m := middleware.NewMiddleware(exec)
	adapter := NewAdapter(m)

	if adapter == nil {
		t.Fatal("expected adapter to be non-nil")
	}
	if adapter.middleware == nil {
		t.Error("expected middleware to be set")
	}
}

func TestBeforeToolCallback_WithMiddleware(t *testing.T) {
	exec := &mockExecutor{}
	m := middleware.NewMiddleware(exec)
	adapter := NewAdapter(m)

	err := adapter.BeforeToolCallback("test_tool", map[string]any{"arg": "value"})
	if err != nil {
		t.Errorf("expected no error, got %v", err)
	}
}

func TestBeforeToolCallback_WithCallback(t *testing.T) {
	exec := &mockExecutor{}
	m := middleware.NewMiddleware(exec)
	adapter := NewAdapter(m)

	var called bool
	adapter.RegisterBeforeCallback(func(toolName string, args map[string]any) error {
		called = true
		return nil
	})

	err := adapter.BeforeToolCallback("test_tool", map[string]any{})
	if err != nil {
		t.Errorf("expected no error, got %v", err)
	}
	if !called {
		t.Error("expected callback to be called")
	}
}

func TestBeforeToolCallback_CallbackError(t *testing.T) {
	exec := &mockExecutor{}
	m := middleware.NewMiddleware(exec)
	adapter := NewAdapter(m)

	adapter.RegisterBeforeCallback(func(toolName string, args map[string]any) error {
		return errors.New("callback error")
	})

	err := adapter.BeforeToolCallback("test_tool", map[string]any{})
	if err == nil {
		t.Error("expected error from callback")
	}
}

func TestAfterToolCallback(t *testing.T) {
	exec := &mockExecutor{}
	m := middleware.NewMiddleware(exec)
	adapter := NewAdapter(m)

	var calledTool string
	var calledArgs map[string]any
	var calledResult *tools.Result

	adapter.RegisterAfterCallback(func(toolName string, args map[string]any, result *tools.Result, err error) {
		calledTool = toolName
		calledArgs = args
		calledResult = result
	})

	result := &tools.Result{Success: true, Output: "test output"}
	adapter.AfterToolCallback("test_tool", map[string]any{"key": "value"}, result, nil)

	if calledTool != "test_tool" {
		t.Errorf("expected tool name 'test_tool', got %s", calledTool)
	}
	if calledArgs["key"] != "value" {
		t.Errorf("expected args to be passed correctly")
	}
	if !calledResult.Success {
		t.Error("expected result to be passed")
	}
}

func TestExecute(t *testing.T) {
	exec := &mockExecutor{
		executeFunc: func(ctx context.Context, toolName string, args map[string]any) (*tools.Result, error) {
			return &tools.Result{Success: true, Output: "executed"}, nil
		},
	}
	m := middleware.NewMiddleware(exec)
	adapter := NewAdapter(m)

	result, err := adapter.Execute(context.Background(), "test_tool", map[string]any{})
	if err != nil {
		t.Errorf("expected no error, got %v", err)
	}
	if !result.Success {
		t.Error("expected success")
	}
	if result.Output != "executed" {
		t.Errorf("expected output 'executed', got %s", result.Output)
	}
}

func TestExecute_StampsADKFramework(t *testing.T) {
	auditor := &mockAuditor{}
	m := middleware.NewMiddleware(&mockExecutor{}).WithAuditor(auditor)
	adapter := NewAdapter(m)

	if _, err := adapter.Execute(context.Background(), "test_tool", map[string]any{}); err != nil {
		t.Fatalf("unexpected error: %v", err)
	}

	if len(auditor.records) == 0 {
		t.Fatal("expected at least one audit record")
	}
	for _, record := range auditor.records {
		if record.Framework != "adk" {
			t.Errorf("expected framework %q, got %q", "adk", record.Framework)
		}
	}
}

func TestExecute_DeniedByPolicy(t *testing.T) {
	exec := &mockExecutor{}
	policy := &mockPolicy{decision: middleware.Decision{Action: middleware.ActionDeny, Reason: "denied"}}
	m := middleware.NewMiddleware(exec).WithPolicy(policy)
	adapter := NewAdapter(m)

	result, err := adapter.Execute(context.Background(), "test_tool", map[string]any{})
	if err != nil && err.Error() != "denied by policy: denied" {
		t.Errorf("unexpected error: %v", err)
	}
	if result != nil && result.Success {
		t.Error("expected failure due to policy")
	}
}

func TestListTools(t *testing.T) {
	exec := &mockExecutor{}
	m := middleware.NewMiddleware(exec)
	adapter := NewAdapter(m)

	adapter.RegisterTool(ToolDefinition{
		Name:        "tool1",
		Description: "First tool",
		Parameters:  map[string]any{},
	})
	adapter.RegisterTool(ToolDefinition{
		Name:        "tool2",
		Description: "Second tool",
		Parameters:  map[string]any{},
	})

	tools := adapter.ListTools()
	if len(tools) != 2 {
		t.Errorf("expected 2 tools, got %d", len(tools))
	}
	if tools[0].Name != "tool1" {
		t.Errorf("expected first tool to be 'tool1'")
	}
}

func TestWithPolicy(t *testing.T) {
	exec := &mockExecutor{}
	m := middleware.NewMiddleware(exec)
	adapter := NewAdapter(m)

	policy := &mockPolicy{decision: middleware.Decision{Action: middleware.ActionAllow}}
	adapter.WithPolicy(policy)

	if adapter.middleware == nil {
		t.Error("expected middleware to be set")
	}
}

func TestWithAuditor(t *testing.T) {
	exec := &mockExecutor{}
	m := middleware.NewMiddleware(exec)
	adapter := NewAdapter(m)

	auditor := &mockAuditor{}
	adapter.WithAuditor(auditor)

	if adapter.middleware == nil {
		t.Error("expected middleware to be set")
	}
}

func TestRegisterTool(t *testing.T) {
	exec := &mockExecutor{}
	m := middleware.NewMiddleware(exec)
	adapter := NewAdapter(m)

	adapter.RegisterTool(ToolDefinition{
		Name:        "custom_tool",
		Description: "A custom tool",
		Parameters: map[string]any{
			"type": "object",
		},
	})

	tools := adapter.ListTools()
	if len(tools) != 1 {
		t.Fatalf("expected 1 tool, got %d", len(tools))
	}
	if tools[0].Name != "custom_tool" {
		t.Errorf("expected tool name 'custom_tool', got %s", tools[0].Name)
	}
}
