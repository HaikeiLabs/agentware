package reasoning

import (
	"testing"

	"github.com/haikeilabs/agentware/go/internal/livecapture"
)

// TestFillLiveCaptures writes the Go adapter's analysis into candidate
// reasoning fixtures captured from live models (scripts/live-captures): the
// extracted reasoning and the clean content, or both null when the adapter
// fails closed, which is the fixture's fail-closed form. The other languages
// then replay the filled fixtures, so any disagreement with Go fails their
// fixture tests. Skipped unless AGENTWARE_LIVE_CAPTURE_DIR is set.
func TestFillLiveCaptures(t *testing.T) {
	for _, file := range livecapture.Files(t, "reasoning") {
		fx := livecapture.Read(t, file)

		examples := []any{}
		failClosed := 0
		for _, ex := range livecapture.Cases(fx, "examples") {
			an, err := New().analyze(ex["raw_response"], "")
			if err != nil {
				ex["expected_extracted_reasoning"] = nil
				ex["expected_clean_content"] = nil
				failClosed++
			} else {
				ex["expected_extracted_reasoning"] = an.reasoning
				ex["expected_clean_content"] = an.clean
			}
			examples = append(examples, ex)
		}
		fx["examples"] = examples

		livecapture.Write(t, file, fx)
		t.Logf("%s: %d examples, %d fail closed in Go", file, len(examples), failClosed)
	}
}
