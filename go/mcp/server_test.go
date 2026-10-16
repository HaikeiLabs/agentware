package mcp

import (
	"context"
	"encoding/json"
	"strings"
	"testing"

	"github.com/haikeilabs/agentware/go/memory"
	"github.com/haikeilabs/agentware/go/middleware"
	"github.com/haikeilabs/agentware/go/tools"
)

const (
	tboxEducation = "../../ontologies/education/TBOX_LEARNING_SOFTWARE.ttl"
	tboxTopics    = "../../ontologies/social/twitch_topics.ttl"
)

func memoryServer(t *testing.T, user string) *Server {
	t.Helper()
	wiki, err := memory.Enable(memory.Config{
		Root:      t.TempDir(),
		TBoxPaths: []string{tboxEducation, tboxTopics},
	})
	if err != nil {
		t.Skipf("T-box unavailable (run: git submodule update --init): %v", err)
	}
	registry := tools.NewToolRegistry()
	wiki.RegisterTools(registry)
	return NewServer(registry, middleware.CallerContext{
		UserID: user, SessionID: "test", Trusted: true,
	})
}

// drive sends newline-delimited requests and decodes one response per line.
func drive(t *testing.T, s *Server, requests ...string) []map[string]any {
	t.Helper()
	var out strings.Builder
	if err := s.Serve(context.Background(), strings.NewReader(strings.Join(requests, "\n")+"\n"), &out); err != nil {
		t.Fatalf("Serve: %v", err)
	}
	var responses []map[string]any
	for line := range strings.SplitSeq(strings.TrimSpace(out.String()), "\n") {
		if line == "" {
			continue
		}
		var m map[string]any
		if err := json.Unmarshal([]byte(line), &m); err != nil {
			t.Fatalf("bad response line %q: %v", line, err)
		}
		responses = append(responses, m)
	}
	return responses
}

func TestInitializeAndListTools(t *testing.T) {
	s := memoryServer(t, "alice")
	rs := drive(t, s,
		`{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}`,
		`{"jsonrpc":"2.0","method":"notifications/initialized"}`,
		`{"jsonrpc":"2.0","id":2,"method":"tools/list"}`,
	)
	if len(rs) != 2 {
		t.Fatalf("expected 2 responses (notification is silent), got %d", len(rs))
	}
	init := rs[0]["result"].(map[string]any)
	if init["protocolVersion"] != ProtocolVersion {
		t.Errorf("protocolVersion = %v", init["protocolVersion"])
	}
	list := rs[1]["result"].(map[string]any)["tools"].([]any)
	if len(list) != 5 {
		t.Fatalf("expected 5 memory tools, got %d", len(list))
	}
	names := map[string]bool{}
	for _, tool := range list {
		names[tool.(map[string]any)["name"].(string)] = true
	}
	for _, want := range memory.Tools() {
		if !names[want] {
			t.Errorf("missing tool %s in tools/list", want)
		}
	}
}

func TestCallToolWriteAndQueryRoundTrip(t *testing.T) {
	s := memoryServer(t, "alice")
	page := "---\nid: go-worker-pools\ntype: sw:Skill\nlabels: [\"Worker Pools\"]\n---\nBody.\n"
	writeReq, _ := json.Marshal(map[string]any{
		"jsonrpc": "2.0", "id": 1, "method": "tools/call",
		"params": map[string]any{
			"name":      memory.ToolWritePage,
			"arguments": map[string]any{"content": page},
		},
	})
	rs := drive(t, s,
		string(writeReq),
		`{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"memory_query","arguments":{"question":"worker"}}}`,
	)
	write := rs[0]["result"].(map[string]any)
	if write["isError"] == true {
		t.Fatalf("write errored: %v", write)
	}
	query := rs[1]["result"].(map[string]any)
	text := query["content"].([]any)[0].(map[string]any)["text"].(string)
	if !strings.Contains(text, "go-worker-pools") {
		t.Errorf("query result missing page: %s", text)
	}
}

func TestCallToolDenyIsToolError(t *testing.T) {
	s := memoryServer(t, "alice")
	bad, _ := json.Marshal(map[string]any{
		"jsonrpc": "2.0", "id": 1, "method": "tools/call",
		"params": map[string]any{
			"name":      memory.ToolWritePage,
			"arguments": map[string]any{"content": "---\nid: p-x\ntype: sw:Skil\n---\n"},
		},
	})
	rs := drive(t, s, string(bad))
	result := rs[0]["result"].(map[string]any)
	if result["isError"] != true {
		t.Fatalf("policy deny must surface as tool error, got %v", result)
	}
	text := result["content"].([]any)[0].(map[string]any)["text"].(string)
	if !strings.Contains(text, "diagnostics: ") || !strings.Contains(text, "unknown-class") {
		t.Errorf("deny text must carry diagnostics, got %s", text)
	}
}

func TestUnknownMethodAndTool(t *testing.T) {
	s := memoryServer(t, "alice")
	rs := drive(t, s,
		`{"jsonrpc":"2.0","id":1,"method":"bogus/method"}`,
		`{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"bogus_tool","arguments":{}}}`,
	)
	if rs[0]["error"] == nil || rs[1]["error"] == nil {
		t.Errorf("expected rpc errors, got %v", rs)
	}
}
