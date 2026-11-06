package llm

import (
	"context"
	"testing"
)

// TestServerBackend_Complete_DecodesNativeToolCalls checks that the OpenAI
// tool_calls shape (name and arguments nested under function, arguments as a
// JSON string or an object) decodes into ToolCall with its ID, name and args.
func TestServerBackend_Complete_DecodesNativeToolCalls(t *testing.T) {
	backend := newTestBackend(t, `{"choices":[{"message":{"role":"assistant","content":null,"tool_calls":[`+
		`{"id":"call_1","type":"function","function":{"name":"hn_top_stories","arguments":"{\"limit\":5}"}},`+
		`{"id":"call_2","type":"function","function":{"name":"echo","arguments":{"text":"hi"}}},`+
		`{"id":"call_3","type":"function","function":{"name":"no_args","arguments":""}}`+
		`]},"finish_reason":"tool_calls"}]}`)

	resp, err := backend.Complete(context.Background(), &Request{Messages: []Message{{Role: RoleUser, Content: "go"}}})
	if err != nil {
		t.Fatalf("Complete: %v", err)
	}
	if len(resp.ToolCalls) != 3 {
		t.Fatalf("expected 3 tool calls, got %d", len(resp.ToolCalls))
	}

	first := resp.ToolCalls[0]
	if first.ID != "call_1" || first.Name != "hn_top_stories" {
		t.Errorf("call 1: got id=%q name=%q", first.ID, first.Name)
	}
	if got, ok := first.Args["limit"].(float64); !ok || got != 5 {
		t.Errorf("call 1: expected limit=5 from string arguments, got %v", first.Args)
	}

	second := resp.ToolCalls[1]
	if second.ID != "call_2" || second.Name != "echo" || second.Args["text"] != "hi" {
		t.Errorf("call 2: expected echo{text:hi} from object arguments, got %+v", second)
	}

	third := resp.ToolCalls[2]
	if third.Name != "no_args" || third.Args == nil || len(third.Args) != 0 {
		t.Errorf("call 3: expected empty non-nil args, got %+v", third)
	}
}

// TestServerBackend_Complete_MalformedToolCallArguments fails the completion
// instead of running a tool with arguments that are not a JSON object.
func TestServerBackend_Complete_MalformedToolCallArguments(t *testing.T) {
	backend := newTestBackend(t, `{"choices":[{"message":{"role":"assistant","tool_calls":[`+
		`{"id":"call_1","type":"function","function":{"name":"echo","arguments":"not json"}}`+
		`]},"finish_reason":"tool_calls"}]}`)

	if _, err := backend.Complete(context.Background(), &Request{Messages: []Message{{Role: RoleUser, Content: "go"}}}); err == nil {
		t.Fatal("expected an error for malformed tool call arguments, got nil")
	}
}
