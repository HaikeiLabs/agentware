package middleware

import (
	"context"
	"testing"
	"time"

	"github.com/haikeilabs/agentware/go/tools"
)

// The action-tool / data-source-connector boundary, at the Go reference level:
// the middleware governs an isolated tool loop with a local policy, with no
// external ABAC/catalog dependency, and can separate an action (mutation) tool
// from a read tool by policy alone. Delegation fields are preserved into the
// audit record.
//
// The Go library has no kei/ABAC module at all, so the "no external dependency"
// property is structural; these tests pin the behavioral side of it.

type boundaryTool struct {
	name string
	out  string
}

func (b *boundaryTool) Name() string        { return b.name }
func (b *boundaryTool) Description() string { return b.name }
func (b *boundaryTool) Execute(ctx context.Context, args map[string]any) (*tools.Result, error) {
	return &tools.Result{Success: true, Output: b.out}, nil
}

// localPolicy is a test PolicyEvaluator wired with allow/deny rules for the
// isolated local loop tests.
type localPolicy struct {
	rules map[string]Action
}

func (p *localPolicy) Evaluate(toolName string, _ map[string]any, _ CallerContext) Decision {
	if a, ok := p.rules[toolName]; ok {
		return Decision{Action: a, Rule: toolName, Reason: "test rule", Timestamp: time.Now()}
	}
	return Decision{Action: ActionDeny, Rule: "default", Reason: "default deny", Timestamp: time.Now()}
}

// Invariant 3 + 6: a local policy governs an isolated loop and separates an
// action tool from a read tool with no external ABAC/catalog consulted.
func TestIsolatedLocalLoopSeparatesActionFromRead(t *testing.T) {
	registry := tools.NewToolRegistry()
	registry.Register(&boundaryTool{name: "github.read", out: "read"})
	registry.Register(&boundaryTool{name: "github.create_issue", out: "mutation"})

	auditor := &mockAuditor{}
	mw := NewMiddleware(tools.NewRegistryExecutor(registry)).
		WithPolicy(&localPolicy{rules: map[string]Action{
			"github.create_issue": ActionDeny,
			"github.read":         ActionAllow,
		}}).
		WithAuditor(auditor)

	readRes, err := mw.Execute(context.Background(), "github.read", map[string]any{})
	if err != nil {
		t.Fatalf("read: unexpected error: %v", err)
	}
	if !readRes.Success {
		t.Errorf("read tool should be allowed, got %+v", readRes)
	}

	actionRes, err := mw.Execute(context.Background(), "github.create_issue", map[string]any{})
	if err != nil {
		t.Fatalf("action: unexpected error: %v", err)
	}
	if actionRes.Success {
		t.Errorf("action tool should be denied, got %+v", actionRes)
	}
	if actionRes.Error == "" {
		t.Error("denied action tool should carry a reason")
	}

	if len(auditor.records) != 2 {
		t.Fatalf("expected 2 audit records, got %d", len(auditor.records))
	}
	decisions := map[string]string{}
	for _, r := range auditor.records {
		decisions[r.ToolName] = r.Decision
	}
	if decisions["github.read"] != string(ActionAllow) {
		t.Errorf("read decision = %q, want allow", decisions["github.read"])
	}
	if decisions["github.create_issue"] != string(ActionDeny) {
		t.Errorf("action decision = %q, want deny", decisions["github.create_issue"])
	}
}

// Invariant 5: delegation fields survive into the audit record through the
// isolated local loop — the human subject, parent span, and depth are preserved
// even when the subagent runs under its own service identity.
func TestIsolatedLocalLoopPreservesDelegationInAudit(t *testing.T) {
	registry := tools.NewToolRegistry()
	registry.Register(&boundaryTool{name: "github.read", out: "read"})

	auditor := &mockAuditor{}
	mw := NewMiddleware(tools.NewRegistryExecutor(registry)).
		WithPolicy(&localPolicy{rules: map[string]Action{"github.read": ActionAllow}}).
		WithAuditor(auditor)

	parent := CallerContext{UserID: "U_HUMAN", InvokingSubject: "U_HUMAN", SessionID: "C1"}
	sub := parent.Delegate("sub-1")
	sub.UserID = "U_AGENT" // the subagent runs under its own service id

	ctx := WithCallerContext(context.Background(), sub)
	if _, err := mw.Execute(ctx, "github.read", map[string]any{}); err != nil {
		t.Fatalf("unexpected error: %v", err)
	}

	if len(auditor.records) != 1 {
		t.Fatalf("expected 1 audit record, got %d", len(auditor.records))
	}
	rec := auditor.records[0]
	if rec.InvokingSubject != "U_HUMAN" {
		t.Errorf("InvokingSubject = %q, want U_HUMAN", rec.InvokingSubject)
	}
	if rec.ParentSpan != "sub-1" {
		t.Errorf("ParentSpan = %q, want sub-1", rec.ParentSpan)
	}
	if rec.DelegationDepth != 1 {
		t.Errorf("DelegationDepth = %d, want 1", rec.DelegationDepth)
	}
}
