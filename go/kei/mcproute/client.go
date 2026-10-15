package mcproute

import (
	"bufio"
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/url"
	"os"
	"strings"
	"sync"
	"sync/atomic"
	"syscall"
	"time"
)

// Defaults and environment variable names.
const (
	DefaultSocketPath   = "/run/kei-proxy/runtime.sock"
	SocketPathEnv       = "KEI_RUNTIME_SOCKET_PATH"
	RouteKeyEnv         = "KEI_MCP_ROUTE_KEY"
	DefaultLoopbackPort = "8080"
	MCPRoutePrefix      = "/mcp/"

	DefaultTimeout    = 10 * time.Second
	DefaultSSETimeout = 120 * time.Second

	maxResponseBytes = 8 << 20
)

// Client talks to the kei-proxy MCP route over its Unix socket or loopback
// TCP. The zero value is not usable directly — use NewClient. It is safe for
// concurrent use.
type Client struct {
	// BaseURL, when set, bypasses socket/loopback discovery and sends requests
	// to this URL directly. Used in tests with httptest.
	BaseURL string
	// SocketPath is the kei-proxy Unix socket. Default: KEI_RUNTIME_SOCKET_PATH
	// (read from Env), then DefaultSocketPath.
	SocketPath string
	// Slug is the MCP connector slug (e.g. "github-connector"). Required.
	Slug string
	// Timeout bounds JSON-RPC calls. Default DefaultTimeout.
	Timeout time.Duration
	// SSETimeout bounds each line of a streamed SSE response. Default DefaultSSETimeout.
	SSETimeout time.Duration
	// Env is where KEI_MCP_ROUTE_KEY and KEI_RUNTIME_SOCKET_PATH are read.
	// Nil means os.Getenv. The route key is read on every request.
	Env map[string]string

	once sync.Once
	pool *http.Client // lazy, set by client()
}

// NewClient creates an MCP route client for the given connector slug.
func NewClient(slug string) *Client {
	return &Client{Slug: slug}
}

func (c *Client) lookup(key string) string {
	if c.Env != nil {
		return c.Env[key]
	}
	return os.Getenv(key)
}

func (c *Client) sseTimeout() time.Duration {
	if c.SSETimeout > 0 {
		return c.SSETimeout
	}
	return DefaultSSETimeout
}

// resolvedSocketPath returns the Unix socket path or "" if not configured.
func (c *Client) resolvedSocketPath() string {
	if c.SocketPath != "" {
		return c.SocketPath
	}
	return c.lookup(SocketPathEnv)
}

// client returns the HTTP client, initialising it once. When BaseURL is set,
// a direct HTTP transport is used; a non-loopback BaseURL is rejected with
// ErrRouteNotLocal. Otherwise, the transport chain tries Unix socket then
// loopback TCP.
func (c *Client) client() *http.Client {
	c.once.Do(func() {
		if c.BaseURL != "" {
			u, err := url.Parse(c.BaseURL)
			if err == nil && u.Hostname() != "" && !isLoopback(u.Hostname()) && u.Scheme != "unix" {
				c.pool = nil // marker: BaseURL rejected
				return
			}
			c.pool = &http.Client{
				Transport:     &http.Transport{Proxy: nil},
				CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse },
			}
			return
		}
		c.pool = &http.Client{
			Transport: &mcpTransport{
				socketPath: c.resolvedSocketPath(),
				base:       &http.Transport{Proxy: nil, MaxIdleConns: 4, IdleConnTimeout: 30 * time.Second},
			},
			CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse },
			Timeout:       0, // per-request deadlines set by context
		}
	})
	return c.pool
}

// mcpTransport tries Unix socket first; if absent, it falls back to loopback
// TCP. The URL host is a placeholder (always "kei-mcp") and is never dialled.
type mcpTransport struct {
	socketPath string
	base       *http.Transport
}

func (t *mcpTransport) RoundTrip(req *http.Request) (*http.Response, error) {
	// Try Unix socket first — but only if it passes validation.
	if t.socketPath != "" {
		if err := validateSocket(t.socketPath); err == nil {
			ut := t.base.Clone()
			ut.DialContext = func(ctx context.Context, _, _ string) (net.Conn, error) {
				var d net.Dialer
				return d.DialContext(ctx, "unix", t.socketPath)
			}
			r := req.Clone(req.Context())
			r.URL.Host = "unix"
			resp, err := ut.RoundTrip(r)
			if err == nil {
				return resp, nil
			}
		}
	}
	// Fall back to loopback TCP — try IPv4 then IPv6.
	ut := t.base.Clone()
	ut.DialContext = func(ctx context.Context, network, addr string) (net.Conn, error) {
		var d net.Dialer
		conn, err := d.DialContext(ctx, "tcp", net.JoinHostPort("127.0.0.1", portFromAddr(addr)))
		if err != nil {
			conn, err = d.DialContext(ctx, "tcp", net.JoinHostPort("::1", portFromAddr(addr)))
		}
		return conn, err
	}
	return ut.RoundTrip(req)
}

// validateSocket checks that path exists, is a Unix socket, and is either
// owned by the current user or not group/world-writable. Returns nil if the
// socket is safe to use, or an error describing why it was rejected.
func validateSocket(path string) error {
	fi, err := os.Lstat(path)
	if err != nil {
		return fmt.Errorf("socket stat: %w", err)
	}
	if fi.Mode()&os.ModeSocket == 0 {
		return fmt.Errorf("not a socket (mode %o)", fi.Mode()&os.ModePerm)
	}
	stat, ok := fi.Sys().(*syscall.Stat_t)
	if !ok {
		return fmt.Errorf("unsupported file info type %T", fi.Sys())
	}
	uid := os.Getuid()
	if stat.Uid == uint32(uid) {
		return nil // owned by current user — always ok
	}
	// Not owned by us: reject if group or world writable.
	if fi.Mode()&0022 != 0 {
		return fmt.Errorf("socket owned by uid %d but is group/world writable", stat.Uid)
	}
	return nil
}

func isLoopback(host string) bool {
	ip := net.ParseIP(host)
	return ip != nil && ip.IsLoopback()
}

func portFromAddr(addr string) string {
	_, port, err := net.SplitHostPort(addr)
	if err != nil {
		return DefaultLoopbackPort
	}
	return port
}

// ListTools calls tools/list and returns the tool descriptors.
func (c *Client) ListTools(ctx context.Context) ([]ToolDescriptor, error) {
	body, err := c.call(ctx, "tools/list", "", nil)
	if err != nil {
		return nil, err
	}
	var result struct {
		Tools []ToolDescriptor `json:"tools"`
	}
	if err := json.Unmarshal(body, &result); err != nil {
		return nil, fmt.Errorf("mcp route: decode tools/list result: %w", err)
	}
	return result.Tools, nil
}

// CallTool calls tools/call and returns the tool result.
func (c *Client) CallTool(ctx context.Context, name string, args map[string]any) (*ToolResult, error) {
	params := map[string]any{"name": name, "arguments": args}
	resp, err := c.call(ctx, "tools/call", name, params)
	if err != nil {
		return nil, err
	}
	var result ToolResult
	if err := json.Unmarshal(resp, &result); err != nil {
		return nil, fmt.Errorf("mcp route: decode tools/call result: %w", err)
	}
	return &result, nil
}

// call sends a JSON-RPC request to the MCP route and returns the raw result
// JSON (the "result" field), or a *RouteError for JSON-RPC errors. toolName
// is set for "tools/call" to populate the Mcp-Name header.
func (c *Client) call(ctx context.Context, method, toolName string, params any) (json.RawMessage, error) {
	routeKey := c.lookup(RouteKeyEnv)
	if routeKey == "" {
		return nil, fmt.Errorf("mcp route: %s is not set", RouteKeyEnv)
	}

	id := time.Now().UnixMilli()
	reqBody, err := json.Marshal(map[string]any{
		"jsonrpc": "2.0",
		"id":      id,
		"method":  method,
		"params":  params,
	})
	if err != nil {
		return nil, fmt.Errorf("mcp route: encode request: %w", err)
	}

	target := buildURL(c.Slug)
	if c.BaseURL != "" {
		target = c.BaseURL + MCPRoutePrefix + url.PathEscape(c.Slug)
	}
	httpReq, err := http.NewRequestWithContext(ctx, http.MethodPost, target, bytes.NewReader(reqBody))
	if err != nil {
		return nil, fmt.Errorf("mcp route: build request: %w", err)
	}
	httpReq.Header.Set("Authorization", "Bearer "+routeKey)
	httpReq.Header.Set("Content-Type", "application/json")
	httpReq.Header.Set("MCP-Protocol-Version", "2026-07-28")
	httpReq.Header.Set("Mcp-Method", method)
	if toolName != "" {
		httpReq.Header.Set("Mcp-Name", toolName)
	}

	client := c.client()
	if client == nil {
		return nil, ErrRouteNotLocal
	}
	httpResp, err := client.Do(httpReq)
	if err != nil {
		if errors.Is(err, ErrRouteNotLocal) {
			return nil, err
		}
		return nil, fmt.Errorf("mcp route: transport: %w", err)
	}
	defer func() { _ = httpResp.Body.Close() }()

	if httpResp.StatusCode == http.StatusUnauthorized {
		// 401: key expired or revoked — never retry, never fall back.
		body, _ := io.ReadAll(io.LimitReader(httpResp.Body, maxResponseBytes))
		if re := parseRouteError(body); re != nil {
			return nil, re
		}
		return nil, ErrSessionEnded
	}
	if httpResp.StatusCode == http.StatusForbidden {
		body, _ := io.ReadAll(io.LimitReader(httpResp.Body, maxResponseBytes))
		if re := parseRouteError(body); re != nil {
			return nil, re
		}
		return nil, fmt.Errorf("mcp route: HTTP %d", httpResp.StatusCode)
	}

	if isEventStream(httpResp) {
		return c.readSSE(ctx, httpResp.Body, id)
	}

	raw, err := io.ReadAll(io.LimitReader(httpResp.Body, maxResponseBytes))
	if err != nil {
		return nil, fmt.Errorf("mcp route: read response: %w", err)
	}

	if httpResp.StatusCode < 200 || httpResp.StatusCode > 299 {
		if re := parseRouteError(raw); re != nil {
			return nil, re
		}
		return nil, fmt.Errorf("mcp route: HTTP %d", httpResp.StatusCode)
	}

	// Decode JSON-RPC envelope.
	var rpc struct {
		JSONRPC string          `json:"jsonrpc"`
		Result  json.RawMessage `json:"result"`
		Error   *struct {
			Code    int    `json:"code"`
			Message string `json:"message"`
			Data    *struct {
				Reason     string `json:"reason"`
				DecisionID string `json:"decision_id"`
			} `json:"data"`
		} `json:"error"`
	}
	if err := json.Unmarshal(raw, &rpc); err != nil {
		return nil, fmt.Errorf("mcp route: decode response: %w", err)
	}
	if rpc.Error != nil {
		return nil, &RouteError{
			Code:       rpc.Error.Code,
			Message:    rpc.Error.Message,
			Reason:     rpc.Error.Data.Reason,
			DecisionID: rpc.Error.Data.DecisionID,
		}
	}
	return rpc.Result, nil
}

// readSSE reads a text/event-stream until it finds a JSON-RPC response event
// (an event with an "id" field), then returns its data.
func (c *Client) readSSE(ctx context.Context, body io.Reader, expectedID int64) (json.RawMessage, error) {
	idle := c.sseTimeout()
	_, cancel := context.WithCancel(context.Background())
	defer cancel()

	var timedOut atomic.Bool
	timer := time.AfterFunc(idle, func() {
		timedOut.Store(true)
		cancel()
	})
	defer timer.Stop()

	scanner := bufio.NewScanner(body)
	scanner.Buffer(make([]byte, 0, 64<<10), maxResponseBytes)

	var dataBuf bytes.Buffer
	for {
		timer.Reset(idle)
		if !scanner.Scan() {
			break
		}
		line := scanner.Text()
		if strings.HasPrefix(line, "data:") {
			dataBuf.WriteString(strings.TrimSpace(line[5:]))
		} else if line == "" {
			// End of event.
			if dataBuf.Len() > 0 {
				var msg struct {
					ID     any              `json:"id"`
					Result json.RawMessage  `json:"result"`
					Error  *json.RawMessage `json:"error"`
				}
				if err := json.Unmarshal(dataBuf.Bytes(), &msg); err == nil && msg.ID != nil {
					return msg.Result, nil
				}
				dataBuf.Reset()
			}
		}
	}
	if err := scanner.Err(); err != nil {
		if timedOut.Load() {
			return nil, fmt.Errorf("mcp route: sse timeout")
		}
		return nil, fmt.Errorf("mcp route: sse read: %w", err)
	}
	return nil, fmt.Errorf("mcp route: sse stream ended without a response")
}

func isEventStream(resp *http.Response) bool {
	ct := resp.Header.Get("Content-Type")
	return strings.Contains(ct, "text/event-stream")
}

// parseRouteError attempts to parse a JSON-RPC error response from raw body.
func parseRouteError(raw []byte) *RouteError {
	var rpc struct {
		Error *struct {
			Code    int    `json:"code"`
			Message string `json:"message"`
			Data    *struct {
				Reason     string `json:"reason"`
				DecisionID string `json:"decision_id"`
			} `json:"data"`
		} `json:"error"`
	}
	if json.Unmarshal(raw, &rpc) != nil || rpc.Error == nil {
		return nil
	}
	re := &RouteError{Code: rpc.Error.Code, Message: rpc.Error.Message}
	if rpc.Error.Data != nil {
		re.Reason = rpc.Error.Data.Reason
		re.DecisionID = rpc.Error.Data.DecisionID
	}
	return re
}

func buildURL(slug string) string {
	return "http://kei-mcp" + MCPRoutePrefix + url.PathEscape(slug)
}

// Types matching the MCP protocol (ADR-036 / go/mcp/protocol.go).

// ToolDescriptor describes an MCP tool.
type ToolDescriptor struct {
	Name        string         `json:"name"`
	Description string         `json:"description"`
	InputSchema map[string]any `json:"inputSchema"`
}

// ToolResult is the response from tools/call.
type ToolResult struct {
	Content []ContentBlock `json:"content"`
	IsError bool           `json:"isError,omitempty"`
}

// ContentBlock is a single piece of content in a tool result.
type ContentBlock struct {
	Type string `json:"type"`
	Text string `json:"text"`
}
