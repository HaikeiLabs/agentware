package middleware

import (
	"context"
	"testing"

	"github.com/haikeilabs/agentware/go/tools"
)

// The delegation contract: InvokingSubject survives every delegation hop,
// ParentSpan and DelegationDepth record where in the chain the call sits, and
// a caller that was never marked trusted stays untrusted (fail-closed).

func TestCallerContextDelegate(t *testing.T) {
	parent := CallerContext{
		UserID:          "pedro-agent",
		SessionID:       "C123",
		InvokingSubject: "U_HUMAN",
	}

	child := parent.Delegate("span-parent")
	grandchild := child.Delegate("span-child")

	if child.InvokingSubject != "U_HUMAN" || grandchild.InvokingSubject != "U_HUMAN" {
		t.Errorf("invoking subject must survive delegation, got %q then %q",
			child.InvokingSubject, grandchild.InvokingSubject)
	}
	if parent.DelegationDepth != 0 || child.DelegationDepth != 1 || grandchild.DelegationDepth != 2 {
		t.Errorf("delegation depth must increment, got %d/%d/%d",
			parent.DelegationDepth, child.DelegationDepth, grandchild.DelegationDepth)
	}
	if child.ParentSpan != "span-parent" || grandchild.ParentSpan != "span-child" {
		t.Errorf("parent span mismatch, got %q then %q", child.ParentSpan, grandchild.ParentSpan)
	}
	if parent.ParentSpan != "" {
		t.Errorf("delegating must not mutate the parent, parent.ParentSpan=%q", parent.ParentSpan)
	}
}

func TestCallerContextDelegate_DropsToOwnParentSpan(t *testing.T) {
	parent := CallerContext{ParentSpan: "outer", InvokingSubject: "U_HUMAN"}

	child := parent.Delegate("")

	if child.ParentSpan != "outer" {
		t.Errorf("expected child to inherit parent's span, got %q", child.ParentSpan)
	}
}

func TestGetCallerContext_MissingContextIsFailClosed(t *testing.T) {
	caller := getCallerContext(context.Background())

	if caller.Trusted {
		t.Error("expected missing caller context to default to Trusted=false")
	}
	if caller.InvokingSubject != "" || caller.ParentSpan != "" || caller.DelegationDepth != 0 {
		t.Errorf("expected empty delegation fields, got %+v", caller)
	}
}

// The audit record is the linkage point: caller delegation fields must appear
// verbatim on the record, and the parent span must come from caller.ParentSpan
// -- not from the session id.
func TestMiddlewareAuditLinksDelegationFields(t *testing.T) {
	auditor := &mockAuditor{}
	mw := NewMiddleware(&mockExecutor{}).WithAuditor(auditor)

	ctx := WithCallerContext(context.Background(), CallerContext{
		UserID:          "u-1",
		SessionID:       "s-1",
		InvokingSubject: "alice@example.com",
		ParentSpan:      "span-7",
		DelegationDepth: 2,
	})
	if _, err := mw.Execute(ctx, "delegate_to_subagent", map[string]any{}); err != nil {
		t.Fatalf("unexpected error: %v", err)
	}

	if len(auditor.records) != 1 {
		t.Fatalf("expected 1 audit record, got %d", len(auditor.records))
	}
	rec := auditor.records[0]
	if rec.InvokingSubject != "alice@example.com" {
		t.Errorf("record InvokingSubject=%q, want %q", rec.InvokingSubject, "alice@example.com")
	}
	if rec.ParentSpan != "span-7" {
		t.Errorf("record ParentSpan=%q, want %q", rec.ParentSpan, "span-7")
	}
	if rec.DelegationDepth != 2 {
		t.Errorf("record DelegationDepth=%d, want 2", rec.DelegationDepth)
	}
}

// A missing caller context must audit as an untrusted, unlinkable call rather
// than fabricating a trusted subject.
func TestMiddlewareAuditWithMissingContextIsFailClosed(t *testing.T) {
	auditor := &mockAuditor{}
	mw := NewMiddleware(&mockExecutor{}).WithAuditor(auditor)

	if _, err := mw.Execute(context.Background(), "some_tool", map[string]any{}); err != nil {
		t.Fatalf("unexpected error: %v", err)
	}

	if len(auditor.records) != 1 {
		t.Fatalf("expected 1 audit record, got %d", len(auditor.records))
	}
	rec := auditor.records[0]
	if rec.InvokingSubject != "" || rec.ParentSpan != "" || rec.DelegationDepth != 0 {
		t.Errorf("expected empty delegation fields on record, got %+v", rec)
	}
}

// Context propagation: the caller must reach both the policy evaluator and
// the underlying executor unchanged.
func TestExecute_PropagatesCallerToPolicyAndExecutor(t *testing.T) {
	var policyCaller *CallerContext
	var execCaller *CallerContext

	exec := &mockExecutor{
		execFn: func(ctx context.Context, toolName string, args map[string]any) (*tools.Result, error) {
			if c, ok := CallerFromContext(ctx); ok {
				execCaller = &c
			}
			return &tools.Result{Success: true}, nil
		},
	}

	caller := CallerContext{
		UserID:          "u-1",
		Role:            "admin",
		Trusted:         true,
		InvokingSubject: "alice@example.com",
		ParentSpan:      "span-7",
		DelegationDepth: 1,
	}

	mw := NewMiddleware(exec).
		WithPolicy(&policyCaptureEvaluator{onEvaluate: func(toolName string, args map[string]any, c CallerContext) {
			policyCaller = &c
		}}).
		WithAuditor(&mockAuditor{})

	ctx := WithCallerContext(context.Background(), caller)
	if _, err := mw.Execute(ctx, "some_tool", map[string]any{}); err != nil {
		t.Fatalf("unexpected error: %v", err)
	}

	if policyCaller == nil {
		t.Fatal("policy evaluator did not receive a caller")
	} else if !sameCaller(*policyCaller, caller) {
		t.Errorf("policy saw caller %+v, want %+v", *policyCaller, caller)
	}
	if execCaller == nil {
		t.Fatal("executor did not receive a caller")
	} else if !sameCaller(*execCaller, caller) {
		t.Errorf("executor saw caller %+v, want %+v", *execCaller, caller)
	}
}

func sameCaller(a, b CallerContext) bool {
	if a.UserID != b.UserID || a.SessionID != b.SessionID || a.Role != b.Role ||
		a.Source != b.Source || a.Trusted != b.Trusted ||
		a.InvokingSubject != b.InvokingSubject || a.ParentSpan != b.ParentSpan ||
		a.DelegationDepth != b.DelegationDepth {
		return false
	}
	if len(a.Metadata) != len(b.Metadata) {
		return false
	}
	for k, v := range a.Metadata {
		if b.Metadata[k] != v {
			return false
		}
	}
	return true
}

type policyCaptureEvaluator struct {
	onEvaluate func(toolName string, args map[string]any, caller CallerContext)
}

func (p *policyCaptureEvaluator) Evaluate(toolName string, args map[string]any, caller CallerContext) Decision {
	if p.onEvaluate != nil {
		p.onEvaluate(toolName, args, caller)
	}
	return Decision{Action: ActionAllow, Rule: "capture"}
}

// Framework stamping must survive into the audit record and, for the audited
// tool client, combine with the delegation chain.
func TestAuditedToolClientAuditLinksDelegationAndFramework(t *testing.T) {
	client := NewAuditedToolClient("third-party-harness", func(ctx context.Context, toolName string, args map[string]any) (*tools.Result, error) {
		return &tools.Result{Success: true}, nil
	})

	ctx := WithCallerContext(context.Background(), CallerContext{
		UserID:          "u-1",
		InvokingSubject: "alice@example.com",
		ParentSpan:      "span-7",
		DelegationDepth: 3,
	})
	if _, err := client.Execute(ctx, "some_tool", map[string]any{}); err != nil {
		t.Fatalf("unexpected error: %v", err)
	}

	records := client.Records(AuditFilter{})
	if len(records) != 1 {
		t.Fatalf("expected 1 audit record, got %d", len(records))
	}
	rec := records[0]
	if rec.Framework != "third-party-harness" {
		t.Errorf("record Framework=%q, want %q", rec.Framework, "third-party-harness")
	}
	if rec.InvokingSubject != "alice@example.com" {
		t.Errorf("record InvokingSubject=%q, want %q", rec.InvokingSubject, "alice@example.com")
	}
	if rec.ParentSpan != "span-7" {
		t.Errorf("record ParentSpan=%q, want %q", rec.ParentSpan, "span-7")
	}
	if rec.DelegationDepth != 3 {
		t.Errorf("record DelegationDepth=%d, want 3", rec.DelegationDepth)
	}
}
