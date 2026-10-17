package guardrails

import (
	"errors"
	"testing"
)

func TestLoadConfigDefaultsAndValidation(t *testing.T) {
	config, err := LoadConfig([]byte(`{"version":1,"tool_names":["inspect"]}`))
	if err != nil {
		t.Fatal(err)
	}
	if config.MaxPrereqViolations != 2 {
		t.Fatalf("max prerequisite violations = %d, want 2", config.MaxPrereqViolations)
	}
	if config.StepNudgeTier != 1 {
		t.Fatalf("step nudge tier = %d, want 1", config.StepNudgeTier)
	}

	for _, input := range []string{
		`{"version":2,"tool_names":["inspect"]}`,
		`{"version":1,"tool_names":["inspect"],"unexpected":true}`,
		`{"version":1,"tool_names":["inspect"]} {"version":1,"tool_names":["inspect"]}`,
	} {
		if _, err := LoadConfig([]byte(input)); err == nil {
			t.Errorf("LoadConfig(%s) succeeded, want error", input)
		}
	}
}

func TestRecordToolErrorReturnsNudgeWithoutCompletingStep(t *testing.T) {
	config := Config{
		Version:       1,
		ToolNames:     []string{"inspect", "finish"},
		RequiredSteps: []string{"inspect"},
		TerminalTool:  "finish",
		Nudges:        &ConfiguredNudges{Retry: "Please correct the failed tool call."},
	}
	guardrails, err := NewGuardrails(config)
	if err != nil {
		t.Fatal(err)
	}

	result := guardrails.Record("session", ToolCall{Tool: "inspect"}, errors.New("temporary failure"), ErrCategoryTimeout)
	if result.Outcome != OutcomeRecorded {
		t.Fatalf("record outcome = %q, want %q", result.Outcome, OutcomeRecorded)
	}
	if result.Nudge == nil || result.Nudge.Content != "Please correct the failed tool call." {
		t.Fatalf("error nudge = %#v, want configured retry text", result.Nudge)
	}
	if got := guardrails.ErrorTracker().GetErrorCount("session", "inspect"); got != 1 {
		t.Fatalf("tracked error count = %d, want 1", got)
	}

	blocked := guardrails.Check("session", []ToolCall{{Tool: "finish"}})
	if blocked.Outcome != OutcomeStepBlocked {
		t.Fatalf("terminal outcome after failed prerequisite = %q, want %q", blocked.Outcome, OutcomeStepBlocked)
	}
}
