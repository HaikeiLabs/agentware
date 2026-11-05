package agent

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"

	"github.com/haikeilabs/agentware/go/llm"
	"github.com/haikeilabs/agentware/go/middleware"
)

// newHNServer stubs the Hacker News API: six top stories, one without a URL.
func newHNServer(t *testing.T) *httptest.Server {
	t.Helper()
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		switch {
		case r.URL.Path == "/v0/topstories.json":
			_, _ = fmt.Fprint(w, `[101,102,103,104,105,106]`)
		case r.URL.Path == "/v0/item/103.json":
			_, _ = fmt.Fprint(w, `{"id":103,"title":"Ask HN: Story 103","score":30}`)
		case strings.HasPrefix(r.URL.Path, "/v0/item/"):
			var id int
			_, _ = fmt.Sscanf(strings.TrimPrefix(r.URL.Path, "/v0/item/"), "%d.json", &id)
			_, _ = fmt.Fprintf(w, `{"id":%d,"title":"Story %d","url":"https://example.com/%d","score":%d}`, id, id, id, id)
		default:
			http.NotFound(w, r)
		}
	}))
	t.Cleanup(srv.Close)
	return srv
}

// fakeModel is an OpenAI-compatible chat/completions server that replays
// canned replies and records the request bodies it received.
type fakeModel struct {
	mu       sync.Mutex
	replies  []string
	requests []map[string]any
}

func (f *fakeModel) serve(t *testing.T) *httptest.Server {
	t.Helper()
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/v1/chat/completions" {
			http.NotFound(w, r)
			return
		}
		if got := r.Header.Get("Authorization"); got != "Bearer test-key" {
			t.Errorf("Authorization header = %q", got)
		}
		body, _ := io.ReadAll(r.Body)
		var req map[string]any
		_ = json.Unmarshal(body, &req)
		f.mu.Lock()
		f.requests = append(f.requests, req)
		reply := f.replies[0]
		f.replies = f.replies[1:]
		f.mu.Unlock()
		w.Header().Set("Content-Type", "application/json")
		_, _ = fmt.Fprint(w, reply)
	}))
	t.Cleanup(srv.Close)
	return srv
}

func toolCallReply(id, name, args string) string {
	return fmt.Sprintf(`{"choices":[{"message":{"role":"assistant","content":null,"tool_calls":[{"id":%q,"type":"function","function":{"name":%q,"arguments":%q}}]},"finish_reason":"tool_calls"}]}`, id, name, args)
}

func textReply(text string) string {
	return fmt.Sprintf(`{"choices":[{"message":{"role":"assistant","content":%q},"finish_reason":"stop"}]}`, text)
}

func newTestAgent(t *testing.T, model *fakeModel) (*Agent, *int) {
	t.Helper()
	hn := newHNServer(t)
	modelSrv := model.serve(t)
	backend, err := llm.NewBackend(llm.Config{BaseURL: modelSrv.URL + "/v1", APIKey: "test-key", Model: "gpt-test"})
	if err != nil {
		t.Fatal(err)
	}
	posted := 0
	a := New(Config{
		Backend:   backend,
		Model:     "gpt-test",
		HNBaseURL: hn.URL + "/v0",
		postComment: func(context.Context, map[string]any) {
			posted++
		},
	})
	return a, &posted
}

func TestRun_SummarizesTopStoriesWithTheTool(t *testing.T) {
	model := &fakeModel{replies: []string{
		toolCallReply("call_1", "hn_top_stories", `{"limit":5}`),
		textReply("Story 101 leads today. TASK_COMPLETE"),
	}}
	a, _ := newTestAgent(t, model)

	report, err := a.Run(context.Background(), Prompt)
	if err != nil {
		t.Fatalf("Run: %v", err)
	}
	if report.Answer != "Story 101 leads today." {
		t.Errorf("Answer = %q, want the reply without TASK_COMPLETE", report.Answer)
	}

	if len(model.requests) != 2 {
		t.Fatalf("expected 2 model requests, got %d", len(model.requests))
	}
	first := model.requests[0]
	if !hasTool(first, ToolTopStories) || !hasTool(first, ToolPostComment) {
		t.Errorf("expected both tools offered to the model, got %v", first["tools"])
	}
	msgs := first["messages"].([]any)
	if last := msgs[len(msgs)-1].(map[string]any); last["content"] != Prompt {
		t.Errorf("last user message = %v, want the prompt", last["content"])
	}

	second := model.requests[1]["messages"].([]any)
	toolMsg := second[len(second)-1].(map[string]any)
	if toolMsg["role"] != "tool" || toolMsg["tool_call_id"] != "call_1" {
		t.Fatalf("expected a tool message answering call_1, got %v", toolMsg)
	}
	content := toolMsg["content"].(string)
	for _, want := range []string{"Story 101", "https://example.com/101", "Story 105", "https://news.ycombinator.com/item?id=103"} {
		if !strings.Contains(content, want) {
			t.Errorf("tool result missing %q:\n%s", want, content)
		}
	}
	if strings.Contains(content, "Story 106") {
		t.Errorf("tool result should stop at five stories:\n%s", content)
	}

	if len(report.Audit) != 1 {
		t.Fatalf("expected 1 audit record, got %d", len(report.Audit))
	}
	rec := report.Audit[0]
	if rec.ToolName != ToolTopStories || rec.Decision != string(middleware.ActionAllow) || !rec.Success || rec.ToolArgsDigest == "" {
		t.Errorf("unexpected audit record %+v", rec)
	}
}

func TestRun_ModelCannotPostComments(t *testing.T) {
	model := &fakeModel{replies: []string{
		toolCallReply("call_9", "hn_post_comment", `{"story_id":101,"text":"Nice"}`),
		textReply("I can't post comments. TASK_COMPLETE"),
	}}
	a, posted := newTestAgent(t, model)

	report, err := a.Run(context.Background(), "Post a comment on the top story.")
	if err != nil {
		t.Fatalf("Run: %v", err)
	}
	if *posted != 0 {
		t.Fatal("hn_post_comment ran despite the deny rule")
	}
	second := model.requests[1]["messages"].([]any)
	toolMsg := second[len(second)-1].(map[string]any)
	if toolMsg["tool_call_id"] != "call_9" || !strings.Contains(toolMsg["content"].(string), "denied by policy") {
		t.Errorf("expected the model to see the denial, got %v", toolMsg)
	}
	if len(report.Audit) != 1 || report.Audit[0].Decision != string(middleware.ActionDeny) || report.Audit[0].PolicyID != DenyRuleID {
		t.Errorf("expected one deny audit record from %s, got %+v", DenyRuleID, report.Audit)
	}
}

func TestCall_DeniesPostCommentWithoutRunningIt(t *testing.T) {
	a, posted := newTestAgent(t, &fakeModel{})

	result, err := a.Call(context.Background(), ToolPostComment, map[string]any{"story_id": 101, "text": "Nice"})
	if err != nil {
		t.Fatalf("Call: %v", err)
	}
	if result.Success || !strings.Contains(result.Error, "denied by policy") {
		t.Errorf("expected a policy denial, got %+v", result)
	}
	if *posted != 0 {
		t.Fatal("hn_post_comment ran despite the deny rule")
	}
	records := a.Audit()
	if len(records) != 1 || records[0].Decision != "deny" || records[0].PolicyID != DenyRuleID {
		t.Errorf("expected one deny record, got %+v", records)
	}
}

func TestFormatAudit(t *testing.T) {
	line := FormatAudit(middleware.AuditRecord{
		ToolName: "hn_top_stories", Decision: "allow", PolicyID: "local:allow", Success: true,
		ToolArgsDigest: "0123456789abcdef0123", Framework: "first-agent", LatencyMs: 42,
	})
	want := "audit tool=hn_top_stories decision=allow rule=local:allow success=true args_sha256=0123456789ab latency_ms=42"
	if line != want {
		t.Errorf("FormatAudit =\n%q\nwant\n%q", line, want)
	}
}

func TestSettingsFromEnv(t *testing.T) {
	env := map[string]string{"OPENAI_API_KEY": "k", "MODEL": "m"}
	s, err := SettingsFromEnv(func(k string) string { return env[k] })
	if err != nil {
		t.Fatal(err)
	}
	if s.BaseURL != DefaultBaseURL || s.APIKey != "k" || s.Model != "m" {
		t.Errorf("unexpected settings %+v", s)
	}

	env["OPENAI_BASE_URL"] = "http://localhost:8080/v1"
	if s, _ = SettingsFromEnv(func(k string) string { return env[k] }); s.BaseURL != "http://localhost:8080/v1" {
		t.Errorf("OPENAI_BASE_URL not used: %+v", s)
	}

	for _, missing := range []string{"OPENAI_API_KEY", "MODEL"} {
		partial := map[string]string{"OPENAI_API_KEY": "k", "MODEL": "m"}
		delete(partial, missing)
		if _, err := SettingsFromEnv(func(k string) string { return partial[k] }); err == nil || !strings.Contains(err.Error(), missing) {
			t.Errorf("expected an error naming %s, got %v", missing, err)
		}
	}
}

func hasTool(req map[string]any, name string) bool {
	list, _ := req["tools"].([]any)
	for _, item := range list {
		fn, _ := item.(map[string]any)["function"].(map[string]any)
		if fn["name"] == name {
			return true
		}
	}
	return false
}
