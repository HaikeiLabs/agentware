package middleware

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"time"

	"github.com/haikeilabs/agentware/go/tools"
)

type ToolExecutor interface {
	Execute(ctx context.Context, toolName string, args map[string]any) (*tools.Result, error)
}

// AuditHook receives completed audit records after they have been recorded.
type AuditHook interface {
	AfterExecution(record AuditRecord)
}

type Middleware interface {
	ToolExecutor
	WithPolicy(evaluator PolicyEvaluator) Middleware
	WithAuditor(auditor Auditor) Middleware
	AddHook(hook AuditHook) Middleware
}

type middlewareImpl struct {
	exec      ToolExecutor
	evaluator PolicyEvaluator
	auditor   Auditor
	hooks     []AuditHook
}

func NewMiddleware(exec ToolExecutor) Middleware {
	return &middlewareImpl{
		exec: exec,
	}
}

func (m *middlewareImpl) Execute(ctx context.Context, toolName string, args map[string]any) (*tools.Result, error) {
	caller := getCallerContext(ctx)

	var decision Decision
	if m.evaluator != nil {
		decision = m.evaluator.Evaluate(toolName, args, caller)
	} else {
		decision = Decision{Action: ActionAllow, Reason: "no policy configured"}
	}

	// Apply redaction before hashing or executing so that neither the audit
	// digest nor the tool ever observes the sensitive values. The caller's map
	// is copied rather than mutated in place.
	execArgs := args
	if decision.Action == ActionFilter && len(decision.RedactedArgs) > 0 {
		execArgs = make(map[string]any, len(args))
		for k, v := range args {
			execArgs[k] = v
		}
		for k, v := range decision.RedactedArgs {
			execArgs[k] = v
		}
	}

	argsBytes, _ := json.Marshal(execArgs)
	argsHash := sha256.Sum256(argsBytes)
	argsDigest := hex.EncodeToString(argsHash[:])

	auditRecord := AuditRecord{
		InvokedAt:       time.Now(),
		InvokingSubject: caller.InvokingSubject,
		ParentSpan:      caller.ParentSpan,
		DelegationDepth: caller.DelegationDepth,
		Framework:       FrameworkFromContext(ctx),
		ToolName:        toolName,
		ToolArgsDigest:  argsDigest,
		Decision:        string(decision.Action),
		PolicyID:        decision.Rule,
	}

	if usage, ok := TokenUsageFromContext(ctx); ok {
		auditRecord.TokensIn = usage.PromptTokens
		auditRecord.TokensOut = usage.CompletionTokens
		auditRecord.CachedTokens = usage.CachedTokens
	}

	if decision.Action == ActionDeny {
		auditRecord.LatencyMs = 0
		auditRecord.Error = "denied by policy: " + decision.Reason
		auditRecord.Success = false
		if m.auditor != nil {
			m.auditor.Record(auditRecord)
		}
		m.runHooks(auditRecord)
		return &tools.Result{
			Success: false,
			Error:   "denied by policy: " + decision.Reason,
		}, nil
	}

	start := time.Now()
	result, err := m.exec.Execute(ctx, toolName, execArgs)
	auditRecord.LatencyMs = int(time.Since(start).Milliseconds())
	if err != nil {
		auditRecord.Error = err.Error()
		auditRecord.Success = false
	} else if result != nil {
		auditRecord.Success = result.Success
		if !result.Success && result.Error != "" {
			auditRecord.Error = result.Error
		}
	} else {
		auditRecord.Success = false
		auditRecord.Error = "nil result with no error"
	}

	if m.auditor != nil {
		m.auditor.Record(auditRecord)
	}
	m.runHooks(auditRecord)

	return result, err
}

func (m *middlewareImpl) WithPolicy(evaluator PolicyEvaluator) Middleware {
	m.evaluator = evaluator
	return m
}

func (m *middlewareImpl) WithAuditor(auditor Auditor) Middleware {
	m.auditor = auditor
	return m
}

func (m *middlewareImpl) AddHook(hook AuditHook) Middleware {
	m.hooks = append(m.hooks, hook)
	return m
}

func (m *middlewareImpl) runHooks(record AuditRecord) {
	for _, hook := range m.hooks {
		hook.AfterExecution(record)
	}
}

func getCallerContext(ctx context.Context) CallerContext {
	if c, ok := ctx.Value(callerContextKey).(CallerContext); ok {
		return c
	}
	return CallerContext{
		Trusted: false,
	}
}

type contextKey string

const callerContextKey contextKey = "caller_context"

const tokenUsageKey contextKey = "token_usage"

const frameworkKey contextKey = "framework"

func WithCallerContext(ctx context.Context, caller CallerContext) context.Context {
	return context.WithValue(ctx, callerContextKey, caller)
}

// WithTokenUsage attaches LLM token usage for the current inference turn to
// ctx. The middleware records it on the AuditRecord after tool execution.
func WithTokenUsage(ctx context.Context, usage TokenUsage) context.Context {
	return context.WithValue(ctx, tokenUsageKey, usage)
}

// TokenUsageFromContext returns the TokenUsage attached to ctx, if any.
func TokenUsageFromContext(ctx context.Context) (TokenUsage, bool) {
	u, ok := ctx.Value(tokenUsageKey).(TokenUsage)
	return u, ok
}

// WithFramework attaches the calling agent framework identifier to ctx.
func WithFramework(ctx context.Context, framework string) context.Context {
	return context.WithValue(ctx, frameworkKey, framework)
}

// FrameworkFromContext returns the calling agent framework identifier, if any.
func FrameworkFromContext(ctx context.Context) string {
	framework, _ := ctx.Value(frameworkKey).(string)
	return framework
}

// CallerFromContext returns the CallerContext attached to ctx, if any.
// Downstream executors use this to scope work to the calling user.
func CallerFromContext(ctx context.Context) (CallerContext, bool) {
	c, ok := ctx.Value(callerContextKey).(CallerContext)
	return c, ok
}
