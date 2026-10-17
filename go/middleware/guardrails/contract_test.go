package guardrails

import (
	"encoding/json"
	"os"
	"testing"
)

type contractDocument struct {
	Version int            `json:"version"`
	Cases   []contractCase `json:"cases"`
}

type contractCase struct {
	Name     string          `json:"name"`
	Config   json.RawMessage `json:"config"`
	Input    json.RawMessage `json:"input"`
	Expected struct {
		Outcome string `json:"outcome"`
	} `json:"expected"`
}

// TestSharedContractCasesReplayable keeps the shared v1 cases consumable by the
// Go implementation before each language adds its facade replay in Phase 1.
func TestSharedContractCasesReplayable(t *testing.T) {
	data, err := os.ReadFile("../../../fixtures/guardrails/cases.v1.json")
	if err != nil {
		t.Fatal(err)
	}

	var doc contractDocument
	if err := json.Unmarshal(data, &doc); err != nil {
		t.Fatalf("decode shared cases: %v", err)
	}
	if doc.Version != 1 {
		t.Fatalf("contract version = %d, want 1", doc.Version)
	}
	if len(doc.Cases) == 0 {
		t.Fatal("shared contract has no cases")
	}

	allowedOutcomes := map[string]bool{
		"execute": true, "retry": true, "step_blocked": true,
		"fatal": true, "recorded": true,
	}
	seen := make(map[string]bool)
	for _, tc := range doc.Cases {
		t.Run(tc.Name, func(t *testing.T) {
			if tc.Name == "" || seen[tc.Name] {
				t.Fatalf("case name is empty or duplicated: %q", tc.Name)
			}
			seen[tc.Name] = true
			if !json.Valid(tc.Config) || !json.Valid(tc.Input) {
				t.Fatal("config and input must be valid JSON values")
			}
			if !allowedOutcomes[tc.Expected.Outcome] {
				t.Fatalf("unsupported facade outcome %q", tc.Expected.Outcome)
			}
		})
	}
}
