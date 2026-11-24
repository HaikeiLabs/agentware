package guardrails

import (
	"encoding/json"
	"os"
	"reflect"
	"testing"
)

// validatorRescueFixture is the shared response-validator fixture: every port
// replays it so the validators strip the same reasoning tags (see
// reasoning.TagPairs).
const validatorRescueFixture = "../../../fixtures/reasoning/validator-rescue.v1.json"

func TestResponseValidatorSharedReasoningFixture(t *testing.T) {
	data, err := os.ReadFile(validatorRescueFixture)
	if err != nil {
		t.Fatal(err)
	}
	var fx struct {
		ToolNames []string `json:"tool_names"`
		Cases     []struct {
			Name              string     `json:"name"`
			Response          string     `json:"response"`
			ExpectedToolCalls []ToolCall `json:"expected_tool_calls"`
		} `json:"cases"`
	}
	if err := json.Unmarshal(data, &fx); err != nil {
		t.Fatal(err)
	}
	if len(fx.Cases) == 0 {
		t.Fatal("no validator cases")
	}
	for _, c := range fx.Cases {
		c := c
		t.Run(c.Name, func(t *testing.T) {
			rv := NewResponseValidator(fx.ToolNames, true)
			got := rv.ValidateTextResponse(c.Response)
			if len(c.ExpectedToolCalls) == 0 {
				if !got.NeedsRetry || len(got.ToolCalls) != 0 {
					t.Fatalf("want no rescued call, got %+v", got.ToolCalls)
				}
				return
			}
			if got.NeedsRetry {
				t.Fatalf("want rescued calls %+v, got retry", c.ExpectedToolCalls)
			}
			if !reflect.DeepEqual(got.ToolCalls, c.ExpectedToolCalls) {
				t.Fatalf("tool calls = %+v, want %+v", got.ToolCalls, c.ExpectedToolCalls)
			}
		})
	}
}
