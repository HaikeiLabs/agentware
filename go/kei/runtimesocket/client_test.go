//go:build !windows

package runtimesocket

import (
	"bytes"
	"context"
	"errors"
	"log/slog"
	"net/http"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/haikeilabs/agentware/go/kei/evaluator"
	"github.com/haikeilabs/agentware/go/middleware"
)

func tokenEnv(path string) map[string]string {
	return map[string]string{evaluator.RuntimeTokenEnv: testToken, SocketPathEnv: path}
}

var (
	sessionAnswer = jsonResponse(201, map[string]any{"schema": SessionSchema, "session_id": "sess-1"})
	permitAnswer  = jsonResponse(200, map[string]any{"schema": AuthorizeResponseSchema, "decision": "permit", "policy_id": "p-allow"})
)

func TestEvaluatorOverTheSocket(t *testing.T) {
	caller := middleware.CallerContext{UserID: "ada", Metadata: map[string]string{"agent_id": "agent-1"}}

	t.Run("permit allows and the session is reused", func(t *testing.T) {
		d := startDaemon(t, sessionAnswer, permitAnswer, permitAnswer)
		client, err := SelectAuthorizationClient("openwebui", SelectOptions{Env: tokenEnv(d.path)})
		if err != nil {
			t.Fatal(err)
		}
		e := evaluator.NewKeiProxyEvaluator(client)
		for i := 0; i < 2; i++ {
			if got := e.Evaluate("github.get_issue", nil, caller); got.Action != middleware.ActionAllow || got.Rule != "p-allow" {
				t.Fatalf("call %d: %s %q", i, got.Action, got.Reason)
			}
		}
		reqs := d.requests()
		if len(reqs) != 3 || reqs[0].Method != http.MethodPut || reqs[1].Session != "sess-1" || reqs[2].Session != "sess-1" {
			t.Errorf("requests %+v", reqs)
		}
		body, _ := reqs[1].Body.(map[string]any)
		for _, k := range []string{"user_id", "invoking_subject", "workspace_id", "resources", "agent_version", "agent_id"} {
			if _, ok := body[k]; ok {
				t.Errorf("authorize body names %q: %v", k, body)
			}
		}
	})

	t.Run("deny denies with its enrollment", func(t *testing.T) {
		d := startDaemon(t, sessionAnswer, jsonResponse(200, map[string]any{
			"schema": AuthorizeResponseSchema, "decision": "deny", "reason": "unlinked",
			"reason_code": "subject_missing", "hint": "Link your account in Kei.",
			"enrollment": map[string]any{"claim_url": "https://kei.example.test/claim/1"},
		}))
		client, _ := SelectAuthorizationClient("openwebui", SelectOptions{Env: tokenEnv(d.path)})
		got := evaluator.NewKeiProxyEvaluator(client).Evaluate("github.get_issue", nil, caller)
		if got.Action != middleware.ActionDeny || got.Reason != "kei-proxy deny: unlinked" || got.Enrollment == nil || got.ReasonCode != "subject_missing" || got.Hint != "Link your account in Kei." {
			t.Errorf("got %s %q %v", got.Action, got.Reason, got.Enrollment)
		}
	})

	t.Run("a daemon refusal denies without leaking the token", func(t *testing.T) {
		d := startDaemon(t, jsonResponse(401, map[string]any{
			"error": map[string]any{"code": "invalid_runtime_token", "message": "bad token " + testToken},
		}))
		client, _ := SelectAuthorizationClient("openwebui", SelectOptions{Env: tokenEnv(d.path)})
		got := evaluator.NewKeiProxyEvaluator(client).Evaluate("github.get_issue", nil, caller)
		if got.Action != middleware.ActionDeny || got.Reason != "kei-proxy proxy_error: runtime authentication failed (invalid_runtime_token)" {
			t.Errorf("got %s %q", got.Action, got.Reason)
		}
	})

	t.Run("a socket that is down denies and never falls back", func(t *testing.T) {
		var logs bytes.Buffer
		client, _ := SelectAuthorizationClient("openwebui", SelectOptions{Env: tokenEnv(shortSocketPath(t))})
		e := evaluator.NewKeiProxyEvaluator(client, evaluator.WithLogger(slog.New(slog.NewTextHandler(&logs, nil))))
		got := e.Evaluate("github.get_issue", nil, caller)
		if got.Action != middleware.ActionDeny || !strings.HasPrefix(got.Reason, "kei-proxy proxy_unavailable") {
			t.Errorf("got %s %q", got.Action, got.Reason)
		}
		if strings.Contains(got.Reason, testToken) || strings.Contains(logs.String(), testToken) {
			t.Error("the token leaked into the decision or the log")
		}
	})

	t.Run("no subject denies", func(t *testing.T) {
		client, _ := SelectAuthorizationClient("openwebui", SelectOptions{Env: tokenEnv(shortSocketPath(t))})
		got := evaluator.NewKeiProxyEvaluator(client).Evaluate("t", nil, middleware.CallerContext{})
		if got.Action != middleware.ActionDeny || got.Reason != "kei-proxy proxy_error: no invoking subject to open a session for" {
			t.Errorf("got %s %q", got.Action, got.Reason)
		}
	})
}

func TestUnavailableSocketErrorIncludesPathAndInstallCommand(t *testing.T) {
	const socketPath = "/run/custom/kei-proxy.sock"
	client, err := SelectAuthorizationClient("openwebui", SelectOptions{
		SocketPath: socketPath,
		Env:        tokenEnv(socketPath),
	})
	if err != nil {
		t.Fatal(err)
	}
	decision := evaluator.NewKeiProxyEvaluator(client).Evaluate(
		"github.get_issue", nil, middleware.CallerContext{UserID: "ada"},
	)
	if !strings.Contains(decision.Reason, socketPath) || !strings.Contains(decision.Reason, "--proxy-only") {
		t.Errorf("reason %q must show the socket path and proxy-only install command", decision.Reason)
	}
}

func TestTheTokenIsReadOnEveryRequest(t *testing.T) {
	d := startDaemon(t, permitAnswer, permitAnswer)
	env := tokenEnv(d.path)
	c := &Client{Env: env}
	s := Session{Source: "s", ExternalID: "e", SessionID: "sess-1"}
	if _, err := c.Authorize(context.Background(), s, evaluator.AuthorizeRequest{Tool: "t"}); err != nil {
		t.Fatal(err)
	}
	env[evaluator.RuntimeTokenEnv] = "rotated"
	if _, err := c.Authorize(context.Background(), s, evaluator.AuthorizeRequest{Tool: "t"}); err != nil {
		t.Fatal(err)
	}
	reqs := d.requests()
	if reqs[0].Authorization != "Bearer "+testToken || reqs[1].Authorization != "Bearer rotated" {
		t.Errorf("authorization headers %q, %q", reqs[0].Authorization, reqs[1].Authorization)
	}
}

func TestAmbientProxyIsIgnored(t *testing.T) {
	t.Setenv("HTTP_PROXY", "http://127.0.0.1:1")
	t.Setenv("http_proxy", "http://127.0.0.1:1")
	d := startDaemon(t, jsonResponse(200, map[string]any{"schema": PolicyBundleStatusSchema}))
	if _, err := (&Client{Env: tokenEnv(d.path)}).PolicyBundleStatus(context.Background()); err != nil {
		t.Errorf("ambient proxy rerouted the socket request: %v", err)
	}
}

func TestRedirectsAreNotFollowed(t *testing.T) {
	path := startServer(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		http.Redirect(w, r, "/elsewhere", http.StatusTemporaryRedirect)
	}))
	_, err := (&Client{Env: tokenEnv(path)}).PolicyBundleStatus(context.Background())
	var se *Error
	if !errors.As(err, &se) || se.Class != evaluator.ReasonProxyError || se.Detail != "the daemon answered 307" {
		t.Errorf("got %v", err)
	}
}

func TestAnOversizedAnswerIsMalformed(t *testing.T) {
	path := startServer(t, http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		_, _ = w.Write([]byte(`{"schema":"` + strings.Repeat("x", maxResponseBytes) + `"}`))
	}))
	_, err := (&Client{Env: tokenEnv(path)}).PolicyBundleStatus(context.Background())
	var se *Error
	if !errors.As(err, &se) || se.Class != evaluator.ReasonMalformedResponse {
		t.Errorf("got %v", err)
	}
}

func TestCallerCancellationIsARefusalWrappingTheContext(t *testing.T) {
	d := startDaemon(t, fakeResponse{Stall: true})
	ctx, cancel := context.WithCancel(context.Background())
	time.AfterFunc(50*time.Millisecond, cancel)
	_, err := (&Client{Env: tokenEnv(d.path)}).PolicyBundleStatus(ctx)
	var se *Error
	if !errors.As(err, &se) || se.Class != evaluator.ReasonProxyUnavailable || !errors.Is(err, context.Canceled) {
		t.Errorf("got %v", err)
	}
}

func TestStreamStopsWhenYieldFails(t *testing.T) {
	raw := "data: 1\n\ndata: 2\n\ndata: [DONE]\n\n"
	d := startDaemon(t, fakeResponse{Status: 200, Raw: &raw, ContentType: "text/event-stream"})
	stop := errors.New("stop")
	var lines []string
	err := (&Client{Env: tokenEnv(d.path)}).StreamChatCompletion(context.Background(), Session{SessionID: "sess-1"}, nil,
		func(line string) error {
			lines = append(lines, line)
			return stop
		})
	if !errors.Is(err, stop) || len(lines) != 1 {
		t.Errorf("got %v after %v", err, lines)
	}
}

// streamServer writes first, flushes, then holds or drops the connection.
func streamServer(t *testing.T, drop bool) string {
	t.Helper()
	release := make(chan struct{})
	var once sync.Once
	t.Cleanup(func() { once.Do(func() { close(release) }) })
	return startServer(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "text/event-stream")
		_, _ = w.Write([]byte("data: first\n\n"))
		w.(http.Flusher).Flush()
		if drop {
			conn, _, err := http.NewResponseController(w).Hijack()
			if err == nil {
				_ = conn.Close()
			}
			return
		}
		select {
		case <-release:
		case <-r.Context().Done():
		}
	}))
}

func TestStreamIdleTimeoutIsAProxyTimeout(t *testing.T) {
	path := streamServer(t, false)
	var lines []string
	c := &Client{Env: tokenEnv(path), ModelTimeout: 200 * time.Millisecond}
	err := c.StreamChatCompletion(context.Background(), Session{SessionID: "sess-1"}, nil, func(l string) error {
		lines = append(lines, l)
		return nil
	})
	var se *Error
	if !errors.As(err, &se) || se.Class != evaluator.ReasonProxyTimeout || len(lines) != 1 {
		t.Errorf("got %v after %v", err, lines)
	}
}

func TestStreamTransportErrorIsUnavailable(t *testing.T) {
	path := streamServer(t, true)
	var lines []string
	err := (&Client{Env: tokenEnv(path)}).StreamChatCompletion(context.Background(), Session{SessionID: "sess-1"}, nil, func(l string) error {
		lines = append(lines, l)
		return nil
	})
	var se *Error
	if !errors.As(err, &se) || se.Class != evaluator.ReasonProxyUnavailable || len(lines) != 1 {
		t.Errorf("got %v after %v", err, lines)
	}
}

func TestConcurrentAuthorizeIsSafe(t *testing.T) {
	responses := []fakeResponse{sessionAnswer}
	for i := 0; i < 16; i++ {
		responses = append(responses, sessionAnswer, permitAnswer)
	}
	d := startDaemon(t, responses...)
	a := &AuthorizationClient{Source: "openwebui", Socket: &Client{Env: tokenEnv(d.path)}}
	var wg sync.WaitGroup
	for i := 0; i < 8; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			_, _ = a.Authorize(context.Background(), evaluator.AuthorizeRequest{UserID: "ada", Tool: "t"})
		}()
	}
	wg.Wait()
}
