package middleware

import (
	"context"
	"errors"
	"testing"

	"github.com/haikeilabs/agentware/go/tools"
)

func TestNewAuditedToolClient(t *testing.T) {
	client := NewAuditedToolClient("test-source", func(ctx context.Context, toolName string, args map[string]any) (*tools.Result, error) {
		return &tools.Result{Success: true}, nil
	})

	if client == nil {
		t.Fatal("expected non-nil client")
	}
	if client.source != "test-source" {
		t.Errorf("source = %q, want %q", client.source, "test-source")
	}
}

func TestAuditedToolClientExecute_AllowRunsExecutorAndAudits(t *testing.T) {
	var gotTool string
	var gotArgs map[string]any
	client := NewAuditedToolClient("test-source", func(ctx context.Context, toolName string, args map[string]any) (*tools.Result, error) {
		gotTool = toolName
		gotArgs = args
		return &tools.Result{Success: true, Output: "done"}, nil
	})

	result, err := client.Execute(context.Background(), "read_file", map[string]any{"path": "/tmp/x"})
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if !result.Success || result.Output != "done" {
		t.Errorf("result = %+v, want success with output %q", result, "done")
	}
	if gotTool != "read_file" {
		t.Errorf("executor got tool %q, want %q", gotTool, "read_file")
	}
	if gotArgs["path"] != "/tmp/x" {
		t.Errorf("executor got args %v, want path=/tmp/x", gotArgs)
	}

	records := client.Records(AuditFilter{})
	if len(records) != 1 {
		t.Fatalf("got %d audit records, want 1", len(records))
	}
	if records[0].ToolName != "read_file" {
		t.Errorf("record tool = %q, want %q", records[0].ToolName, "read_file")
	}
	if records[0].Decision != string(ActionAllow) {
		t.Errorf("record decision = %q, want %q", records[0].Decision, ActionAllow)
	}
	if !records[0].Success {
		t.Error("record Success = false, want true")
	}
	if records[0].ToolArgsDigest == "" {
		t.Error("record ToolArgsDigest is empty, want a digest")
	}
}

func TestAuditedToolClientExecute_RecordsFramework(t *testing.T) {
	client := NewAuditedToolClient("my-client", func(ctx context.Context, toolName string, args map[string]any) (*tools.Result, error) {
		if got := FrameworkFromContext(ctx); got != "my-client" {
			t.Errorf("framework in ctx = %q, want %q", got, "my-client")
		}
		return &tools.Result{Success: true}, nil
	})

	if _, err := client.Execute(context.Background(), "tool", nil); err != nil {
		t.Fatalf("unexpected error: %v", err)
	}

	records := client.Records(AuditFilter{})
	if len(records) != 1 {
		t.Fatalf("got %d audit records, want 1", len(records))
	}
	if records[0].Framework != "my-client" {
		t.Errorf("record framework = %q, want %q", records[0].Framework, "my-client")
	}
}

func TestAuditedToolClientExecute_DenySkipsExecutor(t *testing.T) {
	executed := false
	client := NewAuditedToolClient("test-source", func(ctx context.Context, toolName string, args map[string]any) (*tools.Result, error) {
		executed = true
		return &tools.Result{Success: true}, nil
	}).WithPolicy(&Policy{DefaultDeny: true})

	result, err := client.Execute(context.Background(), "dangerous_tool", nil)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if executed {
		t.Error("executor ran for a denied call")
	}
	if result.Success {
		t.Error("result.Success = true, want false for a denied call")
	}
	if result.Error == "" {
		t.Error("result.Error is empty, want a denial reason")
	}

	records := client.Records(AuditFilter{})
	if len(records) != 1 {
		t.Fatalf("got %d audit records, want 1", len(records))
	}
	if records[0].Decision != string(ActionDeny) {
		t.Errorf("record decision = %q, want %q", records[0].Decision, ActionDeny)
	}
}

func TestAuditedToolClientExecute_ExecutorErrorIsAudited(t *testing.T) {
	wantErr := errors.New("disk on fire")
	client := NewAuditedToolClient("test-source", func(ctx context.Context, toolName string, args map[string]any) (*tools.Result, error) {
		return nil, wantErr
	})

	_, err := client.Execute(context.Background(), "write_file", nil)
	if !errors.Is(err, wantErr) {
		t.Fatalf("err = %v, want %v", err, wantErr)
	}

	records := client.Records(AuditFilter{})
	if len(records) != 1 {
		t.Fatalf("got %d audit records, want 1", len(records))
	}
	if records[0].Success {
		t.Error("record Success = true, want false")
	}
	if records[0].Error != wantErr.Error() {
		t.Errorf("record error = %q, want %q", records[0].Error, wantErr.Error())
	}
}

func TestAuditedToolClientExecute_NilExecutor(t *testing.T) {
	client := NewAuditedToolClient("test-source", nil)

	result, err := client.Execute(context.Background(), "tool", nil)
	if !errors.Is(err, ErrNoExecutor) {
		t.Fatalf("err = %v, want ErrNoExecutor", err)
	}
	if result != nil && result.Success {
		t.Error("result.Success = true, want false")
	}

	if records := client.Records(AuditFilter{}); len(records) != 1 {
		t.Fatalf("got %d audit records, want 1", len(records))
	}
}

func TestAuditedToolClientExecute_FilterRedactsBeforeExecutor(t *testing.T) {
	var gotArgs map[string]any
	client := NewAuditedToolClient("test-source", func(ctx context.Context, toolName string, args map[string]any) (*tools.Result, error) {
		gotArgs = args
		return &tools.Result{Success: true}, nil
	}).WithPolicy(&Policy{
		Rules: []Rule{{
			Name:         "redact-token",
			Tools:        []string{"*"},
			Action:       ActionFilter,
			RedactFields: []string{"token"},
		}},
	})

	args := map[string]any{"token": "secret-value", "keep": "visible"}
	if _, err := client.Execute(context.Background(), "api_call", args); err != nil {
		t.Fatalf("unexpected error: %v", err)
	}

	if gotArgs["token"] != RedactedPlaceholder {
		t.Errorf("executor saw token = %v, want %q", gotArgs["token"], RedactedPlaceholder)
	}
	if gotArgs["keep"] != "visible" {
		t.Errorf("executor saw keep = %v, want %q", gotArgs["keep"], "visible")
	}
	if args["token"] != "secret-value" {
		t.Errorf("caller's map was mutated: token = %v", args["token"])
	}
}

func TestAuditedToolClientWithAuditorAndHook(t *testing.T) {
	auditor := &mockAuditor{}
	hook := &mockAuditHook{}
	client := NewAuditedToolClient("test-source", func(ctx context.Context, toolName string, args map[string]any) (*tools.Result, error) {
		return &tools.Result{Success: true}, nil
	}).WithAuditor(auditor).AddHook(hook)

	if _, err := client.Execute(context.Background(), "tool", nil); err != nil {
		t.Fatalf("unexpected error: %v", err)
	}

	if len(auditor.records) != 1 {
		t.Fatalf("auditor got %d records, want 1", len(auditor.records))
	}
	if len(hook.records) != 1 {
		t.Fatalf("hook got %d records, want 1", len(hook.records))
	}
	if got := client.Records(AuditFilter{ToolName: "tool"}); len(got) != 1 {
		t.Errorf("Records() returned %d, want 1", len(got))
	}
}

func TestAuditedToolClientExecute_PreservesCallerContext(t *testing.T) {
	client := NewAuditedToolClient("test-source", func(ctx context.Context, toolName string, args map[string]any) (*tools.Result, error) {
		caller, ok := CallerFromContext(ctx)
		if !ok {
			t.Error("caller context missing in executor")
		} else if caller.UserID != "u-1" {
			t.Errorf("caller.UserID = %q, want %q", caller.UserID, "u-1")
		}
		return &tools.Result{Success: true}, nil
	})

	ctx := WithCallerContext(context.Background(), CallerContext{
		UserID:          "u-1",
		SessionID:       "s-1",
		InvokingSubject: "agent-1",
		DelegationDepth: 2,
	})
	if _, err := client.Execute(ctx, "tool", nil); err != nil {
		t.Fatalf("unexpected error: %v", err)
	}

	records := client.Records(AuditFilter{})
	if len(records) != 1 {
		t.Fatalf("got %d audit records, want 1", len(records))
	}
	if records[0].InvokingSubject != "agent-1" {
		t.Errorf("record InvokingSubject = %q, want %q", records[0].InvokingSubject, "agent-1")
	}
	if records[0].DelegationDepth != 2 {
		t.Errorf("record DelegationDepth = %d, want 2", records[0].DelegationDepth)
	}
}
