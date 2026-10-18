package middleware

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"testing"

	"github.com/haikeilabs/agentware/go/tools"
)

type mockExecutor struct {
	execFn func(ctx context.Context, toolName string, args map[string]any) (*tools.Result, error)
}

func (m *mockExecutor) Execute(ctx context.Context, toolName string, args map[string]any) (*tools.Result, error) {
	if m.execFn != nil {
		return m.execFn(ctx, toolName, args)
	}
	return &tools.Result{Success: true}, nil
}

type mockEvaluator struct {
	decision Decision
}

func (m *mockEvaluator) Evaluate(toolName string, args map[string]any, caller CallerContext) Decision {
	return m.decision
}

type mockAuditor struct {
	records  []AuditRecord
	onRecord func()
}

func (m *mockAuditor) Record(record AuditRecord) {
	m.records = append(m.records, record)
	if m.onRecord != nil {
		m.onRecord()
	}
}

type mockAuditHook struct {
	records          []AuditRecord
	onAfterExecution func()
}

func (m *mockAuditHook) AfterExecution(record AuditRecord) {
	m.records = append(m.records, record)
	if m.onAfterExecution != nil {
		m.onAfterExecution()
	}
}

func (m *mockAuditor) Query(filter AuditFilter) []AuditRecord {
	return m.records
}

func TestNewMiddleware(t *testing.T) {
	exec := &mockExecutor{}
	mw := NewMiddleware(exec)

	if mw == nil {
		t.Fatal("expected non-nil middleware")
	}
}

func TestMiddlewareExecute_Allow(t *testing.T) {
	exec := &mockExecutor{}
	eval := &mockEvaluator{decision: Decision{Action: ActionAllow, Rule: "test"}}
	aud := &mockAuditor{}

	mw := NewMiddleware(exec).WithPolicy(eval).WithAuditor(aud)

	result, err := mw.Execute(context.Background(), "test_tool", map[string]any{"arg": "value"})
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if !result.Success {
		t.Error("expected success")
	}
}

func TestMiddlewareExecute_Deny(t *testing.T) {
	exec := &mockExecutor{}
	eval := &mockEvaluator{decision: Decision{Action: ActionDeny, Rule: "deny_rule", Reason: "not allowed"}}
	aud := &mockAuditor{}

	mw := NewMiddleware(exec).WithPolicy(eval).WithAuditor(aud)

	result, err := mw.Execute(context.Background(), "test_tool", map[string]any{})
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if result.Success {
		t.Error("expected failure")
	}
	if result.Error == "" {
		t.Error("expected error message")
	}
}

func TestMiddlewareExecute_Filter(t *testing.T) {
	var capturedArgs map[string]any
	exec := &mockExecutor{
		execFn: func(ctx context.Context, toolName string, args map[string]any) (*tools.Result, error) {
			capturedArgs = args
			return &tools.Result{Success: true}, nil
		},
	}
	eval := &mockEvaluator{
		decision: Decision{
			Action:       ActionFilter,
			Rule:         "filter_rule",
			RedactedArgs: map[string]any{"secret": "redacted"},
		},
	}

	mw := NewMiddleware(exec).WithPolicy(eval)

	_, err := mw.Execute(context.Background(), "test_tool", map[string]any{"secret": "original"})
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if capturedArgs["secret"] != "redacted" {
		t.Errorf("expected secret to be redacted, got '%v'", capturedArgs["secret"])
	}
}

func TestMiddlewareAuditorRecords(t *testing.T) {
	exec := &mockExecutor{}
	eval := &mockEvaluator{decision: Decision{Action: ActionAllow, Rule: "test"}}
	aud := &mockAuditor{}

	mw := NewMiddleware(exec).WithPolicy(eval).WithAuditor(aud)

	_, err := mw.Execute(context.Background(), "my_tool", map[string]any{})
	if err != nil {
		t.Fatalf("Execute failed: %v", err)
	}

	if len(aud.records) != 1 {
		t.Fatalf("expected 1 audit record, got %d", len(aud.records))
	}
	if aud.records[0].ToolName != "my_tool" {
		t.Errorf("expected tool name 'my_tool', got '%s'", aud.records[0].ToolName)
	}
}

func TestMiddlewareAuditorRecordsDelegationChain(t *testing.T) {
	auditor := &mockAuditor{}
	mw := NewMiddleware(&mockExecutor{}).WithAuditor(auditor)
	ctx := WithCallerContext(context.Background(), CallerContext{
		UserID:          "user-123",
		InvokingSubject: "agent:planner",
		DelegationDepth: 3,
	})

	if _, err := mw.Execute(ctx, "my_tool", map[string]any{}); err != nil {
		t.Fatalf("Execute failed: %v", err)
	}

	if len(auditor.records) != 1 {
		t.Fatalf("expected 1 audit record, got %d", len(auditor.records))
	}
	record := auditor.records[0]
	if record.InvokingSubject != "agent:planner" {
		t.Errorf("expected invoking subject %q, got %q", "agent:planner", record.InvokingSubject)
	}
	if record.DelegationDepth != 3 {
		t.Errorf("expected delegation depth 3, got %d", record.DelegationDepth)
	}
}

func TestMiddlewareAuditorRecordsFramework(t *testing.T) {
	auditor := &mockAuditor{}
	mw := NewMiddleware(&mockExecutor{}).WithAuditor(auditor)
	ctx := WithFramework(context.Background(), "custom-framework")

	if _, err := mw.Execute(ctx, "my_tool", map[string]any{}); err != nil {
		t.Fatalf("Execute failed: %v", err)
	}

	if len(auditor.records) != 1 {
		t.Fatalf("expected 1 audit record, got %d", len(auditor.records))
	}
	if got := auditor.records[0].Framework; got != "custom-framework" {
		t.Errorf("expected framework %q, got %q", "custom-framework", got)
	}
}

func TestMiddlewareAuditHooksRunAfterAuditRecording(t *testing.T) {
	exec := &mockExecutor{}
	eval := &mockEvaluator{decision: Decision{Action: ActionAllow, Rule: "test"}}
	order := make([]string, 0, 3)
	aud := &mockAuditor{onRecord: func() { order = append(order, "audit") }}
	firstHook := &mockAuditHook{onAfterExecution: func() { order = append(order, "first") }}
	secondHook := &mockAuditHook{onAfterExecution: func() { order = append(order, "second") }}

	mw := NewMiddleware(exec).
		WithPolicy(eval).
		WithAuditor(aud).
		AddHook(firstHook).
		AddHook(secondHook)

	_, err := mw.Execute(context.Background(), "my_tool", map[string]any{})
	if err != nil {
		t.Fatalf("Execute failed: %v", err)
	}

	expectedOrder := []string{"audit", "first", "second"}
	if len(order) != len(expectedOrder) {
		t.Fatalf("expected %d calls, got %d: %v", len(expectedOrder), len(order), order)
	}
	for i, expected := range expectedOrder {
		if order[i] != expected {
			t.Fatalf("expected call %d to be %q, got %q", i, expected, order[i])
		}
	}
	if len(firstHook.records) != 1 || firstHook.records[0].ToolName != "my_tool" {
		t.Fatalf("expected first hook to receive the audit record, got %#v", firstHook.records)
	}
	if len(secondHook.records) != 1 || secondHook.records[0].ToolName != "my_tool" {
		t.Fatalf("expected second hook to receive the audit record, got %#v", secondHook.records)
	}
}

func TestMiddlewareAuditHooksRunForDeniedExecution(t *testing.T) {
	exec := &mockExecutor{}
	eval := &mockEvaluator{decision: Decision{Action: ActionDeny, Reason: "not allowed"}}
	hook := &mockAuditHook{}

	mw := NewMiddleware(exec).WithPolicy(eval).AddHook(hook)

	_, err := mw.Execute(context.Background(), "denied_tool", map[string]any{})
	if err != nil {
		t.Fatalf("Execute failed: %v", err)
	}

	if len(hook.records) != 1 {
		t.Fatalf("expected hook to receive 1 audit record, got %d", len(hook.records))
	}
	if hook.records[0].Success {
		t.Error("expected denied audit record to be unsuccessful")
	}
}

func TestWithCallerContext(t *testing.T) {
	caller := CallerContext{
		UserID:    "user123",
		SessionID: "session456",
		Role:      "admin",
	}

	ctx := WithCallerContext(context.Background(), caller)

	if c, ok := ctx.Value(callerContextKey).(CallerContext); !ok {
		t.Error("expected CallerContext in context")
	} else if c.UserID != "user123" {
		t.Errorf("expected UserID 'user123', got '%s'", c.UserID)
	}
}

func TestExecute_CapturesTokenUsage(t *testing.T) {
	auditor := &mockAuditor{}
	mw := NewMiddleware(&mockExecutor{}).WithAuditor(auditor)

	ctx := WithTokenUsage(context.Background(), TokenUsage{
		PromptTokens:     100,
		CompletionTokens: 20,
		CachedTokens:     64,
	})

	if _, err := mw.Execute(ctx, "my_tool", map[string]any{}); err != nil {
		t.Fatalf("unexpected error: %v", err)
	}

	if len(auditor.records) != 1 {
		t.Fatalf("expected 1 record, got %d", len(auditor.records))
	}
	rec := auditor.records[0]
	if rec.TokensIn != 100 {
		t.Errorf("expected TokensIn 100, got %d", rec.TokensIn)
	}
	if rec.TokensOut != 20 {
		t.Errorf("expected TokensOut 20, got %d", rec.TokensOut)
	}
	if rec.CachedTokens != 64 {
		t.Errorf("expected CachedTokens 64, got %d", rec.CachedTokens)
	}
}

// A denied call still records the token usage of the inference turn that
// produced it — the tokens were spent regardless of the policy decision.
func TestExecute_CapturesTokenUsageOnDeny(t *testing.T) {
	auditor := &mockAuditor{}
	mw := NewMiddleware(&mockExecutor{}).
		WithPolicy(&mockEvaluator{decision: Decision{Action: ActionDeny, Reason: "nope"}}).
		WithAuditor(auditor)

	ctx := WithTokenUsage(context.Background(), TokenUsage{
		PromptTokens:     10,
		CompletionTokens: 5,
		CachedTokens:     8,
	})

	if _, err := mw.Execute(ctx, "my_tool", map[string]any{}); err != nil {
		t.Fatalf("unexpected error: %v", err)
	}

	if len(auditor.records) != 1 {
		t.Fatalf("expected 1 record, got %d", len(auditor.records))
	}
	if got := auditor.records[0].CachedTokens; got != 8 {
		t.Errorf("expected CachedTokens 8, got %d", got)
	}
	if got := auditor.records[0].TokensIn; got != 10 {
		t.Errorf("expected TokensIn 10, got %d", got)
	}
}

// Without usage in the context the record keeps zero counts rather than
// carrying stale values from another turn.
func TestExecute_NoTokenUsageInContext(t *testing.T) {
	auditor := &mockAuditor{}
	mw := NewMiddleware(&mockExecutor{}).WithAuditor(auditor)

	if _, err := mw.Execute(context.Background(), "my_tool", map[string]any{}); err != nil {
		t.Fatalf("unexpected error: %v", err)
	}

	if len(auditor.records) != 1 {
		t.Fatalf("expected 1 record, got %d", len(auditor.records))
	}
	rec := auditor.records[0]
	if rec.TokensIn != 0 || rec.TokensOut != 0 || rec.CachedTokens != 0 {
		t.Errorf("expected zero token counts, got in=%d out=%d cached=%d",
			rec.TokensIn, rec.TokensOut, rec.CachedTokens)
	}
}

func TestTokenUsageFromContext(t *testing.T) {
	if _, ok := TokenUsageFromContext(context.Background()); ok {
		t.Error("expected no usage in a bare context")
	}

	ctx := WithTokenUsage(context.Background(), TokenUsage{CachedTokens: 42})
	usage, ok := TokenUsageFromContext(ctx)
	if !ok {
		t.Fatal("expected usage to be present")
	}
	if usage.CachedTokens != 42 {
		t.Errorf("expected 42, got %d", usage.CachedTokens)
	}
}

// The token-usage key must not collide with the caller-context key.
func TestTokenUsage_DoesNotCollideWithCallerContext(t *testing.T) {
	ctx := WithCallerContext(context.Background(), CallerContext{UserID: "u1"})
	ctx = WithTokenUsage(ctx, TokenUsage{CachedTokens: 7})

	caller, ok := CallerFromContext(ctx)
	if !ok || caller.UserID != "u1" {
		t.Errorf("caller context clobbered: ok=%v caller=%+v", ok, caller)
	}
	usage, ok := TokenUsageFromContext(ctx)
	if !ok || usage.CachedTokens != 7 {
		t.Errorf("usage clobbered: ok=%v usage=%+v", ok, usage)
	}
}

// TestMiddlewareFilterEndToEnd exercises the filter evaluation path end to end
// to confirm sensitive args are redacted before the tool runs, the caller's map
// is left untouched, and the audit digest is taken over redacted values.
func TestMiddlewareFilterEndToEnd(t *testing.T) {
	var capturedArgs map[string]any
	exec := &mockExecutor{
		execFn: func(ctx context.Context, toolName string, args map[string]any) (*tools.Result, error) {
			capturedArgs = args
			return &tools.Result{Success: true}, nil
		},
	}
	eval := &mockEvaluator{
		decision: Decision{
			Action:       ActionFilter,
			Rule:         "redact_password",
			RedactedArgs: map[string]any{"password": RedactedPlaceholder},
		},
	}
	aud := &mockAuditor{}
	mw := NewMiddleware(exec).WithPolicy(eval).WithAuditor(aud)

	callerArgs := map[string]any{"user": "alice", "password": "hunter2"}
	if _, err := mw.Execute(context.Background(), "login", callerArgs); err != nil {
		t.Fatalf("unexpected error: %v", err)
	}

	if capturedArgs["password"] != RedactedPlaceholder {
		t.Errorf("tool received unredacted password: %v", capturedArgs["password"])
	}
	if capturedArgs["user"] != "alice" {
		t.Errorf("non-sensitive arg altered: %v", capturedArgs["user"])
	}
	if callerArgs["password"] != "hunter2" {
		t.Error("caller's args map must not be mutated")
	}

	// The audit digest must match the redacted args, not the original ones.
	redacted := map[string]any{"user": "alice", "password": RedactedPlaceholder}
	b, _ := json.Marshal(redacted)
	sum := sha256.Sum256(b)
	want := hex.EncodeToString(sum[:])
	if len(aud.records) != 1 {
		t.Fatalf("expected 1 audit record, got %d", len(aud.records))
	}
	if aud.records[0].ToolArgsDigest != want {
		t.Error("audit digest was computed over unredacted args")
	}
}
