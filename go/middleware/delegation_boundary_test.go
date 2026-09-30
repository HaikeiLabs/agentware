package middleware

import (
	"context"
	"testing"

	"github.com/soypete/pedro-agentware/go/tools"
)

// The delegation envelope carried across the middleware/delegation boundary
// (docs/tenant-proxy-reference.md): invoking_subject is first-class; the
// extended fields ride in CallerContext.Metadata so the envelope survives every
// hop and reaches the decision point and executor unchanged.
var delegationEnvelope = map[string]string{
	"organization": "acme-corp",
	"workspace":    "ws-sales-pipeline",
	"agent_id":     "agent-gh-bot",
	"connector_id": "github-connector",
	"capability":   "github.issue.write",
	"action":       "execute",
	"resource":     "github:repo:acme-corp/sales-pipeline",
	"trace_id":     "trace-2f8a9c",
}

const (
	envelopeHumanSubject = "U_HUMAN"
	envelopeAgentID      = "agent-gh-bot"
)

func copyEnvelope() map[string]string {
	m := make(map[string]string, len(delegationEnvelope))
	for k, v := range delegationEnvelope {
		m[k] = v
	}
	return m
}

// The full envelope survives every delegation hop, and the child running under
// its own service id still carries the human subject.
func TestDelegationEnvelopeSurvivesEveryHop(t *testing.T) {
	human := CallerContext{
		UserID:          envelopeHumanSubject,
		InvokingSubject: envelopeHumanSubject,
		Metadata:        copyEnvelope(),
	}
	child := human.Delegate("span-1")
	child.UserID = envelopeAgentID
	grandchild := child.Delegate("span-2")
	grandchild.UserID = envelopeAgentID

	ctxs := []CallerContext{human, child, grandchild}
	depths := []int{0, 1, 2}
	for i, ctx := range ctxs {
		if ctx.DelegationDepth != depths[i] {
			t.Errorf("hop %d: DelegationDepth = %d, want %d", i, ctx.DelegationDepth, depths[i])
		}
		if ctx.InvokingSubject != envelopeHumanSubject {
			t.Errorf("hop %d: InvokingSubject = %q, want %q", i, ctx.InvokingSubject, envelopeHumanSubject)
		}
		for k, want := range delegationEnvelope {
			if got := ctx.Metadata[k]; got != want {
				t.Errorf("hop %d: metadata[%q] = %q, want %q", i, k, got, want)
			}
		}
	}
}

// Delegate() never rewrites the human subject; the subagent's own id is a
// different field (UserID), not an override of the attribution.
func TestDelegateNeverRewritesTheHumanSubject(t *testing.T) {
	parent := CallerContext{InvokingSubject: envelopeHumanSubject, Metadata: copyEnvelope()}

	child := parent.Delegate("span-1")
	child.UserID = envelopeAgentID

	if child.InvokingSubject != envelopeHumanSubject {
		t.Errorf("child InvokingSubject = %q, want %q", child.InvokingSubject, envelopeHumanSubject)
	}
	if parent.InvokingSubject != envelopeHumanSubject {
		t.Errorf("parent InvokingSubject = %q, must be untouched", parent.InvokingSubject)
	}
	if child.UserID != envelopeAgentID {
		t.Errorf("child UserID = %q, want %q", child.UserID, envelopeAgentID)
	}
}

// A missing caller is fail-closed: untrusted, empty delegation fields, and an
// empty envelope.
func TestMissingContextIsFailClosedWithEmptyEnvelope(t *testing.T) {
	caller := getCallerContext(context.Background())

	if caller.Trusted {
		t.Error("missing caller must default to Trusted=false")
	}
	if caller.InvokingSubject != "" || caller.ParentSpan != "" || caller.DelegationDepth != 0 {
		t.Errorf("expected empty delegation fields, got %+v", caller)
	}
	for k := range delegationEnvelope {
		if v, ok := caller.Metadata[k]; ok && v != "" {
			t.Errorf("missing context must not carry envelope field %q=%q", k, v)
		}
	}
}

// The envelope reaches both the policy decision point and the executor
// unchanged: the middleware is a pass-through, not a re-envelope.
func TestEnvelopeReachesDecisionPointAndExecutorUnchanged(t *testing.T) {
	var policyMeta, execMeta map[string]string

	exec := &mockExecutor{execFn: func(ctx context.Context, name string, args map[string]any) (*tools.Result, error) {
		if c, ok := CallerFromContext(ctx); ok {
			execMeta = c.Metadata
		}
		return &tools.Result{Success: true}, nil
	}}

	caller := CallerContext{
		UserID:          envelopeAgentID,
		InvokingSubject: envelopeHumanSubject,
		Metadata:        copyEnvelope(),
	}

	mw := NewMiddleware(exec).
		WithPolicy(&policyCaptureEvaluator{onEvaluate: func(name string, args map[string]any, c CallerContext) {
			policyMeta = c.Metadata
		}}).
		WithAuditor(&mockAuditor{})

	ctx := WithCallerContext(context.Background(), caller)
	if _, err := mw.Execute(ctx, delegationEnvelope["capability"], map[string]any{}); err != nil {
		t.Fatalf("unexpected error: %v", err)
	}

	for name, meta := range map[string]map[string]string{"policy": policyMeta, "executor": execMeta} {
		if meta == nil {
			t.Fatalf("caller metadata did not reach the %s boundary", name)
		}
		for k, want := range delegationEnvelope {
			if got := meta[k]; got != want {
				t.Errorf("%s boundary saw metadata[%q] = %q, want %q", name, k, got, want)
			}
		}
	}
}

type requireHumanEvaluator struct{}

func (e *requireHumanEvaluator) Evaluate(toolName string, args map[string]any, caller CallerContext) Decision {
	if caller.InvokingSubject == "" {
		return Decision{Action: ActionDeny, Rule: "require-human", Reason: "no human subject"}
	}
	return Decision{Action: ActionAllow, Rule: "human-ok"}
}

// A forged context -- envelope but no human subject -- is denied and audited as
// a denial; the record does not fabricate an attribution.
func TestForgedContextWithoutHumanSubjectIsDenied(t *testing.T) {
	forged := CallerContext{
		UserID: envelopeAgentID,
		Metadata: map[string]string{
			"agent_id": envelopeAgentID,
			"trace_id": "forged-trace",
		},
	}
	auditor := &mockAuditor{}
	mw := NewMiddleware(&mockExecutor{}).
		WithPolicy(&requireHumanEvaluator{}).
		WithAuditor(auditor)

	ctx := WithCallerContext(context.Background(), forged)
	if _, err := mw.Execute(ctx, "github.create_issue", map[string]any{}); err != nil {
		t.Fatalf("unexpected error: %v", err)
	}

	if len(auditor.records) != 1 {
		t.Fatalf("expected 1 audit record, got %d", len(auditor.records))
	}
	if auditor.records[0].Decision != string(ActionDeny) {
		t.Errorf("forged context must be denied, got %+v", auditor.records[0])
	}
	if auditor.records[0].InvokingSubject != "" {
		t.Errorf("denial must not fabricate a subject, got %q", auditor.records[0].InvokingSubject)
	}
}

// A missing caller is denied by a policy that requires a human subject, and the
// record stays empty rather than being promoted.
func TestMissingContextIsDeniedAndNotPromoted(t *testing.T) {
	auditor := &mockAuditor{}
	mw := NewMiddleware(&mockExecutor{}).
		WithPolicy(&requireHumanEvaluator{}).
		WithAuditor(auditor)

	if _, err := mw.Execute(context.Background(), "github.create_issue", map[string]any{}); err != nil {
		t.Fatalf("unexpected error: %v", err)
	}

	if len(auditor.records) != 1 {
		t.Fatalf("expected 1 audit record, got %d", len(auditor.records))
	}
	if auditor.records[0].Decision != string(ActionDeny) {
		t.Errorf("missing context must be denied, got %+v", auditor.records[0])
	}
}

// The audit record is the linkage point: the human subject and chain survive
// into the record even when the subagent runs under its own service id.
func TestAuditRecordPreservesHumanLinkageThroughTheChain(t *testing.T) {
	auditor := &mockAuditor{}
	mw := NewMiddleware(&mockExecutor{}).WithAuditor(auditor)

	human := CallerContext{UserID: envelopeHumanSubject, InvokingSubject: envelopeHumanSubject, Metadata: copyEnvelope()}
	sub := human.Delegate("sub-1")
	sub.UserID = envelopeAgentID

	ctx := WithCallerContext(context.Background(), sub)
	if _, err := mw.Execute(ctx, delegationEnvelope["capability"], map[string]any{}); err != nil {
		t.Fatalf("unexpected error: %v", err)
	}

	if len(auditor.records) != 1 {
		t.Fatalf("expected 1 audit record, got %d", len(auditor.records))
	}
	rec := auditor.records[0]
	if rec.InvokingSubject != envelopeHumanSubject {
		t.Errorf("record InvokingSubject = %q, want %q", rec.InvokingSubject, envelopeHumanSubject)
	}
	if rec.ParentSpan != "sub-1" {
		t.Errorf("record ParentSpan = %q, want sub-1", rec.ParentSpan)
	}
	if rec.DelegationDepth != 1 {
		t.Errorf("record DelegationDepth = %d, want 1", rec.DelegationDepth)
	}
}
