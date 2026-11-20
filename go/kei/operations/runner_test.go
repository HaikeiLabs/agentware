// Copyright 2026 Haikei Labs
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.

// Package operations runs generated connector operations behind kei-proxy
// authorization and credential release.
package operations

import (
	"context"
	"errors"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"

	connection "github.com/HaikeiLabs/kei-connector-contracts/gen/go/kei/connection"
	github "github.com/HaikeiLabs/kei-connector-contracts/gen/go/kei/connectors/github/v1"
	"github.com/HaikeiLabs/kei-connector-contracts/gen/go/kei/connectors/github/v1/githubops"
	contracts "github.com/HaikeiLabs/kei-connector-contracts/gen/go/kei/contracts/v1"
	"github.com/haikeilabs/agentware/go/kei/evaluator"
	"github.com/haikeilabs/agentware/go/kei/runtimesocket"
	"github.com/haikeilabs/agentware/go/middleware"
	"google.golang.org/protobuf/encoding/protojson"
	"google.golang.org/protobuf/proto"
)

// syntheticReleasedToken is a synthetic credential: the runner's outputs
// (logs, audit, denied errors) must never carry it.
const syntheticReleasedToken = "synthetic-released-token-0001"

// fakeClient is the Runner's socket client stand-in: it records what the
// runner sent and serves canned decisions.
type fakeClient struct {
	mu sync.Mutex

	authorize func(ctx context.Context) (map[string]any, error)
	release   func(ctx context.Context) (*runtimesocket.ReleaseResult, error)

	authorizeReqs []evaluator.AuthorizeRequest
	releaseReqs   []runtimesocket.ReleaseRequest
	releaseUsers  []string
}

func (f *fakeClient) Authorize(ctx context.Context, req evaluator.AuthorizeRequest) (map[string]any, error) {
	f.mu.Lock()
	f.authorizeReqs = append(f.authorizeReqs, req)
	f.mu.Unlock()
	if f.authorize == nil {
		return map[string]any{"decision": "allow"}, nil
	}
	return f.authorize(ctx)
}

func (f *fakeClient) ReleaseCredential(ctx context.Context, userID string, req runtimesocket.ReleaseRequest) (*runtimesocket.ReleaseResult, error) {
	f.mu.Lock()
	f.releaseReqs = append(f.releaseReqs, req)
	f.releaseUsers = append(f.releaseUsers, userID)
	f.mu.Unlock()
	if f.release == nil {
		return &runtimesocket.ReleaseResult{
			Decision:   "allow",
			Credential: &runtimesocket.Credential{ConnectorID: "github", AccessToken: syntheticReleasedToken},
		}, nil
	}
	return f.release(ctx)
}

// providerServer is a fake GitHub API: it counts requests, captures the
// Authorization header and serves one issue.
type providerServer struct {
	*httptest.Server
	mu   sync.Mutex
	hits int
	auth string
}

func newProviderServer(t *testing.T) *providerServer {
	t.Helper()
	p := &providerServer{}
	p.Server = httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		p.mu.Lock()
		p.hits++
		if a := r.Header.Get("Authorization"); a != "" {
			p.auth = a
		}
		p.mu.Unlock()
		if r.URL.Path != "/repos/octo/repo/issues/42" {
			w.WriteHeader(http.StatusNotFound)
			return
		}
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"id": 42, "number": 42, "title": "Synthetic issue"}`))
	}))
	t.Cleanup(p.Server.Close)
	return p
}

func (p *providerServer) count() int { p.mu.Lock(); defer p.mu.Unlock(); return p.hits }
func (p *providerServer) authHeader() string {
	p.mu.Lock()
	defer p.mu.Unlock()
	return p.auth
}

func testConfig(baseURL string) ConnectorConfig {
	return ConnectorConfig{
		ConnectorID:    "github",
		ConnectionName: "user_oauth",
		Conn:           &contracts.Connection{Kind: contracts.ConnectionKind_CONNECTION_KIND_HTTP_BEARER},
		BaseURL:        baseURL,
	}
}

func testCaller() middleware.CallerContext {
	return middleware.CallerContext{
		UserID:          "user-1",
		InvokingSubject: "user-1",
		Source:          "test",
		ParentSpan:      "span-parent",
		DelegationDepth: 2,
	}
}

// captureHandler is an slog.Handler that stores message+level records.
type captureHandler struct {
	mu      sync.Mutex
	records []string
}

func (h *captureHandler) Enabled(_ context.Context, _ slog.Level) bool { return true }
func (h *captureHandler) Handle(_ context.Context, r slog.Record) error {
	h.mu.Lock()
	defer h.mu.Unlock()
	h.records = append(h.records, r.Message+" "+r.Level.String())
	return nil
}
func (h *captureHandler) WithAttrs([]slog.Attr) slog.Handler { return h }
func (h *captureHandler) WithGroup(string) slog.Handler      { return h }

func captured(t *testing.T, h *captureHandler) []string {
	t.Helper()
	h.mu.Lock()
	defer h.mu.Unlock()
	return append([]string(nil), h.records...)
}

func allowFlow(t *testing.T) (*fakeClient, *providerServer, ConnectorConfig) {
	t.Helper()
	fc := &fakeClient{}
	p := newProviderServer(t)
	return fc, p, testConfig(p.URL)
}

func TestAllowThenCall(t *testing.T) {
	fc, p, cfg := allowFlow(t)
	fc.authorize = func(ctx context.Context) (map[string]any, error) {
		return map[string]any{"decision": "allow"}, nil
	}
	fc.release = func(ctx context.Context) (*runtimesocket.ReleaseResult, error) {
		return &runtimesocket.ReleaseResult{
			Decision:   "allow",
			Credential: &runtimesocket.Credential{ConnectorID: "github", AccessToken: syntheticReleasedToken},
		}, nil
	}
	r := NewRunner(fc, "user-1", cfg, WithAuditSink(func(AuditRecord) {}))

	var callInvoked bool
	resp, err := Run(context.Background(), r, testCaller(), githubops.OpGetIssue,
		func(conn any) (*github.GetIssueResponse, error) {
			callInvoked = true
			httpConn, ok := conn.(connection.HTTP)
			if !ok {
				return nil, errors.New("connection is not an HTTP connection")
			}
			status, _, body, err := httpConn.Do(context.Background(), "GET", "/repos/octo/repo/issues/42", nil, nil)
			if err != nil {
				return nil, err
			}
			if status != 200 {
				return nil, errors.New("unexpected status")
			}
			out := &github.GetIssueResponse{}
			if err := protojson.Unmarshal(body, out); err != nil {
				return nil, err
			}
			return out, nil
		}, &github.GetIssueRequest{Owner: "octo", Repository: "repo", IssueNumber: 42})
	if err != nil {
		t.Fatalf("Run: %v", err)
	}
	if !callInvoked {
		t.Fatal("call was not invoked")
	}
	if got := resp.GetTitle(); got != "Synthetic issue" {
		t.Errorf("response title = %q", got)
	}
	if got := p.authHeader(); got != "Bearer "+syntheticReleasedToken {
		t.Errorf("provider Authorization = %q, want released token bearer", got)
	}
	if p.count() != 1 {
		t.Errorf("provider requests = %d, want 1", p.count())
	}
	fc.mu.Lock()
	defer fc.mu.Unlock()
	if len(fc.authorizeReqs) != 1 {
		t.Fatalf("authorize calls = %d, want 1", len(fc.authorizeReqs))
	}
	ar := fc.authorizeReqs[0]
	if ar.Action != "connector:github.issues.get" {
		t.Errorf("authorize action = %q", ar.Action)
	}
	if ar.Resource != "repos/octo/repo" {
		t.Errorf("authorize resource = %q, want rendered template", ar.Resource)
	}
	if ar.UserID != "user-1" || ar.InvokingSubject != "user-1" {
		t.Errorf("authorize user/subject = %q/%q", ar.UserID, ar.InvokingSubject)
	}
	if ar.ParentSpan != "span-parent" || ar.DelegationDepth != 2 {
		t.Errorf("authorize span/depth = %q/%d", ar.ParentSpan, ar.DelegationDepth)
	}
	if len(fc.releaseReqs) != 1 {
		t.Fatalf("release calls = %d, want 1", len(fc.releaseReqs))
	}
	rr := fc.releaseReqs[0]
	if rr.ConnectorID != "github" || rr.Capability != "connector:github.issues.get" || rr.Resource != "repos/octo/repo" {
		t.Errorf("release req = %+v", rr)
	}
	if fc.releaseUsers[0] != "user-1" {
		t.Errorf("release user = %q", fc.releaseUsers[0])
	}
}

func TestDenyBuildsNoConnection(t *testing.T) {
	fc, p, cfg := allowFlow(t)
	fc.authorize = func(ctx context.Context) (map[string]any, error) {
		return map[string]any{"decision": "deny", "reason_code": "policy_denied"}, nil
	}
	var callInvoked bool
	var audit AuditRecord
	r := NewRunner(fc, "user-1", cfg, WithAuditSink(func(a AuditRecord) { audit = a }))

	_, err := Run(context.Background(), r, testCaller(), githubops.OpGetIssue,
		func(conn any) (*github.GetIssueResponse, error) {
			callInvoked = true
			return &github.GetIssueResponse{}, nil
		}, &github.GetIssueRequest{Owner: "octo", Repository: "repo", IssueNumber: 42})
	if err == nil {
		t.Fatal("Run must deny")
	}
	var denied *OperationDeniedError
	if !errors.As(err, &denied) {
		t.Fatalf("error = %T, want *OperationDeniedError", err)
	}
	if denied.ReasonCode != "policy_denied" {
		t.Errorf("reason code = %q", denied.ReasonCode)
	}
	if denied.Decision.Action != middleware.ActionDeny {
		t.Errorf("decision action = %q", denied.Decision.Action)
	}
	if callInvoked {
		t.Error("call was invoked on deny")
	}
	if p.count() != 0 {
		t.Errorf("provider requests = %d, want 0", p.count())
	}
	if audit.Decision != "deny" || audit.Outcome != "denied" || audit.OperationID != "github.issues.get" {
		t.Errorf("audit = %+v", audit)
	}
}

func TestEnrollmentAndConnectReturned(t *testing.T) {
	t.Run("enrollment_required", func(t *testing.T) {
		fc, p, cfg := allowFlow(t)
		fc.authorize = func(ctx context.Context) (map[string]any, error) {
			return map[string]any{"decision": "deny", "reason_code": "enrollment_required",
				"enrollment": map[string]any{"url": "https://synthetic.example/claim-1", "provider": "github"}}, nil
		}
		r := NewRunner(fc, "user-1", cfg)
		_, err := Run(context.Background(), r, testCaller(), githubops.OpGetIssue,
			func(conn any) (*github.GetIssueResponse, error) { return &github.GetIssueResponse{}, nil },
			&github.GetIssueRequest{Owner: "octo", Repository: "repo", IssueNumber: 42})
		if !errors.Is(err, ErrEnrollmentRequired) {
			t.Fatalf("error = %v, want ErrEnrollmentRequired", err)
		}
		var ere *EnrollmentRequiredError
		if !errors.As(err, &ere) {
			t.Fatalf("error = %T, want *EnrollmentRequiredError", err)
		}
		if ere.Enrollment["url"] != "https://synthetic.example/claim-1" {
			t.Errorf("enrollment = %v", ere.Enrollment)
		}
		if p.count() != 0 {
			t.Errorf("provider requests = %d, want 0", p.count())
		}
	})
	t.Run("connect_required", func(t *testing.T) {
		fc, p, cfg := allowFlow(t)
		fc.authorize = func(ctx context.Context) (map[string]any, error) {
			return map[string]any{"decision": "deny", "reason_code": "connect_required",
				"connect": map[string]any{"url": "https://synthetic.example/connect-1", "provider": "github"}}, nil
		}
		r := NewRunner(fc, "user-1", cfg)
		_, err := Run(context.Background(), r, testCaller(), githubops.OpGetIssue,
			func(conn any) (*github.GetIssueResponse, error) { return &github.GetIssueResponse{}, nil },
			&github.GetIssueRequest{Owner: "octo", Repository: "repo", IssueNumber: 42})
		if !errors.Is(err, ErrConnectRequired) {
			t.Fatalf("error = %v, want ErrConnectRequired", err)
		}
		var cre *ConnectRequiredError
		if !errors.As(err, &cre) {
			t.Fatalf("error = %T, want *ConnectRequiredError", err)
		}
		if cre.Connect["url"] != "https://synthetic.example/connect-1" {
			t.Errorf("connect = %v", cre.Connect)
		}
		if p.count() != 0 {
			t.Errorf("provider requests = %d, want 0", p.count())
		}
	})
}

func TestReleaseFailureTimeoutUnavailableDeny(t *testing.T) {
	t.Run("release_error", func(t *testing.T) {
		fc, p, cfg := allowFlow(t)
		fc.release = func(ctx context.Context) (*runtimesocket.ReleaseResult, error) {
			return nil, errors.New("socket closed")
		}
		r := NewRunner(fc, "user-1", cfg)
		_, err := Run(context.Background(), r, testCaller(), githubops.OpGetIssue,
			func(conn any) (*github.GetIssueResponse, error) { return &github.GetIssueResponse{}, nil },
			&github.GetIssueRequest{Owner: "octo", Repository: "repo", IssueNumber: 42})
		deniedWithCode(t, err, "credential_release_failed")
		if p.count() != 0 {
			t.Errorf("provider requests = %d, want 0", p.count())
		}
	})
	t.Run("release_timeout", func(t *testing.T) {
		fc, p, cfg := allowFlow(t)
		fc.release = func(ctx context.Context) (*runtimesocket.ReleaseResult, error) {
			select {
			case <-time.After(150 * time.Millisecond):
				return &runtimesocket.ReleaseResult{Decision: "allow",
					Credential: &runtimesocket.Credential{AccessToken: syntheticReleasedToken}}, nil
			case <-ctx.Done():
				return nil, ctx.Err()
			}
		}
		r := NewRunner(fc, "user-1", cfg)
		ctx, cancel := context.WithTimeout(context.Background(), 20*time.Millisecond)
		defer cancel()
		_, err := Run(ctx, r, testCaller(), githubops.OpGetIssue,
			func(conn any) (*github.GetIssueResponse, error) { return &github.GetIssueResponse{}, nil },
			&github.GetIssueRequest{Owner: "octo", Repository: "repo", IssueNumber: 42})
		deniedWithCode(t, err, "credential_release_failed")
		if p.count() != 0 {
			t.Errorf("provider requests = %d, want 0", p.count())
		}
	})
	t.Run("release_unavailable_decision", func(t *testing.T) {
		fc, p, cfg := allowFlow(t)
		fc.release = func(ctx context.Context) (*runtimesocket.ReleaseResult, error) {
			return &runtimesocket.ReleaseResult{Decision: "deny", ReasonCode: "credential_unavailable"}, nil
		}
		r := NewRunner(fc, "user-1", cfg)
		_, err := Run(context.Background(), r, testCaller(), githubops.OpGetIssue,
			func(conn any) (*github.GetIssueResponse, error) { return &github.GetIssueResponse{}, nil },
			&github.GetIssueRequest{Owner: "octo", Repository: "repo", IssueNumber: 42})
		deniedWithCode(t, err, "credential_unavailable")
		if p.count() != 0 {
			t.Errorf("provider requests = %d, want 0", p.count())
		}
	})
}

func deniedWithCode(t *testing.T, err error, code string) *OperationDeniedError {
	t.Helper()
	if err == nil {
		t.Fatal("Run must deny")
	}
	var denied *OperationDeniedError
	if !errors.As(err, &denied) {
		t.Fatalf("error = %T (%v), want *OperationDeniedError", err, err)
	}
	if denied.ReasonCode != code {
		t.Fatalf("reason code = %q, want %q", denied.ReasonCode, code)
	}
	return denied
}

func TestRunnerNeverLogsCredential(t *testing.T) {
	fc, p, cfg := allowFlow(t)
	var audit AuditRecord
	h := &captureHandler{}
	r := NewRunner(fc, "user-1", cfg,
		WithLogger(slog.New(h)),
		WithAuditSink(func(a AuditRecord) { audit = a }))
	fc.release = func(ctx context.Context) (*runtimesocket.ReleaseResult, error) {
		return &runtimesocket.ReleaseResult{Decision: "allow",
			Credential: &runtimesocket.Credential{AccessToken: syntheticReleasedToken}}, nil
	}
	_, err := Run(context.Background(), r, testCaller(), githubops.OpGetIssue,
		func(conn any) (*github.GetIssueResponse, error) {
			httpConn, ok := conn.(connection.HTTP)
			if !ok {
				return nil, errors.New("connection is not an HTTP connection")
			}
			if _, _, _, err := httpConn.Do(context.Background(), "GET", "/repos/octo/repo/issues/42", nil, nil); err != nil {
				return nil, err
			}
			return nil, errors.New("provider 500: synthetic-error-detail")
		}, &github.GetIssueRequest{Owner: "octo", Repository: "repo", IssueNumber: 42})
	if err == nil {
		t.Fatal("Run must surface the call error")
	}
	for _, rec := range captured(t, h) {
		if strings.Contains(rec, syntheticReleasedToken) {
			t.Errorf("log record carries the released credential: %q", rec)
		}
	}
	if strings.Contains(err.Error(), syntheticReleasedToken) {
		t.Error("returned error carries the released credential")
	}
	if strings.Contains(audit.OperationID+audit.Decision+audit.Outcome, syntheticReleasedToken) {
		t.Error("audit record carries the released credential")
	}
	if audit.Decision != "allow" || audit.Outcome != "error" {
		t.Errorf("audit = %+v", audit)
	}
	if p.count() != 1 {
		t.Errorf("provider requests = %d, want 1", p.count())
	}
}

func TestConnectorNotNamedSurfacesReason(t *testing.T) {
	fc, p, cfg := allowFlow(t)
	r := NewRunner(fc, "user-1", cfg)
	op := connection.Op{
		ID:               "notgithub.issues.get",
		Class:            connection.Read,
		ResourceTemplate: "repos/{owner}/{repository}",
		MethodName:       "GetIssue",
		Connections:      []string{"user_oauth"},
		Transport:        connection.TransportHTTP,
	}
	_, err := Run(context.Background(), r, testCaller(), op,
		func(conn any) (*github.GetIssueResponse, error) { return &github.GetIssueResponse{}, nil },
		&github.GetIssueRequest{Owner: "octo", Repository: "repo", IssueNumber: 42})
	deniedWithCode(t, err, "connector_not_named")
	if p.count() != 0 {
		t.Errorf("provider requests = %d, want 0", p.count())
	}
}

// compile-time: the runner must accept proto messages.
var _ proto.Message = (*github.GetIssueResponse)(nil)
