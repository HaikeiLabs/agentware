package harness

import (
	"context"
	"fmt"

	"github.com/haikeilabs/agentware/go/kei/evaluator"
	"github.com/haikeilabs/agentware/go/middleware"
)

// Harness is a minimal governed tool harness. Trusted defaults to false
// (fail-closed): every authorization decision is evaluated with an untrusted
// caller context unless overridden.
type Harness struct {
	evaluator *evaluator.KeiProxyEvaluator
	tools     map[string]func(ctx context.Context, args map[string]any) (map[string]any, error)
}

// New creates a Harness that evaluates policy through client.
func New(client evaluator.Client, source string) *Harness {
	return &Harness{
		evaluator: evaluator.NewKeiProxyEvaluator(client),
		tools:     make(map[string]func(ctx context.Context, args map[string]any) (map[string]any, error)),
	}
}

// Register adds a named tool.
func (h *Harness) Register(name string, fn func(ctx context.Context, args map[string]any) (map[string]any, error)) {
	h.tools[name] = fn
}

// Execute authorizes toolName for userID and, if allowed, runs the registered
// tool. Trusted is false by default — a missing or unknown decision denies.
func (h *Harness) Execute(ctx context.Context, userID, toolName string, args map[string]any) (map[string]any, error) {
	result, _, err := h.ExecuteWithDecision(ctx, userID, toolName, args)
	return result, err
}

// ExecuteWithDecision authorizes toolName and returns the policy decision even
// when denied, so callers can display safe reason metadata. A denied call never
// invokes the registered handler.
func (h *Harness) ExecuteWithDecision(ctx context.Context, userID, toolName string, args map[string]any) (map[string]any, middleware.Decision, error) {
	caller := middleware.CallerContext{
		InvokingSubject: userID,
		Trusted:         false,
	}
	decision := h.evaluator.Evaluate(toolName, args, caller)
	if decision.Action != middleware.ActionAllow {
		return nil, decision, fmt.Errorf("denied by policy: %s", decision.Reason)
	}
	fn, ok := h.tools[toolName]
	if !ok {
		return nil, decision, fmt.Errorf("unknown tool: %s", toolName)
	}
	result, err := fn(ctx, args)
	return result, decision, err
}
