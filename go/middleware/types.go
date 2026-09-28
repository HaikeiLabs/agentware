package middleware

import "time"

type Action string

const (
	ActionAllow  Action = "allow"
	ActionDeny   Action = "deny"
	ActionFilter Action = "filter"
)

// RedactedPlaceholder replaces the value of any argument redacted by a filter rule.
const RedactedPlaceholder = "[REDACTED]"

// CallerContext describes the caller making a tool call.
//
// InvokingSubject is the identity of the human who originated the request. It
// is set once at the human-facing entry point and carried unchanged across
// every delegation hop, so a tool call made by a subagent several levels deep
// still resolves back to a person rather than to the agent's own service
// identity. ParentSpan and DelegationDepth record where in the delegation
// chain the call was made.
//
// Trusted defaults to false (fail-closed): a caller that was never explicitly
// marked trusted is untrusted.
type CallerContext struct {
	UserID          string            `json:"user_id"`
	SessionID       string            `json:"session_id"`
	Role            string            `json:"role"`
	Source          string            `json:"source"`
	Trusted         bool              `json:"trusted"`
	Metadata        map[string]string `json:"metadata,omitempty"`
	InvokingSubject string            `json:"invoking_subject"`
	ParentSpan      string            `json:"parent_span,omitempty"`
	DelegationDepth int               `json:"delegation_depth"`
}

// Delegate returns the context a subagent spawned by this caller should run
// under. The child inherits InvokingSubject unchanged, takes parentSpan (or
// this context's own ParentSpan when empty) as its ParentSpan, and sits one
// level deeper in the delegation chain. The receiver is not modified.
//
// InvokingSubject is deliberately not overridable: overwriting it with the
// delegating agent's own identity is the exact attribution loss this field
// exists to prevent.
func (c CallerContext) Delegate(parentSpan string) CallerContext {
	if parentSpan == "" {
		parentSpan = c.ParentSpan
	}
	c.ParentSpan = parentSpan
	c.DelegationDepth++
	return c
}

type Decision struct {
	Action       Action
	Rule         string
	Reason       string
	RedactedArgs map[string]any
	// Enrollment is kei-proxy's opaque enrollment object on a DENY
	// (provider, provider_user_id, org_id, workspace_id and optionally
	// url/expires_at). The url is a one-time claim link: surface it to the
	// user, never log it. Nil when absent.
	Enrollment map[string]any
	Timestamp  time.Time
}
