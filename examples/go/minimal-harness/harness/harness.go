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
	caller := middleware.CallerContext{
		InvokingSubject: userID,
		Trusted:         false,
	}
	decision := h.evaluator.Evaluate(toolName, args, caller)
	if decision.Action != middleware.ActionAllow {
		return nil, fmt.Errorf("denied by policy: %s", decision.Reason)
	}
	fn, ok := h.tools[toolName]
	if !ok {
		return nil, fmt.Errorf("unknown tool: %s", toolName)
	}
	return fn(ctx, args)
}
