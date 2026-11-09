package llm

import (
	"encoding/json"
	"os"
	"reflect"
	"testing"
)

func TestProviderExtrasFixtures(t *testing.T) {
	raw, err := os.ReadFile("../../fixtures/llm/provider-extras/cases.json")
	if err != nil {
		t.Fatal(err)
	}
	var cases []struct {
		Name     string `json:"name"`
		Response struct {
			Choices []struct {
				Message struct {
					ToolCalls []wireResponseToolCall `json:"tool_calls"`
				} `json:"message"`
			} `json:"choices"`
		} `json:"response"`
		Expected struct {
			ToolCalls any `json:"tool_calls"`
		} `json:"expected"`
	}
	if err = json.Unmarshal(raw, &cases); err != nil {
		t.Fatal(err)
	}
	for _, tc := range cases {
		t.Run(tc.Name, func(t *testing.T) {
			calls, err := fromWireToolCalls(tc.Response.Choices[0].Message.ToolCalls)
			if err != nil {
				t.Fatal(err)
			}
			msgs, err := toWireMessages([]Message{{Role: RoleAssistant, ToolCalls: calls}})
			if err != nil {
				t.Fatal(err)
			}
			data, err := json.Marshal(msgs[0].ToolCalls)
			if err != nil {
				t.Fatal(err)
			}
			var got any
			if err = json.Unmarshal(data, &got); err != nil {
				t.Fatal(err)
			}
			if !reflect.DeepEqual(got, tc.Expected.ToolCalls) {
				t.Errorf("tool calls differ from fixture")
			}
		})
	}
}
