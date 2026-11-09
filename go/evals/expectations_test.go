package evals

import (
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"sort"
	"strings"
	"testing"
)

type scriptTurn struct {
	Content   string `json:"content"`
	ToolCalls []struct {
		Name      string         `json:"name"`
		Arguments map[string]any `json:"arguments"`
	} `json:"tool_calls"`
}

type fixtureEntry struct {
	Case     json.RawMessage `json:"case"`
	Script   [][]scriptTurn  `json:"script"`
	Expected struct {
		Success       bool     `json:"success"`
		Passes        int      `json:"passes"`
		Repeats       int      `json:"repeats"`
		Turns         int      `json:"turns"`
		ToolCalls     []string `json:"toolCalls"`
		Error         *string  `json:"error"`
		ErrorContains *string  `json:"errorContains"`
	} `json:"expected"`
}

type scriptedModel struct {
	turns    []scriptTurn
	consumed int
}

func (m *scriptedModel) Complete(_ []ChatMessage, _ []ToolDefinition, _ float64, _ int) (*ModelResult, error) {
	if m.consumed >= len(m.turns) {
		return nil, errors.New("script exhausted")
	}
	turn := m.turns[m.consumed]
	m.consumed++
	result := &ModelResult{Content: turn.Content}
	for i, tc := range turn.ToolCalls {
		args, err := json.Marshal(tc.Arguments)
		if err != nil {
			return nil, err
		}
		result.ToolCalls = append(result.ToolCalls, ToolCall{
			ID:        fmt.Sprintf("call-%d-%d", m.consumed, i),
			Name:      tc.Name,
			Arguments: string(args),
		})
	}
	return result, nil
}

func loadExpectationsFixture(t *testing.T) ([]ToolDefinition, []fixtureEntry) {
	t.Helper()
	raw, err := os.ReadFile("../../fixtures/evals/expectations/cases.json")
	if err != nil {
		t.Fatal(err)
	}
	var fixture struct {
		Tools []ToolDefinition `json:"tools"`
		Cases []fixtureEntry   `json:"cases"`
	}
	if err := json.Unmarshal(raw, &fixture); err != nil {
		t.Fatal(err)
	}
	if len(fixture.Cases) == 0 {
		t.Fatal("fixture has no cases")
	}
	return fixture.Tools, fixture.Cases
}

func newScriptedRunner(model ChatModel) *EvalRunner {
	r := NewEvalRunner("http://unused", 10)
	r.ClientFactory = func(string) ChatModel { return model }
	return r
}

func okExecutor(string, map[string]interface{}) string { return "ok" }

func TestEvalExpectationsFixture(t *testing.T) {
	tools, entries := loadExpectationsFixture(t)
	for _, entry := range entries {
		var c EvalCase
		if err := json.Unmarshal(entry.Case, &c); err != nil {
			t.Fatal(err)
		}
		c.Tools = tools
		t.Run(c.Name, func(t *testing.T) {
			var turns []scriptTurn
			for _, repeat := range entry.Script {
				turns = append(turns, repeat...)
			}
			model := &scriptedModel{turns: turns}
			res := newScriptedRunner(model).RunCase(c, "scripted", okExecutor)

			want := entry.Expected
			if res.Success != want.Success {
				t.Errorf("success = %v, want %v (error %q)", res.Success, want.Success, res.Error)
			}
			if res.Passes != want.Passes || res.Repeats != want.Repeats {
				t.Errorf("passes/repeats = %d/%d, want %d/%d", res.Passes, res.Repeats, want.Passes, want.Repeats)
			}
			if res.Turns != want.Turns {
				t.Errorf("turns = %d, want %d", res.Turns, want.Turns)
			}
			names := []string{}
			for _, tc := range res.ToolCalls {
				names = append(names, tc["name"].(string))
			}
			if strings.Join(names, ",") != strings.Join(want.ToolCalls, ",") {
				t.Errorf("tool calls = %v, want %v", names, want.ToolCalls)
			}
			if want.Error != nil && res.Error != *want.Error {
				t.Errorf("error = %q, want %q", res.Error, *want.Error)
			}
			if want.ErrorContains != nil && !strings.Contains(res.Error, *want.ErrorContains) {
				t.Errorf("error = %q, want it to contain %q", res.Error, *want.ErrorContains)
			}
			if model.consumed != len(turns) {
				t.Errorf("consumed %d script turns, want %d", model.consumed, len(turns))
			}
		})
	}
}

func rowKeys(row map[string]interface{}) []string {
	keys := make([]string, 0, len(row))
	for k := range row {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	return keys
}

func TestReportRowSingleRunShapeUnchanged(t *testing.T) {
	tools, _ := loadExpectationsFixture(t)
	model := &scriptedModel{turns: []scriptTurn{{Content: "hi"}}}
	res := newScriptedRunner(model).RunCase(EvalCase{
		Name: "legacy", UserMessage: "hi", Tools: tools, ExpectedTool: "get_weather", MaxTurns: 1,
	}, "scripted", okExecutor)
	got := strings.Join(rowKeys(ReportRow(res)), ",")
	want := "case,duration_ms,error,model,success,tool_calls,turns"
	if got != want {
		t.Errorf("row keys = %s, want %s", got, want)
	}
}

func TestReportRowRepeatedAddsPassesAndRepeats(t *testing.T) {
	tools, _ := loadExpectationsFixture(t)
	model := &scriptedModel{turns: []scriptTurn{{Content: "a"}, {Content: "b"}}}
	res := newScriptedRunner(model).RunCase(EvalCase{
		Name: "repeated", UserMessage: "hi", Tools: tools, ExpectedTool: "get_weather",
		Expect: ExpectNoCall, Repeats: 2, MaxTurns: 1,
	}, "scripted", okExecutor)
	row := ReportRow(res)
	if row["passes"] != 2 || row["repeats"] != 2 {
		t.Errorf("row passes/repeats = %v/%v, want 2/2", row["passes"], row["repeats"])
	}
}

func TestMinPassRateZeroIsExplicit(t *testing.T) {
	tools, _ := loadExpectationsFixture(t)
	zero := 0.0
	model := &scriptedModel{turns: []scriptTurn{{Content: "no tool"}}}
	res := newScriptedRunner(model).RunCase(EvalCase{
		Name: "zero", UserMessage: "hi", Tools: tools, ExpectedTool: "get_weather",
		MinPassRate: &zero, MaxTurns: 1,
	}, "scripted", okExecutor)
	if !res.Success || res.Passes != 0 {
		t.Errorf("success=%v passes=%d, want true/0", res.Success, res.Passes)
	}
}
