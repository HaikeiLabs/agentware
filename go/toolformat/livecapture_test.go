package toolformat

import (
	"testing"

	"github.com/haikeilabs/agentware/go/internal/livecapture"
)

// TestFillLiveCaptures writes the Go formatter's answers into candidate
// toolformat fixtures captured from live models (scripts/live-captures). Each
// parse_calls case gets the calls Go parses as its expected value; a case Go
// cannot parse is moved to go_rejected, since the parse_calls contract has no
// error form. Each validate_format case gets Go's verdict. The other
// languages then replay the filled fixtures, so any disagreement with Go
// fails their fixture tests. Skipped unless AGENTWARE_LIVE_CAPTURE_DIR is set.
func TestFillLiveCaptures(t *testing.T) {
	for _, file := range livecapture.Files(t, "toolformat") {
		fx := livecapture.Read(t, file)
		family, _ := fx["family"].(string)
		formatter, ok := formattersByFamily[family]
		if !ok {
			t.Fatalf("%s: no Go formatter for family %q", file, family)
		}

		rejected, _ := fx["go_rejected"].([]any)
		parsed := []any{}
		for _, c := range livecapture.Cases(fx, "parse_calls") {
			input, _ := c["input"].(string)
			calls, err := formatter.ParseToolCalls(input)
			if err != nil {
				rejected = append(rejected, map[string]any{"section": "parse_calls", "name": c["name"], "error": err.Error()})
				continue
			}
			expected := make([]any, 0, len(calls))
			for _, call := range calls {
				want := map[string]any{"name": call.Name, "args": call.Args}
				if call.ID != "" {
					want["id"] = call.ID
				}
				expected = append(expected, want)
			}
			c["expected"] = expected
			parsed = append(parsed, c)
		}
		fx["parse_calls"] = parsed

		validated := []any{}
		for _, c := range livecapture.Cases(fx, "validate_format") {
			input, _ := c["input"].(string)
			c["valid"] = formatter.ValidateFormat(input) == nil
			validated = append(validated, c)
		}
		fx["validate_format"] = validated
		if len(rejected) > 0 {
			fx["go_rejected"] = rejected
		}

		livecapture.Write(t, file, fx)
		t.Logf("%s: %d parse_calls, %d validate_format, %d rejected by Go", file, len(parsed), len(validated), len(rejected))
	}
}
