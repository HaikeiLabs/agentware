package guardrails

import (
	"encoding/json"
	"errors"
	"os"
	"reflect"
	"testing"
)

type replayContract struct {
	Version int          `json:"version"`
	Cases   []replayCase `json:"cases"`
}

type replayCase struct {
	Name   string          `json:"name"`
	Config json.RawMessage `json:"config"`
	Input  struct {
		Response struct {
			ToolCalls []ToolCall `json:"tool_calls"`
		} `json:"response"`
		Record *struct {
			Tool     string `json:"tool"`
			Error    string `json:"error"`
			Category string `json:"category"`
		} `json:"record"`
		State struct {
			CompletedSteps   []ToolCall `json:"completed_steps"`
			PrereqViolations int        `json:"prereq_violations"`
		} `json:"state"`
	} `json:"input"`
	Expected struct {
		Outcome              string     `json:"outcome"`
		ToolCalls            []ToolCall `json:"tool_calls"`
		MissingSteps         []string   `json:"missing_steps"`
		MissingPrerequisites []string   `json:"missing_prerequisites"`
		NudgeKind            string     `json:"nudge_kind"`
		PrereqViolations     int        `json:"prereq_violations"`
	} `json:"expected"`
}

func TestGuardrailsReplaySharedCases(t *testing.T) {
	data, err := os.ReadFile("../../../fixtures/guardrails/cases.v1.json")
	if err != nil {
		t.Fatal(err)
	}
	var contract replayContract
	if err := json.Unmarshal(data, &contract); err != nil {
		t.Fatalf("decode shared guardrails cases: %v", err)
	}
	if contract.Version != 1 {
		t.Fatalf("contract version = %d, want 1", contract.Version)
	}

	for _, tc := range contract.Cases {
		t.Run(tc.Name, func(t *testing.T) {
			config, err := LoadConfig(tc.Config)
			if err != nil {
				t.Fatalf("load config: %v", err)
			}
			guardrails, err := NewGuardrails(config)
			if err != nil {
				t.Fatalf("create guardrails: %v", err)
			}
			const session = "fixture-session"

			for _, completed := range tc.Input.State.CompletedSteps {
				guardrails.Record(session, completed, nil, "")
			}
			if tc.Input.State.PrereqViolations > 0 {
				if len(config.Prerequisites) == 0 {
					t.Fatal("fixture seeds prerequisite violations without a prerequisite rule")
				}
				rule := config.Prerequisites[0]
				for i := 0; i < tc.Input.State.PrereqViolations; i++ {
					guardrails.Check(session, []ToolCall{{Tool: rule.Tool, Args: map[string]interface{}{}}})
				}
			}

			var result GuardrailResult
			if tc.Input.Record != nil {
				result = guardrails.Record(session, ToolCall{Tool: tc.Input.Record.Tool}, errors.New(tc.Input.Record.Error), ErrorCategory(tc.Input.Record.Category))
			} else {
				result = guardrails.Check(session, tc.Input.Response.ToolCalls)
			}

			if string(result.Outcome) != tc.Expected.Outcome {
				t.Errorf("outcome = %q, want %q", result.Outcome, tc.Expected.Outcome)
			}
			if tc.Expected.NudgeKind != "" {
				if result.Nudge == nil {
					t.Fatal("expected a nudge")
				}
				if string(result.Nudge.Kind) != tc.Expected.NudgeKind {
					t.Errorf("nudge kind = %q, want %q", result.Nudge.Kind, tc.Expected.NudgeKind)
				}
			}
			if tc.Expected.ToolCalls != nil && !reflect.DeepEqual(result.ToolCalls, tc.Expected.ToolCalls) {
				t.Errorf("tool calls = %#v, want %#v", result.ToolCalls, tc.Expected.ToolCalls)
			}
			if tc.Expected.MissingSteps != nil && !reflect.DeepEqual(result.MissingSteps, tc.Expected.MissingSteps) {
				t.Errorf("missing steps = %v, want %v", result.MissingSteps, tc.Expected.MissingSteps)
			}
			if tc.Expected.MissingPrerequisites != nil && !reflect.DeepEqual(result.MissingPrerequisites, tc.Expected.MissingPrerequisites) {
				t.Errorf("missing prerequisites = %v, want %v", result.MissingPrerequisites, tc.Expected.MissingPrerequisites)
			}
			if tc.Expected.PrereqViolations != 0 && result.PrereqViolations != tc.Expected.PrereqViolations {
				t.Errorf("prerequisite violations = %d, want %d", result.PrereqViolations, tc.Expected.PrereqViolations)
			}
		})
	}
}
