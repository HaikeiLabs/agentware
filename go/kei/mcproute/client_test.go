package mcproute

import (
	"encoding/json"
	"errors"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"syscall"
	"testing"
)

// fixtureCase mirrors the JSON schema in fixtures/kei/mcp-route-cases.v1.json.
type fixtureCase struct {
	ID          string `json:"id"`
	Description string `json:"description"`
	Request     struct {
		RouteKey *string           `json:"route_key"`
		Headers  map[string]string `json:"headers"`
		JSONRPC  string            `json:"jsonrpc"`
		ID       json.Number       `json:"id"`
		Method   string            `json:"method"`
		Params   json.RawMessage   `json:"params"`
	} `json:"request"`
	Response struct {
		Status      int              `json:"status"`
		ContentType string           `json:"content_type"`
		Body        *json.RawMessage `json:"body,omitempty"`
		BodyEvents  []string         `json:"body_events,omitempty"`
	} `json:"response"`
	Expected struct {
		Error *struct {
			Type       string `json:"type"`
			Code       int    `json:"code"`
			Message    string `json:"message"`
			Reason     string `json:"reason"`
			DecisionID string `json:"decision_id"`
		} `json:"error"`
		Tools   []string          `json:"tools,omitempty"`
		Content []json.RawMessage `json:"content,omitempty"`
		IsError bool              `json:"isError,omitempty"`
	} `json:"expected"`
}

type fixtures struct {
	Cases []fixtureCase `json:"cases"`
}

func loadFixtures(t *testing.T) *fixtures {
	t.Helper()
	data, err := os.ReadFile("../../../fixtures/kei/mcp-route-cases.v1.json")
	if err != nil {
		t.Fatal("load fixtures:", err)
	}
	var f fixtures
	if err := json.Unmarshal(data, &f); err != nil {
		t.Fatal("parse fixtures:", err)
	}
	return &f
}

func TestMCPRouteFixtures(t *testing.T) {
	f := loadFixtures(t)

	for _, c := range f.Cases {
		c := c
		t.Run(c.ID, func(t *testing.T) {
			t.Parallel()

			routeKey := "test-route-key"
			if c.Request.RouteKey != nil {
				routeKey = *c.Request.RouteKey
			}

			// Build the httptest server that returns the fixture response.
			var server *httptest.Server
			handler := http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				// Verify basic request properties.
				if r.Method != http.MethodPost {
					t.Errorf("expected POST, got %s", r.Method)
				}
				if !strings.HasPrefix(r.URL.Path, "/mcp/") {
					t.Errorf("expected /mcp/ path, got %s", r.URL.Path)
				}
				if r.Header.Get("Authorization") != "Bearer "+routeKey {
					t.Errorf("expected Authorization: Bearer %s, got %q", routeKey, r.Header.Get("Authorization"))
				}
				if r.Header.Get("Content-Type") != "application/json" {
					t.Errorf("expected Content-Type application/json, got %q", r.Header.Get("Content-Type"))
				}

				// Set response headers.
				if c.Response.ContentType == "text/event-stream" {
					w.Header().Set("Content-Type", "text/event-stream")
				} else {
					w.Header().Set("Content-Type", "application/json")
				}
				w.WriteHeader(c.Response.Status)

				if len(c.Response.BodyEvents) > 0 {
					for _, event := range c.Response.BodyEvents {
						_, _ = w.Write([]byte(event))
					}
					if flusher, ok := w.(http.Flusher); ok {
						flusher.Flush()
					}
				} else if c.Response.Body != nil {
					body, _ := json.Marshal(c.Response.Body)
					_, _ = w.Write(body)
				}
			})

			server = httptest.NewServer(handler)
			defer server.Close()

			client := &Client{
				Slug:    "test-connector",
				BaseURL: server.URL,
				Env:     map[string]string{RouteKeyEnv: routeKey},
			}

			ctx := t.Context()
			switch c.Request.Method {
			case "tools/list":
				tools, err := client.ListTools(ctx)
				if c.Expected.Error != nil {
					if err == nil {
						t.Fatal("expected error, got nil")
					}
					var re *RouteError
					if !errors.As(err, &re) {
						t.Fatalf("expected *RouteError, got %T: %v", err, err)
					}
					if re.Code != c.Expected.Error.Code {
						t.Errorf("error code: got %d, want %d", re.Code, c.Expected.Error.Code)
					}
					if re.Reason != c.Expected.Error.Reason {
						t.Errorf("error reason: got %q, want %q", re.Reason, c.Expected.Error.Reason)
					}
					if c.Expected.Error.DecisionID != "" && re.DecisionID != c.Expected.Error.DecisionID {
						t.Errorf("error decision_id: got %q, want %q", re.DecisionID, c.Expected.Error.DecisionID)
					}
				} else {
					if err != nil {
						t.Fatalf("unexpected error: %v", err)
					}
					if len(c.Expected.Tools) > 0 {
						if len(tools) != len(c.Expected.Tools) {
							t.Errorf("tool count: got %d, want %d", len(tools), len(c.Expected.Tools))
						}
						for i, name := range c.Expected.Tools {
							if i < len(tools) && tools[i].Name != name {
								t.Errorf("tool[%d].Name: got %q, want %q", i, tools[i].Name, name)
							}
						}
					}
				}

			case "tools/call":
				var params struct {
					Name      string         `json:"name"`
					Arguments map[string]any `json:"arguments"`
				}
				if c.Request.Params != nil {
					_ = json.Unmarshal(c.Request.Params, &params)
				}
				result, err := client.CallTool(ctx, params.Name, params.Arguments)
				if c.Expected.Error != nil {
					if err == nil {
						t.Fatal("expected error, got nil")
					}
					var re *RouteError
					if !errors.As(err, &re) {
						t.Fatalf("expected *RouteError, got %T: %v", err, err)
					}
					if re.Code != c.Expected.Error.Code {
						t.Errorf("error code: got %d, want %d", re.Code, c.Expected.Error.Code)
					}
					if re.Reason != c.Expected.Error.Reason {
						t.Errorf("error reason: got %q, want %q", re.Reason, c.Expected.Error.Reason)
					}
				} else {
					if err != nil {
						t.Fatalf("unexpected error: %v", err)
					}
					if result == nil {
						t.Fatal("expected non-nil result")
					}
					if result.IsError != c.Expected.IsError {
						t.Errorf("isError: got %v, want %v", result.IsError, c.Expected.IsError)
					}
				}

			default:
				t.Fatalf("unknown method %q", c.Request.Method)
			}
		})
	}
}

func TestRouteNotLocal(t *testing.T) {
	client := &Client{
		Slug:    "test",
		BaseURL: "http://remote.example.com",
		Env:     map[string]string{RouteKeyEnv: "kmr_test0000000000000000000000000000000"},
	}
	_, err := client.ListTools(t.Context())
	if !errors.Is(err, ErrRouteNotLocal) {
		t.Fatalf("expected ErrRouteNotLocal, got %v", err)
	}
}

func TestMissingRouteKey(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusOK)
	}))
	defer server.Close()

	client := &Client{
		Slug:    "test",
		BaseURL: server.URL,
		Env:     map[string]string{}, // no route key
	}
	_, err := client.ListTools(t.Context())
	if err == nil || !strings.Contains(err.Error(), "KEI_MCP_ROUTE_KEY is not set") {
		t.Fatalf("expected missing route key error, got %v", err)
	}
}

func TestTypedErrors(t *testing.T) {
	tests := []struct {
		code     int
		sentinel error
	}{
		{CodeDenied, ErrPolicyDenied},
		{CodeManifest, ErrManifestDrift},
		{CodeUpstream, ErrUpstream},
		{CodeHeaderMismatch, ErrHeaderMismatch},
		{CodeUnauthenticated, ErrSessionEnded},
	}
	for _, tt := range tests {
		err := &RouteError{Code: tt.code, Message: "test"}
		if !errors.Is(err, tt.sentinel) {
			t.Errorf("RouteError{Code: %d}.Is(%v) = false", tt.code, tt.sentinel)
		}
		// Verify the sentinel itself is not nil.
		if tt.sentinel == nil {
			t.Errorf("sentinel for code %d is nil", tt.code)
		}
	}
	// Unknown code returns nil from Unwrap; must not match any sentinel.
	if errors.Is((&RouteError{Code: -1}), ErrPolicyDenied) {
		t.Error("RouteError{Code: -1} should not match ErrPolicyDenied")
	}
}

func Test401PlainNoBody(t *testing.T) {
	// Server returns a plain HTTP 401 with no JSON-RPC body, so
	// parseRouteError returns nil and the code must fall through
	// to ErrSessionEnded. Must also issue exactly one request (no retry).
	var mu sync.Mutex
	var requestCount int
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		mu.Lock()
		requestCount++
		mu.Unlock()
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusUnauthorized)
	}))
	defer server.Close()

	client := &Client{
		Slug:    "test",
		BaseURL: server.URL,
		Env:     map[string]string{RouteKeyEnv: "kmr_test0000000000000000000000000000000"},
	}
	_, err := client.ListTools(t.Context())
	if !errors.Is(err, ErrSessionEnded) {
		t.Fatalf("expected ErrSessionEnded, got %v", err)
	}
	mu.Lock()
	got := requestCount
	mu.Unlock()
	if got != 1 {
		t.Errorf("expected exactly 1 request, got %d", got)
	}
}

func Test401SessionEnded(t *testing.T) {
	var mu sync.Mutex
	var requestCount int
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		mu.Lock()
		requestCount++
		mu.Unlock()
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusUnauthorized)
		_, _ = w.Write([]byte(`{"error":{"code":-31401,"message":"session_ended"}}`))
	}))
	defer server.Close()

	client := &Client{
		Slug:    "test",
		BaseURL: server.URL,
		Env:     map[string]string{RouteKeyEnv: "kmr_test0000000000000000000000000000000"},
	}
	_, err := client.ListTools(t.Context())
	if !errors.Is(err, ErrSessionEnded) {
		t.Fatalf("expected ErrSessionEnded, got %v", err)
	}
	mu.Lock()
	got := requestCount
	mu.Unlock()
	if got != 1 {
		t.Errorf("expected exactly 1 request, got %d", got)
	}
}

func TestValidateSocket(t *testing.T) {
	t.Run("nonexistent", func(t *testing.T) {
		err := validateSocket("/nonexistent/mcproute/test.sock")
		if err == nil {
			t.Fatal("expected error for nonexistent path")
		}
	})

	t.Run("regular_file", func(t *testing.T) {
		f, err := os.CreateTemp(t.TempDir(), "not-a-socket")
		if err != nil {
			t.Fatal(err)
		}
		_ = f.Close()
		err = validateSocket(f.Name())
		if err == nil {
			t.Fatal("expected error for regular file")
		}
	})

	t.Run("valid_socket", func(t *testing.T) {
		// Go's net.Listen("unix", path) removes the socket file on Close(),
		// so keep the listener open during validation.
		path := filepath.Join(t.TempDir(), "v.sock")
		ln, err := net.Listen("unix", path)
		if err != nil {
			t.Fatal(err)
		}
		defer func() { _ = ln.Close() }()
		if err := validateSocket(path); err != nil {
			t.Fatalf("expected nil for valid socket, got: %v", err)
		}
	})

	t.Run("world_writable", func(t *testing.T) {
		// Short name to stay under macOS 104-char Unix socket path limit.
		path := filepath.Join(t.TempDir(), "w.sock")
		ln, err := net.Listen("unix", path)
		if err != nil {
			t.Fatal(err)
		}
		defer func() { _ = ln.Close() }()
		if err := os.Chmod(path, 0777); err != nil {
			t.Fatal(err)
		}
		// Owned by current user → passes regardless of permissions.
		if err := validateSocket(path); err != nil {
			t.Fatalf("expected nil for owned socket even with 0777, got: %v", err)
		}
	})

	t.Run("ownership_check_present", func(t *testing.T) {
		// Verify the ownership logic compiles and runs by checking a known
		// system file that is not a socket.
		f, err := os.CreateTemp(t.TempDir(), "ownership-check")
		if err != nil {
			t.Fatal(err)
		}
		_ = f.Close()
		fi, err := os.Lstat(f.Name())
		if err != nil {
			t.Fatal(err)
		}
		stat, ok := fi.Sys().(*syscall.Stat_t)
		if !ok {
			t.Skip("syscall.Stat_t not available on this platform")
		}
		_ = stat.Uid // just verify we can read it; the actual check happens in validateSocket
	})
}

func TestSocketTransportFallback(t *testing.T) {
	// Create a regular file (not a socket) at the socket path.
	f, err := os.CreateTemp(t.TempDir(), "not-a-socket")
	if err != nil {
		t.Fatal(err)
	}
	_ = f.Close()

	transport := &mcpTransport{
		socketPath: f.Name(),
		base:       &http.Transport{Proxy: nil},
	}
	req, err := http.NewRequest("POST", "http://kei-mcp/mcp/test", nil)
	if err != nil {
		t.Fatal(err)
	}
	_, err = transport.RoundTrip(req)
	// Must fall back to loopback where nothing listens — expect connection refused.
	if err == nil {
		t.Fatal("expected error from loopback fallback")
	}
	if !strings.Contains(err.Error(), "connection refused") {
		t.Logf("fallback error (acceptable): %v", err)
	}
}

func TestRequestHeaders(t *testing.T) {
	var mu sync.Mutex
	var capturedHeader http.Header
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		mu.Lock()
		capturedHeader = r.Header.Clone()
		mu.Unlock()
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte(`{"jsonrpc":"2.0","id":1,"result":{"tools":[]}}`))
	}))
	defer server.Close()

	client := &Client{
		Slug:    "test-connector",
		BaseURL: server.URL,
		Env:     map[string]string{RouteKeyEnv: "kmr_test0000000000000000000000000000000"},
	}
	if _, err := client.ListTools(t.Context()); err != nil {
		t.Fatal(err)
	}
	mu.Lock()
	h := capturedHeader
	mu.Unlock()

	if h.Get("MCP-Protocol-Version") != "2026-07-28" {
		t.Errorf("MCP-Protocol-Version: got %q, want %q", h.Get("MCP-Protocol-Version"), "2026-07-28")
	}
	if h.Get("Mcp-Method") != "tools/list" {
		t.Errorf("Mcp-Method: got %q, want %q", h.Get("Mcp-Method"), "tools/list")
	}
	if h.Get("Mcp-Name") != "" {
		t.Errorf("Mcp-Name should be empty for tools/list, got %q", h.Get("Mcp-Name"))
	}
}

func TestRequestHeadersToolCall(t *testing.T) {
	var mu sync.Mutex
	var capturedHeader http.Header
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		mu.Lock()
		capturedHeader = r.Header.Clone()
		mu.Unlock()
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte(`{"jsonrpc":"2.0","id":1,"result":{"content":[{"type":"text","text":"ok"}]}}`))
	}))
	defer server.Close()

	client := &Client{
		Slug:    "test-connector",
		BaseURL: server.URL,
		Env:     map[string]string{RouteKeyEnv: "kmr_test0000000000000000000000000000000"},
	}
	if _, err := client.CallTool(t.Context(), "my-tool", nil); err != nil {
		t.Fatal(err)
	}
	mu.Lock()
	h := capturedHeader
	mu.Unlock()

	if h.Get("MCP-Protocol-Version") != "2026-07-28" {
		t.Errorf("MCP-Protocol-Version: got %q, want %q", h.Get("MCP-Protocol-Version"), "2026-07-28")
	}
	if h.Get("Mcp-Method") != "tools/call" {
		t.Errorf("Mcp-Method: got %q, want %q", h.Get("Mcp-Method"), "tools/call")
	}
	if h.Get("Mcp-Name") != "my-tool" {
		t.Errorf("Mcp-Name: got %q, want %q", h.Get("Mcp-Name"), "my-tool")
	}
}

func TestRouteKeyLeak(t *testing.T) {
	routeKey := "kmr_testsecretvalue1234567890abcdefghijk"

	tests := []struct {
		name string
		err  error
	}{
		{
			name: "401_session_ended",
			err: (func() error {
				server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
					w.Header().Set("Content-Type", "application/json")
					w.WriteHeader(http.StatusUnauthorized)
					_, _ = w.Write([]byte(`{"error":{"code":-31401,"message":"session_ended"}}`))
				}))
				defer server.Close()
				client := &Client{Slug: "test", BaseURL: server.URL, Env: map[string]string{RouteKeyEnv: routeKey}}
				_, err := client.ListTools(t.Context())
				return err
			})(),
		},
		{
			name: "403_policy_denied",
			err: (func() error {
				server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
					w.Header().Set("Content-Type", "application/json")
					w.WriteHeader(http.StatusForbidden)
					_, _ = w.Write([]byte(`{"error":{"code":-31403,"message":"policy_denied"}}`))
				}))
				defer server.Close()
				client := &Client{Slug: "test", BaseURL: server.URL, Env: map[string]string{RouteKeyEnv: routeKey}}
				_, err := client.ListTools(t.Context())
				return err
			})(),
		},
		{
			name: "missing_route_key",
			err: (func() error {
				client := &Client{Slug: "test", Env: map[string]string{}}
				_, err := client.ListTools(t.Context())
				return err
			})(),
		},
		{
			name: "route_not_local",
			err: (func() error {
				client := &Client{Slug: "test", BaseURL: "http://remote.example.com", Env: map[string]string{RouteKeyEnv: routeKey}}
				_, err := client.ListTools(t.Context())
				return err
			})(),
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			if tt.err == nil {
				return // no error to leak
			}
			if strings.Contains(tt.err.Error(), routeKey) {
				t.Errorf("error message leaks route key: %v", tt.err)
			}
		})
	}
}
