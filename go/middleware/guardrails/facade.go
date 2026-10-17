package guardrails

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"reflect"
)

const guardrailsConfigVersion = 1

// Config configures response quality and progress checks. It does not express
// tool authorization or make allow/deny decisions.
type Config struct {
	Version             int               `json:"version"`
	ToolNames           []string          `json:"tool_names"`
	RequiredSteps       []string          `json:"required_steps,omitempty"`
	TerminalTool        string            `json:"terminal_tool,omitempty"`
	Prerequisites       []Prerequisite    `json:"prerequisites,omitempty"`
	MaxPrereqViolations int               `json:"max_prereq_violations,omitempty"`
	StepNudgeTier       int               `json:"step_nudge_tier,omitempty"`
	Nudges              *ConfiguredNudges `json:"nudges,omitempty"`
}

// Prerequisite describes a tool that must have been recorded before another
// tool can be called. MatchArg optionally requires both calls to share an arg.
type Prerequisite struct {
	Tool     string `json:"tool"`
	Requires string `json:"requires"`
	MatchArg string `json:"match_arg,omitempty"`
}

// ConfiguredNudges optionally overrides the default quality guidance text.
type ConfiguredNudges struct {
	Retry        string `json:"retry,omitempty"`
	StepBlocked  string `json:"step_blocked,omitempty"`
	Prerequisite string `json:"prerequisite,omitempty"`
	Fatal        string `json:"fatal,omitempty"`
}

// Outcome is a quality and progress result, never an authorization decision.
type Outcome string

const (
	OutcomeExecute     Outcome = "execute"
	OutcomeRetry       Outcome = "retry"
	OutcomeStepBlocked Outcome = "step_blocked"
	OutcomeFatal       Outcome = "fatal"
	OutcomeRecorded    Outcome = "recorded"
)

// GuardrailResult contains the guidance returned for a response or tool
// result. A Nudge can be appended to the model-facing tool response.
type GuardrailResult struct {
	Outcome              Outcome
	ToolCalls            []ToolCall
	MissingSteps         []string
	MissingPrerequisites []string
	PrereqViolations     int
	Nudge                *Nudge
}

type recordedCall struct {
	Tool string
	Args map[string]interface{}
}

// Guardrails composes response validation, step enforcement, error tracking,
// and nudges behind one configured agent-quality facade.
type Guardrails struct {
	config           Config
	validator        *ResponseValidator
	steps            *StepEnforcer
	errors           *ErrorTracker
	recorded         map[string][]recordedCall
	prereqViolations map[string]int
}

// LoadConfig decodes and validates a versioned Guardrails JSON document.
func LoadConfig(data []byte) (Config, error) {
	decoder := json.NewDecoder(bytes.NewReader(data))
	decoder.DisallowUnknownFields()
	var config Config
	if err := decoder.Decode(&config); err != nil {
		return Config{}, fmt.Errorf("decode guardrails config: %w", err)
	}
	if err := decoder.Decode(new(interface{})); err != io.EOF {
		if err != nil {
			return Config{}, fmt.Errorf("decode guardrails config trailing data: %w", err)
		}
		return Config{}, errors.New("decode guardrails config: multiple JSON values")
	}
	if err := validateConfig(&config); err != nil {
		return Config{}, err
	}
	return config, nil
}

// NewGuardrails creates the facade from a validated configuration.
func NewGuardrails(config Config) (*Guardrails, error) {
	if err := validateConfig(&config); err != nil {
		return nil, err
	}
	steps := NewStepEnforcer()
	if config.TerminalTool != "" {
		steps.AddStep(config.TerminalTool, config.RequiredSteps)
	}
	return &Guardrails{
		config:           config,
		validator:        NewResponseValidator(config.ToolNames, false),
		steps:            steps,
		errors:           NewErrorTracker(),
		recorded:         make(map[string][]recordedCall),
		prereqViolations: make(map[string]int),
	}, nil
}

// Check validates generated tool calls and returns quality or progress
// guidance. It never authorizes or denies execution.
func (g *Guardrails) Check(sessionID string, calls []ToolCall) GuardrailResult {
	if len(calls) == 0 {
		return GuardrailResult{Outcome: OutcomeRetry, Nudge: g.withText(RetryNudge("", g.config.ToolNames), g.retryText())}
	}
	validation := g.validator.ValidateToolCalls(calls)
	if validation.NeedsRetry {
		return GuardrailResult{Outcome: OutcomeRetry, Nudge: g.withText(validation.Nudge, g.retryText())}
	}

	for _, call := range validation.ToolCalls {
		if g.config.TerminalTool != "" && call.Tool == g.config.TerminalTool {
			allowed, missing := g.steps.CanExecute(sessionID, call.Tool)
			if !allowed {
				nudge := StepNudge(call.Tool, missing, g.config.StepNudgeTier)
				return GuardrailResult{Outcome: OutcomeStepBlocked, MissingSteps: missing, Nudge: g.withText(nudge, g.stepText())}
			}
		}
		missing := g.missingPrerequisites(sessionID, call)
		if len(missing) > 0 {
			g.prereqViolations[sessionID]++
			count := g.prereqViolations[sessionID]
			nudge := PrerequisiteNudge(call.Tool, missing)
			outcome := OutcomeRetry
			if count > g.config.MaxPrereqViolations {
				outcome = OutcomeFatal
				nudge = g.withText(nudge, g.fatalText())
			} else {
				nudge = g.withText(nudge, g.prerequisiteText())
			}
			return GuardrailResult{Outcome: outcome, MissingPrerequisites: missing, PrereqViolations: count, Nudge: nudge}
		}
	}
	return GuardrailResult{Outcome: OutcomeExecute, ToolCalls: validation.ToolCalls}
}

// Record stores successful progress or tracks a tool error. Error nudges are
// returned for injection into the model-facing tool-error response.
func (g *Guardrails) Record(sessionID string, call ToolCall, err error, category ErrorCategory) GuardrailResult {
	if err != nil {
		g.errors.RecordError(sessionID, call.Tool, call.Args, err, category)
		nudge := &Nudge{
			Role:    "user",
			Content: fmt.Sprintf("Tool %q failed: %s. Correct the issue and try again.", call.Tool, err.Error()),
			Kind:    NudgeKindRetry,
			Tier:    0,
		}
		return GuardrailResult{Outcome: OutcomeRecorded, Nudge: g.withText(nudge, g.retryText())}
	}
	g.steps.MarkStepComplete(sessionID, call.Tool)
	g.recorded[sessionID] = append(g.recorded[sessionID], recordedCall(call))
	return GuardrailResult{Outcome: OutcomeRecorded}
}

// ErrorTracker exposes the composed tracker for inspection and integration.
func (g *Guardrails) ErrorTracker() *ErrorTracker { return g.errors }

func (g *Guardrails) missingPrerequisites(sessionID string, call ToolCall) []string {
	var missing []string
	for _, rule := range g.config.Prerequisites {
		if rule.Tool != call.Tool || g.hasRecordedPrerequisite(sessionID, rule, call) {
			continue
		}
		found := false
		for _, prereq := range missing {
			if prereq == rule.Requires {
				found = true
				break
			}
		}
		if !found {
			missing = append(missing, rule.Requires)
		}
	}
	return missing
}

func (g *Guardrails) hasRecordedPrerequisite(sessionID string, rule Prerequisite, call ToolCall) bool {
	for _, prior := range g.recorded[sessionID] {
		if prior.Tool != rule.Requires {
			continue
		}
		if rule.MatchArg == "" {
			return true
		}
		priorValue, priorOK := prior.Args[rule.MatchArg]
		callValue, callOK := call.Args[rule.MatchArg]
		if priorOK && callOK && reflect.DeepEqual(priorValue, callValue) {
			return true
		}
	}
	return false
}

func validateConfig(config *Config) error {
	if config.Version != guardrailsConfigVersion {
		return fmt.Errorf("unsupported guardrails config version %d", config.Version)
	}
	if len(config.ToolNames) == 0 {
		return errors.New("guardrails config requires at least one tool name")
	}
	tools := make(map[string]bool, len(config.ToolNames))
	for _, tool := range config.ToolNames {
		if tool == "" || tools[tool] {
			return fmt.Errorf("guardrails config has an empty or duplicate tool name %q", tool)
		}
		tools[tool] = true
	}
	if config.TerminalTool != "" && !tools[config.TerminalTool] {
		return fmt.Errorf("terminal tool %q is not in tool_names", config.TerminalTool)
	}
	for _, step := range config.RequiredSteps {
		if !tools[step] {
			return fmt.Errorf("required step %q is not in tool_names", step)
		}
	}
	seenPrerequisites := make(map[string]bool)
	for _, rule := range config.Prerequisites {
		if !tools[rule.Tool] || !tools[rule.Requires] || rule.Tool == rule.Requires {
			return fmt.Errorf("invalid prerequisite rule %q requires %q", rule.Tool, rule.Requires)
		}
		key := rule.Tool + "\x00" + rule.Requires + "\x00" + rule.MatchArg
		if seenPrerequisites[key] {
			return fmt.Errorf("duplicate prerequisite rule for %q requiring %q", rule.Tool, rule.Requires)
		}
		seenPrerequisites[key] = true
	}
	if config.MaxPrereqViolations == 0 {
		config.MaxPrereqViolations = 2
	}
	if config.MaxPrereqViolations < 1 {
		return errors.New("max_prereq_violations must be at least 1")
	}
	if config.StepNudgeTier == 0 {
		config.StepNudgeTier = 1
	}
	if config.StepNudgeTier < 1 || config.StepNudgeTier > 3 {
		return errors.New("step_nudge_tier must be from 1 to 3")
	}
	return nil
}

func (g *Guardrails) withText(nudge *Nudge, text string) *Nudge {
	if nudge != nil && text != "" {
		nudge.Content = text
	}
	return nudge
}

func (g *Guardrails) retryText() string {
	if g.config.Nudges != nil {
		return g.config.Nudges.Retry
	}
	return ""
}

func (g *Guardrails) stepText() string {
	if g.config.Nudges != nil {
		return g.config.Nudges.StepBlocked
	}
	return ""
}

func (g *Guardrails) prerequisiteText() string {
	if g.config.Nudges != nil {
		return g.config.Nudges.Prerequisite
	}
	return ""
}

func (g *Guardrails) fatalText() string {
	if g.config.Nudges != nil {
		return g.config.Nudges.Fatal
	}
	return ""
}
