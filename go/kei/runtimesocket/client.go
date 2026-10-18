package runtimesocket

import (
	"bufio"
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"maps"
	"net"
	"net/http"
	"os"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	"github.com/haikeilabs/agentware/go/kei/evaluator"
)

// Names and values shared with the Python and TypeScript clients.
const (
	DefaultSocketPath        = "/run/kei-proxy/runtime.sock"
	SocketPathEnv            = "KEI_RUNTIME_SOCKET_PATH"
	SessionHeader            = "X-Kei-Session"
	SessionSchema            = "kei.session/v2"
	AuthorizeRequestSchema   = "kei.authorize-request/v1"
	AuthorizeResponseSchema  = "kei.authorize-response/v1"
	PolicyBundleStatusSchema = "kei.policy-bundle-status/v1"
	// SessionNotFound is the daemon code for a session it no longer knows.
	SessionNotFound = "session_not_found"

	// DefaultTimeout bounds session, authorize, model-list and status calls.
	DefaultTimeout = 10 * time.Second
	// DefaultModelTimeout bounds a chat completion, and is the idle limit
	// between lines of a streamed one.
	DefaultModelTimeout = 120 * time.Second
)

// baseURL is the request URL host; the Unix-socket dialer ignores it.
const baseURL = "http://kei-proxy"

// maxResponseBytes bounds a non-streamed answer and one streamed line.
const maxResponseBytes = 8 << 20

// Identity is who a runtime session is for: a harness Source and the user's
// id there.
type Identity struct {
	Source     string
	ExternalID string
}

// Session is a named daemon session, as PUT /v1/session returned it.
type Session struct {
	Source     string
	ExternalID string
	SessionID  string
}

// Identity returns the identity the session was opened for.
func (s Session) Identity() Identity {
	return Identity{Source: s.Source, ExternalID: s.ExternalID}
}

// Client talks to the kei-proxy daemon over its Unix socket. The zero value
// is usable; it is safe for concurrent use and must not be copied after first
// use. One session is cached per (source, external id), as in the TypeScript
// SDK. Every failure is an *Error, never a partial answer.
type Client struct {
	// SocketPath is the daemon's socket. Default: KEI_RUNTIME_SOCKET_PATH
	// (read from Env), then DefaultSocketPath.
	SocketPath string
	// Timeout bounds session, authorize, model-list and status calls.
	// Default DefaultTimeout.
	Timeout time.Duration
	// ModelTimeout bounds a chat completion, and the wait for each line of a
	// streamed one. Default DefaultModelTimeout.
	ModelTimeout time.Duration
	// Env is where KEI_RUNTIME_TOKEN and KEI_RUNTIME_SOCKET_PATH are read.
	// Nil means the process environment. The token is read on every request.
	Env map[string]string

	mu       sync.Mutex
	sessions map[[2]string]Session

	httpOnce sync.Once
	http     *http.Client
}

// String names only the socket path.
func (c *Client) String() string {
	return fmt.Sprintf("runtimesocket.Client{SocketPath: %q}", c.ResolvedSocketPath())
}

// GoString names only the socket path.
func (c *Client) GoString() string { return c.String() }

func (c *Client) lookup(key string) string {
	if c.Env != nil {
		return c.Env[key]
	}
	return os.Getenv(key)
}

// ResolvedSocketPath is the socket the client dials: SocketPath, else
// KEI_RUNTIME_SOCKET_PATH, else DefaultSocketPath.
func (c *Client) ResolvedSocketPath() string {
	if c.SocketPath != "" {
		return c.SocketPath
	}
	if p := c.lookup(SocketPathEnv); p != "" {
		return p
	}
	return DefaultSocketPath
}

func (c *Client) timeout() time.Duration {
	if c.Timeout > 0 {
		return c.Timeout
	}
	return DefaultTimeout
}

func (c *Client) modelTimeout() time.Duration {
	if c.ModelTimeout > 0 {
		return c.ModelTimeout
	}
	return DefaultModelTimeout
}

// OpenSession creates, or reuses, the daemon session for identity.
func (c *Client) OpenSession(ctx context.Context, identity Identity) (Session, error) {
	if strings.TrimSpace(identity.Source) == "" || strings.TrimSpace(identity.ExternalID) == "" {
		return Session{}, fmt.Errorf("open session: %w", ErrInvalidIdentity)
	}
	key := [2]string{identity.Source, identity.ExternalID}
	c.mu.Lock()
	cached, ok := c.sessions[key]
	c.mu.Unlock()
	if ok {
		return cached, nil
	}
	resp, err := c.send(ctx, http.MethodPut, "/v1/session", "", map[string]any{
		"schema": SessionSchema, "source": identity.Source, "external_id": identity.ExternalID,
	}, c.timeout())
	if err != nil {
		return Session{}, err
	}
	body, err := resp.object()
	if err != nil {
		return Session{}, err
	}
	sessionID, _ := body["session_id"].(string)
	if body["schema"] != SessionSchema || sessionID == "" {
		return Session{}, malformed("the daemon returned an invalid session")
	}
	session := Session{Source: identity.Source, ExternalID: identity.ExternalID, SessionID: sessionID}
	c.mu.Lock()
	if c.sessions == nil {
		c.sessions = make(map[[2]string]Session)
	}
	c.sessions[key] = session
	c.mu.Unlock()
	return session, nil
}

// CloseSession ends session on the daemon. A session the daemon already ended
// is not an error.
func (c *Client) CloseSession(ctx context.Context, session Session) error {
	c.forget(session)
	resp, err := c.send(ctx, http.MethodDelete, "/v1/session", session.SessionID, nil, c.timeout())
	if err != nil {
		return err
	}
	if resp.status == http.StatusNotFound && errorCode(resp.body) == SessionNotFound {
		return nil
	}
	return resp.check()
}

// ForgetSession drops the cached session for identity; the next OpenSession
// opens a new one.
func (c *Client) ForgetSession(identity Identity) {
	c.mu.Lock()
	delete(c.sessions, [2]string{identity.Source, identity.ExternalID})
	c.mu.Unlock()
}

// forget drops session from the cache, unless it was already replaced.
func (c *Client) forget(session Session) {
	key := [2]string{session.Source, session.ExternalID}
	c.mu.Lock()
	if cached, ok := c.sessions[key]; ok && cached.SessionID == session.SessionID {
		delete(c.sessions, key)
	}
	c.mu.Unlock()
}

func (c *Client) reopen(ctx context.Context, stale Session) (Session, error) {
	c.forget(stale)
	return c.OpenSession(ctx, stale.Identity())
}

func validSession(s Session) error {
	if s.Source == "" || s.ExternalID == "" || s.SessionID == "" {
		return ErrInvalidSession
	}
	return nil
}

// Authorize asks the daemon for one decision in session.
//
// It returns the kei.authorize-response/v1 object (decision plus reason_code,
// reason, policy_id, enrollment ...). Only permit/allow is an allow; a deny is
// returned, not an error. The request names no user or agent: UserID,
// InvokingSubject, AgentVersion, Resources and WorkspaceID are not
// sent because the daemon takes identity from the authenticated session,
// derives workspace scope from the token, and rejects unknown fields.
func (c *Client) Authorize(ctx context.Context, session Session, req evaluator.AuthorizeRequest) (map[string]any, error) {
	if err := validSession(session); err != nil {
		return nil, fmt.Errorf("authorize: %w", err)
	}
	body := map[string]any{"schema": AuthorizeRequestSchema, "tool": req.Tool}
	for k, v := range map[string]string{
		"action":           req.Action,
		"resource":         req.Resource,
		"span_id":          req.SpanID,
		"parent_span":      req.ParentSpan,
		"framework":        req.Framework,
		"tool_args_digest": req.ToolArgsDigest,
	} {
		if v != "" {
			body[k] = v
		}
	}
	if req.DelegationDepth > 0 {
		body["delegation_depth"] = req.DelegationDepth
	}

	for attempt := 0; attempt < 2; attempt++ {
		resp, err := c.send(ctx, http.MethodPost, "/v1/authorize", session.SessionID, body, c.timeout())
		var answer map[string]any
		if err == nil {
			answer, err = resp.object()
		}
		if err != nil {
			var se *Error
			if attempt == 0 && errors.As(err, &se) && se.Code == SessionNotFound {
				if session, err = c.reopen(ctx, session); err != nil {
					return nil, err
				}
				continue
			}
			return nil, err
		}
		if _, ok := answer["decision"].(string); !ok || answer["schema"] != AuthorizeResponseSchema {
			return nil, malformed("the daemon returned an invalid authorize response")
		}
		if attempt == 0 && answer["reason_code"] == SessionNotFound {
			if session, err = c.reopen(ctx, session); err != nil {
				return nil, err
			}
			continue
		}
		return answer, nil
	}
	return nil, refusal(evaluator.ReasonProxyError, "socket authorize failed closed")
}

// ListModels returns the models the daemon advertises (GET /v1/models), each
// an object with a non-empty string id.
func (c *Client) ListModels(ctx context.Context) ([]map[string]any, error) {
	resp, err := c.send(ctx, http.MethodGet, "/v1/models", "", nil, c.timeout())
	if err != nil {
		return nil, err
	}
	body, err := resp.object()
	if err != nil {
		return nil, err
	}
	data, ok := body["data"].([]any)
	if !ok {
		return nil, malformed("the daemon returned no model list")
	}
	models := make([]map[string]any, 0, len(data))
	for _, entry := range data {
		if m, ok := entry.(map[string]any); ok {
			if id, ok := m["id"].(string); ok && id != "" {
				models = append(models, m)
			}
		}
	}
	return models, nil
}

// ChatCompletion runs one non-streaming chat completion in session. The body
// is payload with "stream": false.
func (c *Client) ChatCompletion(ctx context.Context, session Session, payload map[string]any) (map[string]any, error) {
	body := maps.Clone(payload)
	if body == nil {
		body = map[string]any{}
	}
	body["stream"] = false
	call := func(sessionID string) (*response, error) {
		return c.send(ctx, http.MethodPost, "/v1/chat/completions", sessionID, body, c.modelTimeout())
	}
	resp, err := call(session.SessionID)
	if err != nil {
		return nil, err
	}
	if !resp.ok() && errorCode(resp.body) == SessionNotFound {
		if session, err = c.reopen(ctx, session); err != nil {
			return nil, err
		}
		if resp, err = call(session.SessionID); err != nil {
			return nil, err
		}
	}
	return resp.object()
}

// StreamChatCompletion runs a streaming chat completion in session and calls
// yield with each server-sent "data:" line, unchanged ("data: [DONE]"
// included); blank lines, comments and other fields are dropped. The body is
// payload with "stream": true. ModelTimeout bounds the wait for the answer and
// for each following line, not the whole stream. If yield returns an error the
// stream stops and that error is returned as is.
func (c *Client) StreamChatCompletion(ctx context.Context, session Session, payload map[string]any, yield func(line string) error) error {
	body := maps.Clone(payload)
	if body == nil {
		body = map[string]any{}
	}
	body["stream"] = true
	for attempt := 0; attempt < 2; attempt++ {
		retry, err := c.stream(ctx, session.SessionID, body, attempt == 0, yield)
		if !retry {
			return err
		}
		if session, err = c.reopen(ctx, session); err != nil {
			return err
		}
	}
	return refusal(evaluator.ReasonProxyError, "socket stream failed closed")
}

// stream runs one streaming attempt. It reports retry when the daemon said
// session_not_found and mayRetry is set.
func (c *Client) stream(ctx context.Context, sessionID string, body map[string]any, mayRetry bool, yield func(string) error) (retry bool, err error) {
	idle := c.modelTimeout()
	streamCtx, cancel := context.WithCancel(ctx)
	defer cancel()
	var timedOut atomic.Bool
	timer := time.AfterFunc(idle, func() {
		timedOut.Store(true)
		cancel()
	})
	defer timer.Stop()

	req, err := c.request(streamCtx, http.MethodPost, "/v1/chat/completions", sessionID, body)
	if err != nil {
		return false, err
	}
	res, err := c.httpClient().Do(req)
	if err != nil {
		return false, transportError(ctx, err, timedOut.Load(), c.ResolvedSocketPath())
	}
	defer func() { _ = res.Body.Close() }()

	if res.StatusCode < 200 || res.StatusCode > 299 {
		errBody, _ := io.ReadAll(io.LimitReader(res.Body, maxResponseBytes))
		code := errorCode(errBody)
		if mayRetry && code == SessionNotFound {
			return true, nil
		}
		return false, statusError(res.StatusCode, code)
	}

	scanner := bufio.NewScanner(res.Body)
	scanner.Buffer(make([]byte, 0, 64<<10), maxResponseBytes)
	for {
		timer.Reset(idle)
		if !scanner.Scan() {
			break
		}
		line := scanner.Text()
		if !strings.HasPrefix(line, "data:") {
			continue
		}
		if err := yield(line); err != nil {
			return false, err
		}
	}
	if err := scanner.Err(); err != nil {
		if errors.Is(err, bufio.ErrTooLong) {
			return false, malformed("a streamed line exceeds the size limit")
		}
		return false, transportError(ctx, err, timedOut.Load(), c.ResolvedSocketPath())
	}
	return false, nil
}

// PolicyBundleStatus returns the daemon's kei.policy-bundle-status/v1 (e.g.
// its bundle_schema).
func (c *Client) PolicyBundleStatus(ctx context.Context) (map[string]any, error) {
	resp, err := c.send(ctx, http.MethodGet, "/v1/policy-bundle/status", "", nil, c.timeout())
	if err != nil {
		return nil, err
	}
	body, err := resp.object()
	if err != nil {
		return nil, err
	}
	if body["schema"] != PolicyBundleStatusSchema {
		return nil, malformed("the daemon returned an invalid bundle status")
	}
	return body, nil
}

// Transport ---------------------------------------------------------------

// httpClient dials the socket and nothing else: no ambient proxy, no
// redirects.
func (c *Client) httpClient() *http.Client {
	c.httpOnce.Do(func() {
		c.http = &http.Client{
			Transport: &http.Transport{
				// Ambient HTTP(S)_PROXY must never reroute a socket request.
				Proxy: nil,
				DialContext: func(ctx context.Context, _, _ string) (net.Conn, error) {
					return dialSocket(ctx, c.ResolvedSocketPath())
				},
				MaxIdleConns:    4,
				IdleConnTimeout: 30 * time.Second,
			},
			CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse },
		}
	})
	return c.http
}

// request builds one request with the token read now. A missing token is a
// missing_token refusal before anything is sent.
func (c *Client) request(ctx context.Context, method, path, sessionID string, body map[string]any) (*http.Request, error) {
	token := c.lookup(evaluator.RuntimeTokenEnv)
	if token == "" {
		return nil, refusal(evaluator.ReasonMissingToken, evaluator.RuntimeTokenEnv+" is not set")
	}
	var reader io.Reader
	if body != nil {
		var buf bytes.Buffer
		enc := json.NewEncoder(&buf)
		enc.SetEscapeHTML(false)
		if err := enc.Encode(body); err != nil {
			return nil, fmt.Errorf("encode %s %s body: %w", method, path, err)
		}
		reader = bytes.NewReader(bytes.TrimRight(buf.Bytes(), "\n"))
	}
	req, err := http.NewRequestWithContext(ctx, method, baseURL+path, reader)
	if err != nil {
		return nil, fmt.Errorf("build %s %s: %w", method, path, err)
	}
	req.Header.Set("Authorization", "Bearer "+token)
	if body != nil {
		req.Header.Set("Content-Type", "application/json")
	}
	if sessionID != "" {
		req.Header.Set(SessionHeader, sessionID)
	}
	return req, nil
}

// response is a fully read answer.
type response struct {
	status   int
	body     []byte
	overflow bool
}

// send runs one request bounded by timeout and reads the whole answer.
func (c *Client) send(ctx context.Context, method, path, sessionID string, body map[string]any, timeout time.Duration) (*response, error) {
	callCtx, cancel := context.WithTimeout(ctx, timeout)
	defer cancel()
	req, err := c.request(callCtx, method, path, sessionID, body)
	if err != nil {
		return nil, err
	}
	res, err := c.httpClient().Do(req)
	if err != nil {
		return nil, transportError(ctx, err, false, c.ResolvedSocketPath())
	}
	defer func() { _ = res.Body.Close() }()
	data, err := io.ReadAll(io.LimitReader(res.Body, maxResponseBytes+1))
	if err != nil {
		return nil, transportError(ctx, err, false, c.ResolvedSocketPath())
	}
	resp := &response{status: res.StatusCode, body: data}
	if len(data) > maxResponseBytes {
		resp.body, resp.overflow = data[:maxResponseBytes], true
	}
	return resp, nil
}

func (r *response) ok() bool { return r.status >= 200 && r.status <= 299 }

// check turns a non-2xx answer into a proxy_error carrying the daemon code.
func (r *response) check() error {
	if !r.ok() {
		return statusError(r.status, errorCode(r.body))
	}
	return nil
}

// object returns a 2xx answer's JSON object body ({} when empty).
func (r *response) object() (map[string]any, error) {
	if err := r.check(); err != nil {
		return nil, err
	}
	if r.overflow {
		return nil, malformed("the daemon's answer exceeds the size limit")
	}
	if r.status == http.StatusNoContent || len(bytes.TrimSpace(r.body)) == 0 {
		return map[string]any{}, nil
	}
	var parsed any
	if err := json.Unmarshal(r.body, &parsed); err != nil {
		return nil, malformed("the daemon's answer is not JSON")
	}
	obj, ok := parsed.(map[string]any)
	if !ok {
		return nil, malformed("the daemon's answer is not a JSON object")
	}
	return obj, nil
}

// SendTools posts a serialized tool manifest to the runtime socket's
// registration endpoint. The caller owns manifest validation and encoding.
func (c *Client) SendTools(ctx context.Context, manifest []byte) error {
	var body map[string]any
	if err := json.Unmarshal(manifest, &body); err != nil || body == nil {
		return malformed("tool manifest is not a JSON object")
	}
	resp, err := c.send(ctx, http.MethodPost, "/v1/tools:register", "", body, c.timeout())
	if err != nil {
		return err
	}
	return resp.check()
}
