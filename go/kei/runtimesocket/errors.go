package runtimesocket

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net"
	"strconv"

	"github.com/haikeilabs/agentware/go/kei/evaluator"
)

// Sentinel errors.
var (
	// ErrInvalidIdentity is a session identity with an empty source or
	// external id. It is a caller error, returned before any request.
	ErrInvalidIdentity = errors.New("a socket session needs a non-empty source and external_id")
	// ErrInvalidSession is a Session with an empty source, external id or
	// session id. It is a caller error, returned before any request.
	ErrInvalidSession = errors.New("socket calls need an explicit named session")
	// ErrInvalidSource is an empty session source for SelectAuthorizationClient.
	ErrInvalidSource = errors.New("source must be non-empty")
	// ErrUnsupportedPlatform is wrapped by every refusal on Windows.
	ErrUnsupportedPlatform = errors.New("the kei-proxy runtime socket is not supported on windows")
)

// Error is a daemon call that produced no usable answer. Callers fail closed.
//
// Class is one of proxy_unavailable, proxy_timeout, proxy_error,
// malformed_response or missing_token. Code is the daemon's error code
// ({"error": {"code": ...}}) when it sent one, e.g. invalid_runtime_token or
// session_not_found. Detail is built by this package and never contains a
// daemon message, a response body or the token.
type Error struct {
	Class  evaluator.ReasonClass
	Detail string
	Code   string

	// cause is a transport or context error kept for errors.Is; its text is
	// never part of Error().
	cause error
}

// Error reads like evaluator.AuthorizeError: "kei-proxy <class>: <detail>".
func (e *Error) Error() string {
	return fmt.Sprintf("kei-proxy %s: %s", e.Class, e.Detail)
}

// Unwrap returns the refusal as an *evaluator.AuthorizeError, so
// evaluator.KeiProxyEvaluator denies with this reason class.
func (e *Error) Unwrap() error {
	return &evaluator.AuthorizeError{Class: e.Class, Detail: e.Detail}
}

// Is reports whether the underlying transport or context error matches
// target, e.g. ErrUnsupportedPlatform or context.Canceled.
func (e *Error) Is(target error) bool {
	return e.cause != nil && errors.Is(e.cause, target)
}

func refusal(class evaluator.ReasonClass, detail string) *Error {
	return &Error{Class: class, Detail: detail}
}

func malformed(detail string) *Error {
	return refusal(evaluator.ReasonMalformedResponse, detail)
}

// transportError classifies a failed request. parent is the caller's context:
// when it ended, the refusal wraps its error rather than blaming the daemon.
func transportError(parent context.Context, err error, timedOut bool) *Error {
	var se *Error
	if errors.As(err, &se) {
		return se
	}
	if errors.Is(err, ErrUnsupportedPlatform) {
		return &Error{Class: evaluator.ReasonProxyUnavailable, Detail: ErrUnsupportedPlatform.Error(), cause: ErrUnsupportedPlatform}
	}
	if !timedOut {
		if ctxErr := parent.Err(); ctxErr != nil {
			class := evaluator.ReasonProxyUnavailable
			if errors.Is(ctxErr, context.DeadlineExceeded) {
				class = evaluator.ReasonProxyTimeout
			}
			return &Error{Class: class, Detail: "the caller's context ended before the daemon answered", cause: ctxErr}
		}
	}
	var ne net.Error
	if timedOut || errors.Is(err, context.DeadlineExceeded) || (errors.As(err, &ne) && ne.Timeout()) {
		return &Error{Class: evaluator.ReasonProxyTimeout, Detail: "the kei-proxy daemon did not answer in time", cause: err}
	}
	return &Error{Class: evaluator.ReasonProxyUnavailable, Detail: "the kei-proxy daemon is unreachable", cause: err}
}

// statusError is a non-2xx answer, carrying only the daemon's error code.
func statusError(status int, code string) *Error {
	what := fmt.Sprintf("the daemon answered %d", status)
	if status == 401 {
		what = "runtime authentication failed"
	}
	if code != "" {
		what += " (" + code + ")"
	}
	return &Error{Class: evaluator.ReasonProxyError, Detail: what, Code: code}
}

// errorCode extracts the daemon's {"error": {"code"}}, {"error": "code"} or
// {"code": ...}, or "".
func errorCode(body []byte) string {
	var parsed map[string]any
	if json.Unmarshal(body, &parsed) != nil {
		return ""
	}
	switch e := parsed["error"].(type) {
	case map[string]any:
		return scalar(e["code"])
	case string:
		return e
	}
	return scalar(parsed["code"])
}

// scalar renders a JSON code the way Python's str(x or "") would.
func scalar(v any) string {
	switch x := v.(type) {
	case string:
		return x
	case float64:
		if x == 0 {
			return ""
		}
		return strconv.FormatFloat(x, 'f', -1, 64)
	case bool:
		if x {
			return "True"
		}
	}
	return ""
}
