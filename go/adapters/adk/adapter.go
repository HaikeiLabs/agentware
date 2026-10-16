package adk

import (
	"context"
	"time"

	"github.com/haikeilabs/agentware/go/middleware"
	"github.com/haikeilabs/agentware/go/tools"
)

type ToolDefinition struct {
	Name        string
	Description string
	Parameters  map[string]any
}

type BeforeToolCallback func(toolName string, args map[string]any) error
type AfterToolCallback func(toolName string, args map[string]any, result *tools.Result, err error)

type ADKToolAdapter struct {
	middleware      middleware.Middleware
	beforeCallbacks []BeforeToolCallback
	afterCallbacks  []AfterToolCallback
	toolDefinitions []ToolDefinition
}

func NewAdapter(m middleware.Middleware) *ADKToolAdapter {
	return &ADKToolAdapter{
		middleware:      m,
		beforeCallbacks: []BeforeToolCallback{},
		afterCallbacks:  []AfterToolCallback{},
		toolDefinitions: []ToolDefinition{},
	}
}

func (a *ADKToolAdapter) BeforeToolCallback(toolName string, args map[string]any) error {
	for _, cb := range a.beforeCallbacks {
		if err := cb(toolName, args); err != nil {
			return err
		}
	}
	if a.middleware != nil {
		// Fail-closed: a caller not present in the context is untrusted.
		caller := middleware.CallerContext{
			Trusted: false,
		}
		ctx := middleware.WithCallerContext(context.Background(), caller)
		ctx = middleware.WithFramework(ctx, "adk")
		_, err := a.middleware.Execute(ctx, toolName, args)
		if err != nil {
			return err
		}
	}
	return nil
}

func (a *ADKToolAdapter) AfterToolCallback(toolName string, args map[string]any, result *tools.Result, err error) {
	for _, cb := range a.afterCallbacks {
		cb(toolName, args, result, err)
	}
}

func (a *ADKToolAdapter) Execute(ctx context.Context, toolName string, args map[string]any) (*tools.Result, error) {
	ctx = middleware.WithFramework(ctx, "adk")

	if err := a.BeforeToolCallback(toolName, args); err != nil {
		return &tools.Result{
			Success: false,
			Error:   err.Error(),
		}, nil
	}

	var result *tools.Result
	var execErr error

	if a.middleware != nil {
		result, execErr = a.middleware.Execute(ctx, toolName, args)
	} else {
		execErr = ErrNoExecutor
		result = &tools.Result{
			Success: false,
			Error:   "no middleware configured",
		}
	}

	a.AfterToolCallback(toolName, args, result, execErr)

	return result, execErr
}

func (a *ADKToolAdapter) ListTools() []ToolDefinition {
	return a.toolDefinitions
}

func (a *ADKToolAdapter) RegisterTool(tool ToolDefinition) {
	a.toolDefinitions = append(a.toolDefinitions, tool)
}

func (a *ADKToolAdapter) RegisterBeforeCallback(cb BeforeToolCallback) {
	a.beforeCallbacks = append(a.beforeCallbacks, cb)
}

func (a *ADKToolAdapter) RegisterAfterCallback(cb AfterToolCallback) {
	a.afterCallbacks = append(a.afterCallbacks, cb)
}

func (a *ADKToolAdapter) GetMiddleware() middleware.Middleware {
	return a.middleware
}

func (a *ADKToolAdapter) WithPolicy(evaluator middleware.PolicyEvaluator) *ADKToolAdapter {
	if a.middleware != nil {
		a.middleware = a.middleware.WithPolicy(evaluator)
	}
	return a
}

func (a *ADKToolAdapter) WithAuditor(auditor middleware.Auditor) *ADKToolAdapter {
	if a.middleware != nil {
		a.middleware = a.middleware.WithAuditor(auditor)
	}
	return a
}

var ErrNoExecutor = &AdapterError{Message: "no executor configured"}

type AdapterError struct {
	Message string
}

func (e *AdapterError) Error() string {
	return e.Message
}

type auditMiddlewareAdapter struct {
	ADKToolAdapter
}

func NewAuditAdapter(m middleware.Middleware, auditor middleware.Auditor) *ADKToolAdapter {
	adapter := NewAdapter(m)
	adapter.RegisterAfterCallback(func(toolName string, args map[string]any, result *tools.Result, err error) {
		if auditor != nil {
			// The after-callback carries no context, so no caller identity is
			// attributable here; the middleware records the authoritative
			// record with the full delegation chain. This fallback row exists
			// only to surface outcome fields (success, error) when the audit
			// record did not already capture them. It is never trusted.
			auditor.Record(middleware.AuditRecord{
				InvokedAt: time.Now(),
				Framework: "adk",
				ToolName:  toolName,
				Success:   result.Success,
				Error:     result.Error,
			})
		}
	})
	return adapter
}
