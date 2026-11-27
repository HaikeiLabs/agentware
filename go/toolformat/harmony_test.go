package toolformat

import (
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"

	"github.com/haikeilabs/agentware/go/tools"
)

// harmonyFixture holds the Harmony-only sections of
// fixtures/toolformat/harmony-cases.json. Its four standard sections replay
// in TestToolformatFixtures.
type harmonyFixture struct {
	RenderPrompt []struct {
		Name             string           `json:"name"`
		System           json.RawMessage  `json:"system"`
		Instructions     string           `json:"instructions"`
		Tools            []fixtureToolDef `json:"tools"`
		Messages         []harmonyMsgDef  `json:"messages"`
		AutoDropAnalysis *bool            `json:"auto_drop_analysis"`
		NextRole         *string          `json:"next_role"`
		Expected         string           `json:"expected"`
	} `json:"render_prompt"`
	ParseCompletion []struct {
		Name     string `json:"name"`
		Input    string `json:"input"`
		Expected struct {
			Reasoning string `json:"reasoning"`
			Content   string `json:"content"`
			ToolCalls []struct {
				Name string         `json:"name"`
				Args map[string]any `json:"args"`
			} `json:"tool_calls"`
			Messages []harmonyMsgDef `json:"messages"`
		} `json:"expected"`
	} `json:"parse_completion"`
}

type fixtureToolDef struct {
	Name        string          `json:"name"`
	Description string          `json:"description"`
	InputSchema json.RawMessage `json:"input_schema"`
}

type harmonyMsgDef struct {
	Role        string `json:"role"`
	Content     string `json:"content"`
	Channel     string `json:"channel"`
	Recipient   string `json:"recipient"`
	ContentType string `json:"content_type"`
	Name        string `json:"name"`
}

type harmonySystemDef struct {
	ModelIdentity   *string  `json:"model_identity"`
	KnowledgeCutoff *string  `json:"knowledge_cutoff"`
	CurrentDate     string   `json:"current_date"`
	ReasoningEffort *string  `json:"reasoning_effort"`
	ValidChannels   []string `json:"valid_channels"`
}

func loadHarmonyFixture(t *testing.T) harmonyFixture {
	t.Helper()
	data, err := os.ReadFile(filepath.Join(toolformatFixtures, "harmony-cases.json"))
	if err != nil {
		t.Fatal(err)
	}
	var fx harmonyFixture
	if err := json.Unmarshal(data, &fx); err != nil {
		t.Fatal(err)
	}
	return fx
}

// harmonyOptions maps a fixture case onto HarmonyPromptOptions. A missing
// "system" key is the default system message and null leaves it out.
func harmonyOptions(t *testing.T, system json.RawMessage, instructions string, defs []fixtureToolDef, drop *bool, next *string) HarmonyPromptOptions {
	t.Helper()
	opts := HarmonyPromptOptions{Instructions: instructions}
	if len(system) > 0 {
		if string(system) == "null" {
			opts.OmitSystem = true
		} else {
			var def harmonySystemDef
			if err := json.Unmarshal(system, &def); err != nil {
				t.Fatal(err)
			}
			s := DefaultHarmonySystem()
			if def.ModelIdentity != nil {
				s.ModelIdentity = *def.ModelIdentity
			}
			if def.KnowledgeCutoff != nil {
				s.KnowledgeCutoff = *def.KnowledgeCutoff
			}
			s.CurrentDate = def.CurrentDate
			if def.ReasoningEffort != nil {
				s.ReasoningEffort = *def.ReasoningEffort
			}
			if def.ValidChannels != nil {
				s.ValidChannels = def.ValidChannels
			}
			opts.System = &s
		}
	}
	for _, d := range defs {
		opts.Tools = append(opts.Tools, newFixtureTool(t, d.Name, d.Description, d.InputSchema))
	}
	opts.KeepAnalysis = drop != nil && !*drop
	if next != nil {
		opts.NextRole = *next
		opts.OmitNextRole = *next == ""
	}
	return opts
}

func TestHarmonyRenderPrompt(t *testing.T) {
	for _, tc := range loadHarmonyFixture(t).RenderPrompt {
		t.Run(tc.Name, func(t *testing.T) {
			messages := make([]HarmonyMessage, len(tc.Messages))
			for i, m := range tc.Messages {
				messages[i] = HarmonyMessage(m)
			}
			got, err := RenderHarmonyPrompt(messages, harmonyOptions(t, tc.System, tc.Instructions, tc.Tools, tc.AutoDropAnalysis, tc.NextRole))
			if err != nil {
				t.Fatal(err)
			}
			if got != tc.Expected {
				t.Errorf("render mismatch\n got: %q\nwant: %q", got, tc.Expected)
			}
		})
	}
}

func TestHarmonyParseCompletion(t *testing.T) {
	for _, tc := range loadHarmonyFixture(t).ParseCompletion {
		t.Run(tc.Name, func(t *testing.T) {
			got := ParseHarmonyCompletion(tc.Input)
			if got.Reasoning != tc.Expected.Reasoning {
				t.Errorf("reasoning = %q, want %q", got.Reasoning, tc.Expected.Reasoning)
			}
			if got.Content != tc.Expected.Content {
				t.Errorf("content = %q, want %q", got.Content, tc.Expected.Content)
			}
			if len(got.ToolCalls) != len(tc.Expected.ToolCalls) {
				t.Fatalf("got %d calls, want %d", len(got.ToolCalls), len(tc.Expected.ToolCalls))
			}
			for i, want := range tc.Expected.ToolCalls {
				if got.ToolCalls[i].Name != want.Name || !reflect.DeepEqual(got.ToolCalls[i].Args, want.Args) {
					t.Errorf("call %d = %s %v, want %s %v", i, got.ToolCalls[i].Name, got.ToolCalls[i].Args, want.Name, want.Args)
				}
			}
			wantMessages := make([]HarmonyMessage, len(tc.Expected.Messages))
			for i, m := range tc.Expected.Messages {
				wantMessages[i] = HarmonyMessage(m)
			}
			if len(got.Messages) != 0 || len(wantMessages) != 0 {
				if !reflect.DeepEqual(got.Messages, wantMessages) {
					t.Errorf("messages\n got: %#v\nwant: %#v", got.Messages, wantMessages)
				}
			}
		})
	}
}

func TestHarmonyParsedMessagesRenderBackIntoHistory(t *testing.T) {
	c := ParseHarmonyCompletion(`<|channel|>analysis<|message|>Need weather for Tokyo.<|end|><|start|>assistant<|channel|>commentary to=functions.get_weather <|constrain|>json<|message|>{"city":"Tokyo"}<|call|>`)
	want := []string{
		`<|start|>assistant<|channel|>analysis<|message|>Need weather for Tokyo.<|end|>`,
		`<|start|>assistant to=functions.get_weather<|channel|>commentary <|constrain|>json<|message|>{"city":"Tokyo"}<|call|>`,
	}
	for i, m := range c.Messages {
		got, err := RenderHarmonyMessage(m)
		if err != nil || got != want[i] {
			t.Errorf("message %d = %q (%v), want %q", i, got, err, want[i])
		}
	}
	final := ParseHarmonyCompletion(`<|channel|>final<|message|>done<|return|>`).Messages[0]
	if got, _ := RenderHarmonyMessage(final); got != `<|start|>assistant<|channel|>final<|message|>done<|end|>` {
		t.Errorf("final = %q", got)
	}
}

func TestHarmonyStopTokens(t *testing.T) {
	if got := HarmonyStopTokens(); !reflect.DeepEqual(got, []string{"<|return|>", "<|call|>"}) {
		t.Errorf("stop tokens = %q", got)
	}
}

func TestHarmonyRejectsInvalidMessages(t *testing.T) {
	for _, m := range []HarmonyMessage{{Role: "robot"}, {Role: "tool", Content: "x"}} {
		if _, err := RenderHarmonyMessage(m); !errors.Is(err, ErrInvalidHarmonyMessage) {
			t.Errorf("%+v: err = %v, want ErrInvalidHarmonyMessage", m, err)
		}
	}
	bad := DefaultHarmonySystem()
	bad.ReasoningEffort = "max"
	if _, err := RenderHarmonyPrompt(nil, HarmonyPromptOptions{System: &bad}); !errors.Is(err, ErrInvalidHarmonyMessage) {
		t.Errorf("effort max: err = %v", err)
	}
}

// Harmony is opt-in: chat-completions users keep the OpenAI formatter.
func TestHarmonyIsOptIn(t *testing.T) {
	for _, model := range []string{"openai/gpt-oss-20b", "gpt-oss-120b", "ggml-org/gpt-oss-20b-GGUF"} {
		if f := GetFormatter(model); f.ModelFamily() != "openai" {
			t.Errorf("%s: family = %s, want openai", model, f.ModelFamily())
		}
	}
}

// A schema given only as a Go map has no key order: required properties come
// first, in "required" order, then the rest by name. Go-native []string
// values work like decoded JSON arrays.
func TestHarmonyMapSchemaOrder(t *testing.T) {
	tool := fixtureTool{name: "pick", description: "Pick one.", inputSchema: map[string]any{
		"type": "object",
		"properties": map[string]any{
			"zeta":  map[string]any{"type": "string", "enum": []string{"a", "b"}},
			"alpha": map[string]any{"type": "integer"},
			"mid":   map[string]any{"type": "boolean"},
		},
		"required": []string{"mid"},
	}}
	got := (&HarmonyFormatter{}).FormatToolDefinitions([]tools.Tool{tool})
	want := "type pick = (_: {\nmid: boolean,\nalpha?: number,\nzeta?: \"a\" | \"b\",\n}) => any;"
	if !strings.Contains(got, want) {
		t.Errorf("got %q", got)
	}
}

func TestHarmonyNoToolsRendersNothing(t *testing.T) {
	if got := (&HarmonyFormatter{}).FormatToolDefinitions(nil); got != "" {
		t.Errorf("got %q", got)
	}
}
