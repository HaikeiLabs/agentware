package mcp

import (
	"bufio"
	"context"
	"encoding/json"
	"fmt"
	"io"

	"github.com/haikeilabs/agentware/go/middleware"
	"github.com/haikeilabs/agentware/go/tools"
)

// Server serves a tool registry over MCP. Every tools/call runs with the
// configured CallerContext attached, so downstream middleware and executors
// see a stable principal for the life of the process.
type Server struct {
	registry *tools.ToolRegistry
	caller   middleware.CallerContext
	name     string
	version  string
}

// NewServer builds a server for registry, executing calls as caller.
func NewServer(registry *tools.ToolRegistry, caller middleware.CallerContext) *Server {
	return &Server{
		registry: registry,
		caller:   caller,
		name:     "pedro-agentware",
		version:  "0.1.0",
	}
}

// Serve reads newline-delimited JSON-RPC requests from r and writes
// responses to w until EOF. Notifications (no id) get no response.
func (s *Server) Serve(ctx context.Context, r io.Reader, w io.Writer) error {
	scanner := bufio.NewScanner(r)
	scanner.Buffer(make([]byte, 0, 64*1024), 16*1024*1024)
	enc := json.NewEncoder(w)
	for scanner.Scan() {
		line := scanner.Bytes()
		if len(line) == 0 {
			continue
		}
		resp := s.handle(ctx, line)
		if resp == nil {
			continue
		}
		if err := enc.Encode(resp); err != nil {
			return fmt.Errorf("mcp: write response: %w", err)
		}
	}
	return scanner.Err()
}

func (s *Server) handle(ctx context.Context, raw []byte) *response {
	var req request
	if err := json.Unmarshal(raw, &req); err != nil {
		return &response{JSONRPC: "2.0", Error: &rpcError{Code: codeParse, Message: err.Error()}}
	}
	if req.JSONRPC != "2.0" || req.Method == "" {
		return &response{JSONRPC: "2.0", ID: req.ID,
			Error: &rpcError{Code: codeInvalidRequest, Message: "expected jsonrpc 2.0 request"}}
	}
	isNotification := len(req.ID) == 0
	var (
		result any
		rerr   *rpcError
	)
	switch req.Method {
	case "initialize":
		result = initializeResult{
			ProtocolVersion: ProtocolVersion,
			Capabilities:    map[string]any{"tools": map[string]any{}},
			ServerInfo:      serverInfo{Name: s.name, Version: s.version},
		}
	case "ping":
		result = map[string]any{}
	case "notifications/initialized":
		return nil
	case "tools/list":
		result = s.listTools()
	case "tools/call":
		result, rerr = s.callTool(ctx, req.Params)
	default:
		rerr = &rpcError{Code: codeMethodNotFound, Message: "unknown method " + req.Method}
	}
	if isNotification {
		return nil
	}
	if rerr != nil {
		return &response{JSONRPC: "2.0", ID: req.ID, Error: rerr}
	}
	return &response{JSONRPC: "2.0", ID: req.ID, Result: result}
}

func (s *Server) listTools() listToolsResult {
	out := listToolsResult{Tools: []toolDescriptor{}}
	schemas := s.registry.Schemas()
	for _, t := range s.registry.All() {
		schema, ok := schemas[t.Name()]
		if !ok {
			schema = map[string]any{"type": "object"}
		}
		out.Tools = append(out.Tools, toolDescriptor{
			Name:        t.Name(),
			Description: t.Description(),
			InputSchema: schema,
		})
	}
	return out
}

func (s *Server) callTool(ctx context.Context, params json.RawMessage) (any, *rpcError) {
	var p callToolParams
	if err := json.Unmarshal(params, &p); err != nil {
		return nil, &rpcError{Code: codeInvalidParams, Message: err.Error()}
	}
	tool, ok := s.registry.Get(p.Name)
	if !ok {
		return nil, &rpcError{Code: codeInvalidParams, Message: "unknown tool " + p.Name}
	}
	if p.Arguments == nil {
		p.Arguments = map[string]any{}
	}
	ctx = middleware.WithCallerContext(ctx, s.caller)
	res, err := tool.Execute(ctx, p.Arguments)
	if err != nil {
		return nil, &rpcError{Code: codeInternal, Message: err.Error()}
	}
	if !res.Success {
		return callToolResult{
			Content: []contentBlock{{Type: "text", Text: res.Error}},
			IsError: true,
		}, nil
	}
	return callToolResult{
		Content: []contentBlock{{Type: "text", Text: res.Output}},
	}, nil
}
