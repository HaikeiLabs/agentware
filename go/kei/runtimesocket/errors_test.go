package runtimesocket

import (
	"context"
	"errors"
	"fmt"
	"strings"
	"testing"

	"github.com/haikeilabs/agentware/go/kei/evaluator"
	"github.com/haikeilabs/agentware/go/middleware"
)

const testToken = "kei_rt_unit-canary-0123456789abcdef"

func TestErrorReadsLikeAuthorizeErrorAndUnwrapsToIt(t *testing.T) {
	err := fmt.Errorf("call: %w", &Error{Class: evaluator.ReasonProxyError, Detail: "the daemon answered 500 (internal)", Code: "internal"})
	if got, want := errors.Unwrap(err).Error(), "kei-proxy proxy_error: the daemon answered 500 (internal)"; got != want {
		t.Errorf("Error() = %q, want %q", got, want)
	}
	var ae *evaluator.AuthorizeError
	if !errors.As(err, &ae) {
		t.Fatalf("errors.As(*evaluator.AuthorizeError) failed for %v", err)
	}
	if ae.Class != evaluator.ReasonProxyError || ae.Detail != "the daemon answered 500 (internal)" {
		t.Errorf("unwrapped to %+v", ae)
	}
}

type stubClient struct{ err error }

func (s stubClient) Authorize(context.Context, evaluator.AuthorizeRequest) (map[string]any, error) {
	return nil, s.err
}

func TestEvaluatorDeniesASocketErrorWithItsClass(t *testing.T) {
	for _, class := range []evaluator.ReasonClass{
		evaluator.ReasonProxyUnavailable, evaluator.ReasonProxyTimeout, evaluator.ReasonProxyError,
		evaluator.ReasonMalformedResponse, evaluator.ReasonMissingToken,
	} {
		e := evaluator.NewKeiProxyEvaluator(stubClient{err: &Error{Class: class, Detail: "d"}})
		d := e.Evaluate("github.get_issue", nil, middleware.CallerContext{UserID: "ada"})
		if d.Action != middleware.ActionDeny || d.Reason != "kei-proxy "+string(class)+": d" {
			t.Errorf("%s: got %s %q", class, d.Action, d.Reason)
		}
	}
}

func TestUnsupportedPlatformIsAnUnavailableRefusal(t *testing.T) {
	err := transportError(context.Background(), fmt.Errorf("dial: %w", ErrUnsupportedPlatform), false, "/run/kei-proxy.sock")
	if err.Class != evaluator.ReasonProxyUnavailable || !errors.Is(err, ErrUnsupportedPlatform) {
		t.Errorf("got %v (class %s)", err, err.Class)
	}
	var ae *evaluator.AuthorizeError
	if !errors.As(err, &ae) || ae.Class != evaluator.ReasonProxyUnavailable {
		t.Errorf("does not unwrap to a proxy_unavailable AuthorizeError: %v", err)
	}
}

func TestCallerCancellationWrapsTheContextError(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	err := transportError(ctx, context.Canceled, false, "/run/kei-proxy.sock")
	if err.Class != evaluator.ReasonProxyUnavailable || !errors.Is(err, context.Canceled) {
		t.Errorf("got %v", err)
	}
}

func TestErrorCodeShapes(t *testing.T) {
	cases := map[string]string{
		`{"error":{"code":"invalid_runtime_token","message":"x"}}`: "invalid_runtime_token",
		`{"error":"session_not_found"}`:                            "session_not_found",
		`{"code":"internal"}`:                                      "internal",
		`{"error":{"message":"no code"}}`:                          "",
		`not json`:                                                 "",
		`["error"]`:                                                "",
	}
	for body, want := range cases {
		if got := errorCode([]byte(body)); got != want {
			t.Errorf("errorCode(%s) = %q, want %q", body, got, want)
		}
	}
}

func TestStatusErrorDetail(t *testing.T) {
	if got := statusError(401, "invalid_runtime_token").Detail; got != "runtime authentication failed (invalid_runtime_token)" {
		t.Errorf("401 detail = %q", got)
	}
	if got := statusError(502, "").Detail; got != "the daemon answered 502" {
		t.Errorf("502 detail = %q", got)
	}
}

func TestStringShowsOnlyTheSocketPath(t *testing.T) {
	c := &Client{SocketPath: "/run/x.sock", Env: map[string]string{evaluator.RuntimeTokenEnv: testToken}}
	a := &AuthorizationClient{Source: "openwebui", Socket: c}
	for _, s := range []string{c.String(), fmt.Sprintf("%v", c), fmt.Sprintf("%#v", c), fmt.Sprintf("%+v", a)} {
		if strings.Contains(s, testToken) || !strings.Contains(s, "/run/x.sock") {
			t.Errorf("repr %q", s)
		}
	}
}

func TestSocketPathResolution(t *testing.T) {
	cases := []struct {
		name string
		c    *Client
		want string
	}{
		{"explicit wins", &Client{SocketPath: "/a.sock", Env: map[string]string{SocketPathEnv: "/b.sock"}}, "/a.sock"},
		{"environment", &Client{Env: map[string]string{SocketPathEnv: "/b.sock"}}, "/b.sock"},
		{"default", &Client{Env: map[string]string{}}, DefaultSocketPath},
	}
	for _, tc := range cases {
		if got := tc.c.ResolvedSocketPath(); got != tc.want {
			t.Errorf("%s: %q, want %q", tc.name, got, tc.want)
		}
	}
	t.Setenv(SocketPathEnv, "/proc-env.sock")
	if got := (&Client{}).ResolvedSocketPath(); got != "/proc-env.sock" {
		t.Errorf("nil Env does not read the process environment: %q", got)
	}
}

func TestSelectAuthorizationClient(t *testing.T) {
	t.Setenv(SocketPathEnv, "")
	if _, err := SelectAuthorizationClient("  ", SelectOptions{}); !errors.Is(err, ErrInvalidSource) {
		t.Errorf("empty source: %v", err)
	}

	explicit, err := SelectAuthorizationClient("openwebui", SelectOptions{SocketPath: "/a.sock", Env: map[string]string{}})
	if ac, ok := explicit.(*AuthorizationClient); err != nil || !ok || ac.Socket.ResolvedSocketPath() != "/a.sock" || ac.Source != "openwebui" {
		t.Errorf("explicit socket: %#v, %v", explicit, err)
	}

	fromEnv, _ := SelectAuthorizationClient("openwebui", SelectOptions{Env: map[string]string{SocketPathEnv: "/b.sock"}})
	if ac, ok := fromEnv.(*AuthorizationClient); !ok || ac.Socket.ResolvedSocketPath() != "/b.sock" {
		t.Errorf("socket from env: %#v", fromEnv)
	}

	cli, _ := SelectAuthorizationClient("openwebui", SelectOptions{Env: map[string]string{}, Executable: "/opt/kei-proxy"})
	if c, ok := cli.(*evaluator.CLIClient); !ok || c.Executable != "/opt/kei-proxy" {
		t.Errorf("no socket: %#v", cli)
	}

	t.Setenv(SocketPathEnv, "/proc.sock")
	if _, ok := mustSelect(t, SelectOptions{}).(*AuthorizationClient); !ok {
		t.Error("nil Env does not read KEI_RUNTIME_SOCKET_PATH from the process environment")
	}
}

func mustSelect(t *testing.T, opts SelectOptions) evaluator.Client {
	t.Helper()
	c, err := SelectAuthorizationClient("openwebui", opts)
	if err != nil {
		t.Fatalf("select: %v", err)
	}
	return c
}

func TestCallerErrorsComeBeforeAnyRequest(t *testing.T) {
	c := &Client{SocketPath: "/nonexistent/kei.sock", Env: map[string]string{}}
	if _, err := c.OpenSession(context.Background(), Identity{Source: "s", ExternalID: " "}); !errors.Is(err, ErrInvalidIdentity) {
		t.Errorf("open: %v", err)
	}
	if _, err := c.Authorize(context.Background(), Session{Source: "s", ExternalID: "e"}, evaluator.AuthorizeRequest{Tool: "t"}); !errors.Is(err, ErrInvalidSession) {
		t.Errorf("authorize: %v", err)
	}
	a := &AuthorizationClient{Source: "s", Socket: c}
	var se *Error
	if _, err := a.Authorize(context.Background(), evaluator.AuthorizeRequest{Tool: "t"}); !errors.As(err, &se) || se.Class != evaluator.ReasonProxyError {
		t.Errorf("no subject: %v", err)
	}
}
