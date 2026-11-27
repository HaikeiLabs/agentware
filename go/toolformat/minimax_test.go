package toolformat

import (
	"encoding/json"
	"os"
	"path/filepath"
	"reflect"
	"testing"

	"github.com/haikeilabs/agentware/go/tools"
)

// The shared runner (fixtures_test.go) replays minimax-cases.json through the
// default (M2) formatter. These tests check the M3 variant against the
// fixture's m3_format_definitions section and the selector routing.

type minimaxFixture struct {
	toolformatFixture
	M3FormatDefinitions []struct {
		Name     string `json:"name"`
		Expected string `json:"expected"`
	} `json:"m3_format_definitions"`
}

func loadMiniMaxFixture(t *testing.T) minimaxFixture {
	t.Helper()
	data, err := os.ReadFile(filepath.Join(toolformatFixtures, "minimax-cases.json"))
	if err != nil {
		t.Fatal(err)
	}
	var fx minimaxFixture
	if err := json.Unmarshal(data, &fx); err != nil {
		t.Fatal(err)
	}
	return fx
}

func TestMiniMaxM3FormatDefinitions(t *testing.T) {
	fx := loadMiniMaxFixture(t)
	if len(fx.M3FormatDefinitions) == 0 {
		t.Fatal("fixture has no m3_format_definitions")
	}
	f := &MiniMaxFormatter{Version: MiniMaxM3}
	for _, tc := range fx.M3FormatDefinitions {
		if got := f.FormatToolDefinitions(loadFixtureTools(t)); got != tc.Expected {
			t.Errorf("%s:\n got:  %q\n want: %q", tc.Name, got, tc.Expected)
		}
	}
}

func TestMiniMaxM3VariantParsesValidatesAndFormatsLikeM2(t *testing.T) {
	fx := loadMiniMaxFixture(t)
	m2, m3 := &MiniMaxFormatter{}, &MiniMaxFormatter{Version: MiniMaxM3}
	for _, tc := range fx.ParseCalls {
		a, errA := m2.ParseToolCalls(tc.Input)
		b, errB := m3.ParseToolCalls(tc.Input)
		if errA != nil || errB != nil || !reflect.DeepEqual(a, b) {
			t.Errorf("%s: m2 %v (%v) vs m3 %v (%v)", tc.Name, a, errA, b, errB)
		}
	}
	for _, tc := range fx.ValidateFormat {
		if got := m3.ValidateFormat(tc.Input) == nil; got != tc.Valid {
			t.Errorf("%s: valid = %v, want %v", tc.Name, got, tc.Valid)
		}
	}
	if got := m3.FormatToolResult("get_weather", &tools.Result{Success: true, Output: "Sunny"}); got != "<response>Sunny</response>" {
		t.Errorf("result = %q", got)
	}
}

func TestMiniMaxEmptyToolListRendersNothing(t *testing.T) {
	for _, f := range []*MiniMaxFormatter{{}, {Version: MiniMaxM2}, {Version: MiniMaxM3}} {
		if got := f.FormatToolDefinitions(nil); got != "" {
			t.Errorf("version %q: got %q", f.Version, got)
		}
	}
}

func TestGetFormatterRoutesMiniMax(t *testing.T) {
	for model, want := range map[string]MiniMaxVersion{
		"MiniMax-M2":             MiniMaxM2,
		"MiniMaxAI/MiniMax-M2.5": MiniMaxM2,
		"minimax-m2.1":           MiniMaxM2,
		"MiniMax-M3":             MiniMaxM3,
		"MiniMaxAI/MiniMax-M3":   MiniMaxM3,
		"minimax-m3-preview":     MiniMaxM3,
	} {
		f, ok := GetFormatter(model).(*MiniMaxFormatter)
		if !ok {
			t.Errorf("%s: got %T", model, GetFormatter(model))
			continue
		}
		if f.ModelFamily() != "minimax" || f.version() != want {
			t.Errorf("%s: family %q version %q, want minimax %q", model, f.ModelFamily(), f.version(), want)
		}
	}
	for _, model := range []string{"MiniMax-Text-01", "minimax-7b", "MiniMax-M1"} {
		if _, ok := GetFormatter(model).(*MiniMaxFormatter); ok {
			t.Errorf("%s: routed to MiniMaxFormatter", model)
		}
	}
}
