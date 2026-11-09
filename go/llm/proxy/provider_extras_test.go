package proxy

import (
	"encoding/json"
	"os"
	"reflect"
	"testing"
)

func TestProviderExtrasThroughProxy(t *testing.T) {
	data, err := os.ReadFile("../../../fixtures/llm/provider-extras/cases.json")
	if err != nil {
		t.Fatal(err)
	}
	var cases []struct {
		Name     string `json:"name"`
		Response struct {
			Choices []struct {
				Message map[string]any `json:"message"`
			} `json:"choices"`
		} `json:"response"`
	}
	if err := json.Unmarshal(data, &cases); err != nil {
		t.Fatal(err)
	}
	for _, tc := range cases {
		t.Run(tc.Name, func(t *testing.T) {
			original := tc.Response.Choices[0].Message
			got := ToOpenAIMessages(ToInternalMessages(MessageList{original}))[0]
			before := original["tool_calls"].([]any)
			after := got["tool_calls"].([]map[string]any)
			for i, raw := range before {
				source := raw.(map[string]any)
				want, present := source["extra_content"]
				value, exists := after[i]["extra_content"]
				if present != exists || !reflect.DeepEqual(value, want) {
					t.Errorf("metadata mismatch at call %d", i)
				}
			}
		})
	}
}
