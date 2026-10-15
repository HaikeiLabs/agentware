// Package mcproute implements an MCP route client that speaks JSON-RPC 2.0
// to the kei-proxy /mcp/{slug} endpoint (ADR-036 §4.5). It enforces a
// metadata-only control-plane boundary: provider payloads, customer content,
// and credentials never cross this client.
//
// Transport selection (tried in order):
//  1. Unix socket at KEI_RUNTIME_SOCKET_PATH (default /run/kei-proxy/runtime.sock)
//  2. TCP 127.0.0.1:8080
//  3. TCP [::1]:8080
//  4. Any other host -> RouteNotLocal error
//
// Authentication uses KEI_MCP_ROUTE_KEY read from the environment on every
// request; the value is never cached or persisted.
//
// Usage:
//
//	client := mcproute.NewClient("github-connector")
//	tools, err := client.ListTools(ctx)
//	result, err := client.CallTool(ctx, "get_issue", map[string]any{"id": "42"})
package mcproute
