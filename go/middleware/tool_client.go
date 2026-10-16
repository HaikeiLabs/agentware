package middleware

import (
	"context"

	"github.com/haikeilabs/agentware/go/tools"
)

// ExecutorFunc adapts a plain function to the ToolExecutor interface so
// callers can hand AuditedToolClient a closure instead of defining a type.
type ExecutorFunc func(ctx context.Context, toolName string, args map[string]any) (*tools.Result, error)

// Execute implements ToolExecutor.
func (f ExecutorFunc) Execute(ctx context.Context, toolName string, args map[string]any) (*tools.Result, error) {
	return f(ctx, toolName, args)
}

// AuditedToolClient wraps a tool executor so that every call is evaluated
// against policy before execution and recorded to the audit log afterwards.
//
// Policy evaluation and audit recording are performed by the middleware this
// client builds internally; the client's job is to give callers a small,
// dependency-free surface (a function in, a Result out) rather than requiring
// them to assemble a Middleware, a PolicyEvaluator and an Auditor themselves.
type AuditedToolClient struct {
	// source identifies the middleware this client reports to. It is recorded
	// on every AuditRecord as the framework field so audit consumers can tell
	// which client produced a record.
	source     string
	middleware Middleware
	auditor    Auditor
}

// NewAuditedToolClient returns a client that runs executor behind policy
// evaluation and audit logging.
//
// source identifies this client in the audit trail. Policy evaluation and
// auditing are in-process: the middleware package holds no network client, so
// source is recorded as the framework on each audit record rather than dialed.
// Callers wanting a remote decision point should supply a PolicyEvaluator that
// performs the call, via WithPolicy.
//
// A nil executor is allowed; calls then fail with a non-nil Result whose Error
// is set, and are still audited.
func NewAuditedToolClient(source string, executor func(ctx context.Context, toolName string, args map[string]any) (*tools.Result, error)) *AuditedToolClient {
	auditor := NewInMemoryAuditor()

	var exec ToolExecutor
	if executor != nil {
		exec = ExecutorFunc(executor)
	} else {
		exec = ExecutorFunc(func(context.Context, string, map[string]any) (*tools.Result, error) {
			return nil, ErrNoExecutor
		})
	}

	return &AuditedToolClient{
		source:     source,
		middleware: NewMiddleware(exec).WithAuditor(auditor),
		auditor:    auditor,
	}
}

// WithPolicy sets the policy evaluator consulted before each execution.
// Without one the underlying middleware allows every call. It returns the
// client so construction can be chained.
func (c *AuditedToolClient) WithPolicy(evaluator PolicyEvaluator) *AuditedToolClient {
	c.middleware.WithPolicy(evaluator)
	return c
}

// WithAuditor replaces the default in-memory auditor. Records written before
// the swap remain only in the previous auditor.
func (c *AuditedToolClient) WithAuditor(auditor Auditor) *AuditedToolClient {
	c.auditor = auditor
	c.middleware.WithAuditor(auditor)
	return c
}

// AddHook registers a hook invoked with each completed audit record.
func (c *AuditedToolClient) AddHook(hook AuditHook) *AuditedToolClient {
	c.middleware.AddHook(hook)
	return c
}

// Execute authorizes toolName against policy, runs the wrapped executor when
// the call is allowed, and records an audit entry either way.
//
// A call denied by policy returns a Result with Success false and a nil error,
// matching the middleware's contract: policy denial is an outcome, not a
// transport failure. Errors from the executor are returned to the caller after
// being recorded.
func (c *AuditedToolClient) Execute(ctx context.Context, toolName string, args map[string]any) (*tools.Result, error) {
	ctx = WithFramework(ctx, c.source)
	return c.middleware.Execute(ctx, toolName, args)
}

// Records returns the audit records captured by this client's auditor,
// filtered by filter. It returns nil when the auditor does not support
// querying.
func (c *AuditedToolClient) Records(filter AuditFilter) []AuditRecord {
	if c.auditor == nil {
		return nil
	}
	return c.auditor.Query(filter)
}

// ErrNoExecutor is returned when a client is constructed without an executor.
var ErrNoExecutor = &tools.ToolError{
	Code:    "NO_EXECUTOR",
	Message: "audited tool client has no executor configured",
}
