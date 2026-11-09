package llm

import (
	"encoding/json"
	"os"
	"reflect"
	"testing"
)

func TestAnthropicNativeFixtures(t *testing.T) {
	data, err := os.ReadFile("../../fixtures/llm/anthropic-native/cases.json")
	if err != nil {
		t.Fatal(err)
	}
	var cases []struct {
		Name     string          `json:"name"`
		Response json.RawMessage `json:"response"`
		Tools    []struct {
			Name        string         `json:"name"`
			Description string         `json:"description"`
			InputSchema map[string]any `json:"input_schema"`
		} `json:"tools"`
		ExpectedTools    any `json:"expected_tools"`
		ExpectedMessages any `json:"expected_messages"`
	}
	if err = json.Unmarshal(data, &cases); err != nil {
		t.Fatal(err)
	}
	for _, tc := range cases {
		t.Run(tc.Name, func(t *testing.T) {
			resp, err := ParseAnthropicResponse(tc.Response)
			if err != nil {
				t.Fatal(err)
			}
			if tc.Name == "no thinking" && len(resp.ToolCalls[0].Metadata) != 0 {
				t.Fatal("unexpected metadata")
			}
			if tc.Name != "no thinking" && len(resp.ToolCalls[0].Metadata) == 0 {
				t.Fatal("missing metadata")
			}
			tools := []ToolDefinition{}
			for _, tool := range tc.Tools {
				tools = append(tools, ToolDefinition{Name: tool.Name, Description: tool.Description, InputSchema: tool.InputSchema})
			}
			actualTools := FormatAnthropicTools(tools)
			b, _ := json.Marshal(actualTools)
			var gotTools any
			_ = json.Unmarshal(b, &gotTools)
			if !reflect.DeepEqual(gotTools, tc.ExpectedTools) {
				t.Fatal("tool definitions differ")
			}
			messages := []Message{{Role: RoleAssistant, Content: resp.Content, ToolCalls: resp.ToolCalls}}
			for _, call := range resp.ToolCalls {
				messages = append(messages, Message{Role: RoleTool, ToolCallID: call.ID, Content: "done"})
			}
			msgs, err := FormatAnthropicMessages(messages)
			if err != nil {
				t.Fatal(err)
			}
			b, _ = json.Marshal(msgs)
			var got any
			_ = json.Unmarshal(b, &got)
			if !reflect.DeepEqual(got, tc.ExpectedMessages) {
				t.Fatalf("messages differ from fixture: %s", tc.Name)
			}
		})
	}
}
