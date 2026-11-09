package evals

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"reflect"
	"time"
)

// Values of EvalCase.Expect.
const (
	// ExpectCall (the default) passes a repeat when ExpectedTool is called.
	ExpectCall = "call"
	// ExpectNoCall passes a repeat when ExpectedTool (with ExpectedArgs, if
	// given) is NOT called.
	ExpectNoCall = "no_call"
)

// EvalCase is one case. The JSON tags follow the shared case-file contract
// (docs/evals/README.md).
type EvalCase struct {
	Name         string           `json:"name"`
	Description  string           `json:"description"`
	SystemPrompt string           `json:"systemPrompt"`
	UserMessage  string           `json:"userMessage"`
	Tools        []ToolDefinition `json:"tools"`
	ExpectedTool string           `json:"expectedTool"`
	// Expect is ExpectCall (also when empty) or ExpectNoCall.
	Expect string `json:"expect,omitempty"`
	// ExpectedArgs is a partial match on the expected tool call's arguments:
	// every key must be present with an equal value; extra arguments are
	// allowed. Nil means no argument check.
	ExpectedArgs map[string]any `json:"expectedArgs,omitempty"`
	// Repeats is how many times to run the case; 0 means 1.
	Repeats int `json:"repeats,omitempty"`
	// MinPassRate (0..1) is the pass threshold for passes/repeats; nil means 1.0.
	MinPassRate *float64 `json:"minPassRate,omitempty"`
	MaxTurns    int      `json:"maxTurns"`
}

func (c EvalCase) repeats() int {
	if c.Repeats == 0 {
		return 1
	}
	return c.Repeats
}

func (c EvalCase) minPassRate() float64 {
	if c.MinPassRate == nil {
		return 1.0
	}
	return *c.MinPassRate
}

// Validate reports an invalid Expect, Repeats or MinPassRate.
func (c EvalCase) Validate() error {
	if c.Expect != "" && c.Expect != ExpectCall && c.Expect != ExpectNoCall {
		return fmt.Errorf("invalid expect %q: must be %q or %q", c.Expect, ExpectCall, ExpectNoCall)
	}
	if c.Repeats < 0 {
		return fmt.Errorf("invalid repeats %d: must be an integer >= 1", c.Repeats)
	}
	if r := c.minPassRate(); !(r >= 0 && r <= 1) {
		return fmt.Errorf("invalid minPassRate %g: must be between 0 and 1", r)
	}
	return nil
}

// ArgsMatch reports whether every key of expected is in args with an equal
// value. Values are compared after a JSON round trip, so Go ints match JSON
// numbers.
func ArgsMatch(expected map[string]any, args map[string]any) bool {
	if args == nil && len(expected) > 0 {
		return false
	}
	for key, want := range expected {
		got, ok := args[key]
		if !ok || !reflect.DeepEqual(normalizeJSON(want), normalizeJSON(got)) {
			return false
		}
	}
	return true
}

func normalizeJSON(v any) any {
	raw, err := json.Marshal(v)
	if err != nil {
		return v
	}
	var out any
	if err := json.Unmarshal(raw, &out); err != nil {
		return v
	}
	return out
}

type EvalResult struct {
	CaseName  string
	ModelName string
	Success   bool
	// Turns is the total across all repeats.
	Turns int
	// ToolCalls covers all repeats, in order.
	ToolCalls  []map[string]interface{}
	Error      string
	DurationMs int64
	// Passes is the number of repeats that passed.
	Passes int
	// Repeats is the number of repeats run.
	Repeats int
}

type EvalReport struct {
	Timestamp string
	Models    []string
	Results   []EvalResult
}

func (r *EvalReport) PassRate(model string) float64 {
	var passed, total int
	for _, res := range r.Results {
		if res.ModelName == model {
			total++
			if res.Success {
				passed++
			}
		}
	}
	if total == 0 {
		return 0.0
	}
	return float64(passed) / float64(total)
}

// ChatModel is the model seam; *ModelClient satisfies it.
type ChatModel interface {
	Complete(messages []ChatMessage, tools []ToolDefinition, temperature float64, maxTokens int) (*ModelResult, error)
}

type EvalRunner struct {
	BaseURL  string
	MaxTurns int
	Results  []EvalResult
	// ClientFactory builds the model client for a model name. Nil uses
	// NewModelClient against BaseURL.
	ClientFactory func(model string) ChatModel
}

func NewEvalRunner(baseURL string, maxTurns int) *EvalRunner {
	return &EvalRunner{
		BaseURL:  baseURL,
		MaxTurns: maxTurns,
	}
}

type ToolExecutor func(toolName string, args map[string]interface{}) string

func (r *EvalRunner) client(model string) ChatModel {
	if r.ClientFactory != nil {
		return r.ClientFactory(model)
	}
	return NewModelClient(r.BaseURL, model, "")
}

// RunCase runs the case Repeats times. It passes when
// passes/repeats >= MinPassRate.
func (r *EvalRunner) RunCase(case_ EvalCase, model string, toolExecutor ToolExecutor) EvalResult {
	start := time.Now()
	var toolCallsMade []map[string]interface{}

	if err := case_.Validate(); err != nil {
		repeats := case_.Repeats
		if repeats < 1 {
			repeats = 1
		}
		return EvalResult{
			CaseName:   case_.Name,
			ModelName:  model,
			Error:      err.Error(),
			DurationMs: time.Since(start).Milliseconds(),
			Repeats:    repeats,
		}
	}

	repeats := case_.repeats()
	minPassRate := case_.minPassRate()
	passes, turns := 0, 0
	firstErr := ""
	for i := 0; i < repeats; i++ {
		passed, repeatTurns, errMsg := r.runRepeat(case_, model, toolExecutor, &toolCallsMade)
		turns += repeatTurns
		if passed {
			passes++
		} else if firstErr == "" {
			firstErr = errMsg
		}
	}

	success := float64(passes)/float64(repeats) >= minPassRate
	errMsg := ""
	if !success {
		errMsg = firstErr
		if repeats > 1 {
			errMsg = fmt.Sprintf("passed %d/%d repeats (minPassRate %g): %s", passes, repeats, minPassRate, firstErr)
		}
	}
	return EvalResult{
		CaseName:   case_.Name,
		ModelName:  model,
		Success:    success,
		Turns:      turns,
		ToolCalls:  toolCallsMade,
		Error:      errMsg,
		DurationMs: time.Since(start).Milliseconds(),
		Passes:     passes,
		Repeats:    repeats,
	}
}

// runRepeat runs one repeat and returns whether it passed, its turns and its
// failure message.
func (r *EvalRunner) runRepeat(case_ EvalCase, model string, toolExecutor ToolExecutor, toolCallsMade *[]map[string]interface{}) (bool, int, string) {
	client := r.client(model)
	noCall := case_.Expect == ExpectNoCall

	messages := []ChatMessage{
		{Role: "system", Content: case_.SystemPrompt},
		{Role: "user", Content: case_.UserMessage},
	}

	turns := 0
	for turns < case_.MaxTurns {
		result, err := client.Complete(messages, case_.Tools, 0.0, 2048)
		turns++
		if err != nil {
			return false, turns, err.Error()
		}

		if len(result.ToolCalls) == 0 {
			break
		}

		for _, tc := range result.ToolCalls {
			*toolCallsMade = append(*toolCallsMade, map[string]interface{}{
				"turn":      turns,
				"name":      tc.Name,
				"arguments": tc.Arguments,
			})

			var args map[string]interface{}
			parseErr := json.Unmarshal([]byte(tc.Arguments), &args)
			if tc.Arguments == "" {
				parseErr = nil
				args = map[string]interface{}{}
			}

			if tc.Name == case_.ExpectedTool {
				matches := case_.ExpectedArgs == nil || (parseErr == nil && ArgsMatch(case_.ExpectedArgs, args))
				if noCall {
					if matches {
						return false, turns, fmt.Sprintf("Tool '%s' was called but expect is no_call", tc.Name)
					}
				} else {
					// Reaching the expected tool decides the repeat either way.
					if matches {
						return true, turns, ""
					}
					return false, turns, fmt.Sprintf("Tool '%s' called with arguments that do not match expectedArgs", tc.Name)
				}
			}

			toolResult := toolExecutor(tc.Name, args)

			messages = append(messages, ChatMessage{
				Role:    "assistant",
				Content: "",
			})
			messages = append(messages, ChatMessage{
				Role:    "tool",
				Content: toolResult,
			})
		}
	}

	if noCall {
		return true, turns, ""
	}
	return false, turns, fmt.Sprintf("Expected tool '%s' not called in %d turns", case_.ExpectedTool, turns)
}

func (r *EvalRunner) RunEvals(cases_ []EvalCase, models []string, toolExecutor ToolExecutor) EvalReport {
	report := EvalReport{
		Timestamp: time.Now().Format(time.RFC3339),
		Models:    models,
	}

	for _, model := range models {
		fmt.Printf("\n=== Testing model: %s ===\n", model)
		for _, case_ := range cases_ {
			fmt.Printf("  Running: %s...", case_.Name)
			result := r.RunCase(case_, model, toolExecutor)
			r.Results = append(r.Results, result)
			report.Results = append(report.Results, result)

			status := "PASS"
			if !result.Success {
				status = "FAIL"
			}
			fmt.Printf("%s (%d turns, %dms)\n", status, result.Turns, result.DurationMs)

			if !result.Success {
				fmt.Printf("    Error: %s\n", result.Error)
			}
		}
	}

	return report
}

func (r *EvalRunner) SaveReport(report EvalReport, outputPath string) {
	dir := filepath.Dir(outputPath)
	_ = os.MkdirAll(dir, 0755)

	output := map[string]interface{}{
		"timestamp": report.Timestamp,
		"models":    report.Models,
		"results":   []map[string]interface{}{
			// Results will be serialized individually below
		},
	}

	results := make([]map[string]interface{}, len(report.Results))
	for i, res := range report.Results {
		results[i] = ReportRow(res)
	}
	output["results"] = results

	data, _ := json.MarshalIndent(output, "", "  ")
	if err := os.WriteFile(outputPath, data, 0644); err != nil {
		fmt.Fprintf(os.Stderr, "Warning: failed to write report: %v\n", err)
	}
}

// ReportRow is one result as a saved-report row. "passes" and "repeats" are
// added only when the case ran more than once, so single-run reports keep
// their shape.
func ReportRow(res EvalResult) map[string]interface{} {
	row := map[string]interface{}{
		"case":        res.CaseName,
		"model":       res.ModelName,
		"success":     res.Success,
		"turns":       res.Turns,
		"tool_calls":  res.ToolCalls,
		"error":       res.Error,
		"duration_ms": res.DurationMs,
	}
	if res.Repeats > 1 {
		row["passes"] = res.Passes
		row["repeats"] = res.Repeats
	}
	return row
}
