package reasoning

import (
	"encoding/json"
	"os"
	"path/filepath"
	"testing"

	"github.com/haikeilabs/agentware/go/internal/conformance"
)

// reasoningFixtures is the shared reasoning fixture set. Every
// <family>-cases.json file is replayed through the Go reasoning adapter so the
// language implementations stay in parity (see docs/testing.md).
const reasoningFixtures = "../../fixtures/reasoning"

type reasoningFixture struct {
	Family   string `json:"family"`
	Examples []struct {
		Name                       string  `json:"name"`
		RawResponse                any     `json:"raw_response"`
		ExpectedCleanContent       *string `json:"expected_clean_content"`
		ExpectedExtractedReasoning *string `json:"expected_extracted_reasoning"`
	} `json:"examples"`
}

// excludedReasoningCases lists reasoning fixture cases (keyed by
// "<family>/<case name>") that the Go adapter does not match, with the
// reason. The same cases are listed in fixtures/EXCLUSIONS.md.
var excludedReasoningCases = map[string]string{}

// TestReasoningFixtures replays every case in every shared reasoning fixture
// through the Go adapter's analysis and records each case for the
// equal-coverage gate. It asserts the extracted reasoning and the clean
// content left after stripping. A case whose expected clean content and
// reasoning are both null is a fail-closed case: the adapter must return an
// error rather than partial output.
func TestReasoningFixtures(t *testing.T) {
	files, err := filepath.Glob(filepath.Join(reasoningFixtures, "*-cases.json"))
	if err != nil {
		t.Fatal(err)
	}
	if len(files) == 0 {
		t.Fatal("no reasoning fixture files found")
	}

	for _, file := range files {
		data, err := os.ReadFile(file)
		if err != nil {
			t.Fatal(err)
		}
		var fx reasoningFixture
		if err := json.Unmarshal(data, &fx); err != nil {
			t.Fatal(err)
		}
		fixtureName := "reasoning/" + filepath.Base(file)

		for _, ex := range fx.Examples {
			ex := ex
			t.Run(fx.Family+"/"+ex.Name, func(t *testing.T) {
				if reason, ok := excludedReasoningCases[fx.Family+"/"+ex.Name]; ok {
					t.Skip("excluded (see fixtures/EXCLUSIONS.md): " + reason)
				}
				conformance.Record(t, fixtureName, "examples", ex.Name)
				a := New()
				an, err := a.analyze(ex.RawResponse, "")

				failClosed := ex.ExpectedCleanContent == nil && ex.ExpectedExtractedReasoning == nil
				if failClosed {
					if err == nil {
						t.Fatalf("expected fail-closed error, got analysis clean=%q reasoning=%q", an.clean, an.reasoning)
					}
					return
				}
				if err != nil {
					t.Fatalf("unexpected error: %v", err)
				}
				wantClean := ""
				if ex.ExpectedCleanContent != nil {
					wantClean = *ex.ExpectedCleanContent
				}
				wantReason := ""
				if ex.ExpectedExtractedReasoning != nil {
					wantReason = *ex.ExpectedExtractedReasoning
				}
				if an.clean != wantClean {
					t.Errorf("clean content = %q, want %q", an.clean, wantClean)
				}
				if an.reasoning != wantReason {
					t.Errorf("extracted reasoning = %q, want %q", an.reasoning, wantReason)
				}
			})
		}
	}
}
