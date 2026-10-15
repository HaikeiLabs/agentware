package mcproute

import (
	"errors"
	"fmt"
)

// Sentinel errors.
var (
	// ErrRouteNotLocal is returned when the resolved transport address is not a
	// loopback address or a Unix socket. The client never dials a remote host.
	ErrRouteNotLocal = errors.New("mcp route: the route host is not a loopback address")
	// ErrSessionEnded is returned on HTTP 401: the route key expired or was
	// revoked. The caller should not retry with the same key.
	ErrSessionEnded = errors.New("mcp route: session ended — the route key expired or was revoked")
	// ErrPolicyDenied is returned when the policy evaluator denies a tool call
	// (JSON-RPC code -31403).
	ErrPolicyDenied = errors.New("mcp route: tool call denied by policy")
	// ErrManifestDrift is returned when the connector manifest no longer matches
	// the approved listing (JSON-RPC code -31409).
	ErrManifestDrift = errors.New("mcp route: connector manifest drift detected")
	// ErrUpstream is returned when the upstream MCP server is unreachable or
	// returns an error (JSON-RPC code -31502).
	ErrUpstream = errors.New("mcp route: upstream MCP server error")
	// ErrHeaderMismatch is returned when the MCP headers on the request do not
	// match the JSON-RPC method (JSON-RPC code -32020).
	ErrHeaderMismatch = errors.New("mcp route: request header mismatch")
)

// JSON-RPC error codes from kei-connector-runtime/internal/mcproute/errors.go.
const (
	CodeParse           = -32700
	CodeInvalidRequest  = -32600
	CodeMethodNotFound  = -32601
	CodeInvalidParams   = -32602
	CodeInternal        = -32603
	CodeUnauthenticated = -31401
	CodeInvalidArgs     = -31400
	CodeDenied          = -31403
	CodeManifest        = -31409
	CodeCredential      = -31424
	CodeUpstream        = -31502
	CodeHeaderMismatch  = -32020
)

// RouteError is a structured JSON-RPC error from the kei-proxy MCP route.
// Every non-2xx or JSON-RPC error response is returned as a *RouteError.
// Callers should use errors.As to extract it.
type RouteError struct {
	// Code is the JSON-RPC error code (e.g. -31403 for policy denied).
	Code int
	// Message is the JSON-RPC error message (e.g. "policy_denied").
	Message string
	// Reason is the error.data.reason field, when present.
	Reason string
	// DecisionID is the error.data.decision_id, when present.
	DecisionID string
}

func (e *RouteError) Error() string {
	if e.Reason != "" {
		if e.DecisionID != "" {
			return fmt.Sprintf("mcp route error %d (%s): %s [%s]", e.Code, e.Message, e.Reason, e.DecisionID)
		}
		return fmt.Sprintf("mcp route error %d (%s): %s", e.Code, e.Message, e.Reason)
	}
	return fmt.Sprintf("mcp route error %d (%s)", e.Code, e.Message)
}

func (e *RouteError) Unwrap() error {
	switch e.Code {
	case CodeDenied:
		return ErrPolicyDenied
	case CodeManifest:
		return ErrManifestDrift
	case CodeUpstream:
		return ErrUpstream
	case CodeHeaderMismatch:
		return ErrHeaderMismatch
	case CodeUnauthenticated:
		return ErrSessionEnded
	default:
		return nil
	}
}
