// Package evaluator is a middleware.PolicyEvaluator backed by
// `kei-proxy authorize`.
//
// It is the Go port of pedro_agentware.kei.evaluator. All three SDK languages
// are held to the parity table in fixtures/kei/authorize-cases.v1.json (see
// docs/kei-proxy-evaluator-parity.md): the same proxy output yields the same
// Decision, reason class and enrollment everywhere.
//
// Fail closed: only an explicit allow/permit with exit 0 allows. Proxy stdout
// can carry a credential or an enrollment claim link and stderr is free-form,
// so neither is ever logged or copied into a reason.
package evaluator

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"strings"
	"time"

	"github.com/soypete/pedro-agentware/go/middleware"
)

// ReasonClass classifies every Decision the evaluator returns. A reason reads
// "kei-proxy <class>" or "kei-proxy <class>: <detail>".
type ReasonClass string

// Reason classes, shared with the Python and TypeScript ports.
const (
	ReasonAllow              ReasonClass = "allow"
	ReasonDeny               ReasonClass = "deny"
	ReasonEnrollmentRequired ReasonClass = "enrollment_required"
	ReasonUnknownDecision    ReasonClass = "unknown_decision"
	ReasonNoDecision         ReasonClass = "no_decision"
	ReasonMalformedResponse  ReasonClass = "malformed_response"
	ReasonEmptyResponse      ReasonClass = "empty_response"
	ReasonProxyError         ReasonClass = "proxy_error"
	ReasonExitMismatch       ReasonClass = "exit_mismatch"
	ReasonProxyUnavailable   ReasonClass = "proxy_unavailable"
	ReasonProxyTimeout       ReasonClass = "proxy_timeout"
	ReasonMissingToken       ReasonClass = "missing_token"
	ReasonPinMismatch        ReasonClass = "pin_mismatch"
)

// ReasonClasses lists every ReasonClass in contract order.
var ReasonClasses = []ReasonClass{
	ReasonAllow, ReasonDeny, ReasonEnrollmentRequired, ReasonUnknownDecision,
	ReasonNoDecision, ReasonMalformedResponse, ReasonEmptyResponse, ReasonProxyError,
	ReasonExitMismatch, ReasonProxyUnavailable, ReasonProxyTimeout, ReasonMissingToken,
	ReasonPinMismatch,
}

// Rule is the Decision rule when kei-proxy supplies no policy id.
const Rule = "kei-proxy"

// isAffirmative reports whether a normalized decision allows. kei-proxy says
// "allow"; older proxies say "permit". Everything else denies.
func isAffirmative(decision string) bool {
	return decision == "allow" || decision == "permit"
}

// AuthorizeError is an authorize call that produced no usable answer. Detail
// is built by this package and never contains proxy stdout or stderr.
type AuthorizeError struct {
	Class  ReasonClass
	Detail string
}

func (e *AuthorizeError) Error() string {
	return fmt.Sprintf("kei-proxy %s: %s", e.Class, e.Detail)
}

// AuthorizeRequest is the canonical audit context sent with one call.
type AuthorizeRequest struct {
	UserID          string
	Tool            string
	Action          string
	Resource        string
	SpanID          string
	InvokingSubject string
	ParentSpan      string
	DelegationDepth int
	AgentID         string
	AgentVersion    string
	Framework       string
	ToolArgsDigest  string
	Resources       []string
	// WorkspaceID is accepted for parity; kei-proxy derives workspace scope
	// from the runtime token.
	WorkspaceID string
}

// Client is the single call the evaluator needs. Return the parsed authorize
// object, or an error (an *AuthorizeError keeps its class) to deny.
type Client interface {
	Authorize(ctx context.Context, req AuthorizeRequest) (map[string]any, error)
}

// KeiProxyEvaluator is a middleware.PolicyEvaluator over kei-proxy.
type KeiProxyEvaluator struct {
	client        Client
	defaultAction string
	logger        *slog.Logger
}

var _ middleware.PolicyEvaluator = (*KeiProxyEvaluator)(nil)

// Option configures a KeiProxyEvaluator.
type Option func(*KeiProxyEvaluator)

// WithDefaultAction sets the action verb sent when a tool call names none in
// args["action"]. Default "execute".
func WithDefaultAction(action string) Option {
	return func(e *KeiProxyEvaluator) { e.defaultAction = action }
}

// WithLogger sets the logger for class-level diagnostics. Default slog.Default().
func WithLogger(logger *slog.Logger) Option {
	return func(e *KeiProxyEvaluator) { e.logger = logger }
}

// NewKeiProxyEvaluator builds an evaluator over client, typically a *CLIClient.
func NewKeiProxyEvaluator(client Client, opts ...Option) *KeiProxyEvaluator {
	e := &KeiProxyEvaluator{client: client, defaultAction: "execute", logger: slog.Default()}
	for _, opt := range opts {
		opt(e)
	}
	return e
}

// Evaluate authorizes toolName via kei-proxy and translates the answer.
//
// span_id, agent_id, agent_version, framework and workspace_id are read from
// caller.Metadata; framework falls back to caller.Source.
func (e *KeiProxyEvaluator) Evaluate(toolName string, args map[string]any, caller middleware.CallerContext) middleware.Decision {
	resources := ResourcesTouched(toolName, args)
	resource := toolName
	if len(resources) > 0 {
		resource = resources[0]
	}
	userID := caller.InvokingSubject
	if userID == "" {
		userID = caller.UserID
	}
	action := e.defaultAction
	if a, ok := args["action"]; ok && a != nil && fmt.Sprint(a) != "" {
		action = fmt.Sprint(a)
	}
	framework := caller.Metadata["framework"]
	if framework == "" {
		framework = caller.Source
	}
	req := AuthorizeRequest{
		UserID:          userID,
		Tool:            toolName,
		Action:          action,
		Resource:        resource,
		SpanID:          caller.Metadata["span_id"],
		InvokingSubject: userID,
		ParentSpan:      caller.ParentSpan,
		DelegationDepth: caller.DelegationDepth,
		AgentID:         caller.Metadata["agent_id"],
		AgentVersion:    caller.Metadata["agent_version"],
		Framework:       framework,
		ToolArgsDigest:  ToolArgsDigest(args),
		Resources:       resources,
		WorkspaceID:     caller.Metadata["workspace_id"],
	}

	response, err := e.client.Authorize(context.Background(), req)
	if err != nil {
		var ae *AuthorizeError
		if errors.As(err, &ae) {
			e.logger.Warn("kei-proxy authorize denied", "tool", toolName, "class", string(ae.Class))
			return decision(middleware.ActionDeny, reason(ae.Class, ae.Detail), "", nil)
		}
		e.logger.Warn("kei-proxy authorize failed, denying", "tool", toolName, "error", err)
		return decision(middleware.ActionDeny, reason(ReasonProxyError, "authorize failed: "+err.Error()), "", nil)
	}

	normalized := ""
	if d, ok := response["decision"]; ok && d != nil {
		normalized = strings.ToLower(strings.TrimSpace(stringify(d)))
	}
	proxyReason := optionalString(response["reason"])
	policyID := optionalString(response["policy_id"])
	if policyID == "" {
		policyID = optionalString(response["policy"])
	}

	if isAffirmative(normalized) {
		return decision(middleware.ActionAllow, reason(ReasonAllow, proxyReason), policyID, nil)
	}

	carried, _ := response["enrollment"].(map[string]any)
	var (
		class      ReasonClass
		detail     string
		enrollment map[string]any
	)
	switch normalized {
	case "":
		class = ReasonNoDecision
	case "deny":
		class, detail, enrollment = ReasonDeny, proxyReason, carried
	case "enrollment_required":
		class, detail, enrollment = ReasonEnrollmentRequired, proxyReason, carried
		if detail == "" {
			detail = "enrollment is required before this call"
		}
	default:
		class = ReasonUnknownDecision
		detail = "'" + normalized + "'"
		if proxyReason != "" {
			detail += " (" + proxyReason + ")"
		}
	}

	e.logger.Debug("kei-proxy denied", "tool", toolName, "class", string(class))
	return decision(middleware.ActionDeny, reason(class, detail), policyID, enrollment)
}

func decision(action middleware.Action, reasonText, policyID string, enrollment map[string]any) middleware.Decision {
	rule := policyID
	if rule == "" {
		rule = Rule
	}
	return middleware.Decision{
		Action:     action,
		Rule:       rule,
		Reason:     reasonText,
		Enrollment: enrollment,
		Timestamp:  time.Now(),
	}
}

func reason(class ReasonClass, detail string) string {
	if detail == "" {
		return "kei-proxy " + string(class)
	}
	return "kei-proxy " + string(class) + ": " + detail
}

func optionalString(v any) string {
	if v == nil {
		return ""
	}
	return stringify(v)
}

// stringify renders a decoded JSON scalar the way Python's str() would for
// the values kei-proxy emits: integral floats without a fraction.
func stringify(v any) string {
	if f, ok := v.(float64); ok && f == float64(int64(f)) {
		return fmt.Sprint(int64(f))
	}
	return fmt.Sprint(v)
}
