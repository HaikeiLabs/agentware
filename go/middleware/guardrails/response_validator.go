package guardrails

import (
	"encoding/json"
	"regexp"
	"strings"

	"github.com/haikeilabs/agentware/go/reasoning"
)

type ToolCall struct {
	Tool string
	Args map[string]interface{}
}

type ValidationResult struct {
	ToolCalls  []ToolCall
	Nudge      *Nudge
	NeedsRetry bool
}

type ResponseValidator struct {
	ToolNames     []string
	RescueEnabled bool
	RetryNudgeFn  func(rawResponse string, toolNames []string) *Nudge
}

func NewResponseValidator(toolNames []string, rescueEnabled bool) *ResponseValidator {
	return &ResponseValidator{
		ToolNames:     toolNames,
		RescueEnabled: rescueEnabled,
		RetryNudgeFn:  RetryNudge,
	}
}

func (v *ResponseValidator) ValidateTextResponse(response string) ValidationResult {
	if v.RescueEnabled {
		rescued := v.RescueToolCall(response)
		if len(rescued) > 0 {
			return ValidationResult{
				ToolCalls:  rescued,
				Nudge:      nil,
				NeedsRetry: false,
			}
		}
	}

	return ValidationResult{
		ToolCalls:  nil,
		Nudge:      v.RetryNudgeFn(response, v.ToolNames),
		NeedsRetry: true,
	}
}

func (v *ResponseValidator) ValidateToolCalls(toolCalls []ToolCall) ValidationResult {
	unknown := make([]string, 0)
	validCalls := make([]ToolCall, 0)

	for _, tc := range toolCalls {
		if !v.isValidTool(tc.Tool) {
			unknown = append(unknown, tc.Tool)
		} else {
			validCalls = append(validCalls, tc)
		}
	}

	if len(unknown) > 0 {
		return ValidationResult{
			ToolCalls:  nil,
			Nudge:      UnknownToolNudge(unknown[0], v.ToolNames),
			NeedsRetry: true,
		}
	}

	return ValidationResult{
		ToolCalls:  validCalls,
		Nudge:      nil,
		NeedsRetry: false,
	}
}

func (v *ResponseValidator) isValidTool(name string) bool {
	for _, tn := range v.ToolNames {
		if tn == name {
			return true
		}
	}
	return false
}

func (v *ResponseValidator) RescueToolCall(response string) []ToolCall {
	cleaned := v.stripThinkTags(response)
	cleaned = v.stripPythonTag(cleaned)
	cleaned = strings.TrimSpace(cleaned)

	if cleaned == "" {
		return nil
	}

	calls := v.extractJSONToolCalls(cleaned)
	if len(calls) > 0 {
		return calls
	}

	calls = v.extractRehearsalToolCalls(cleaned)
	if len(calls) > 0 {
		return calls
	}

	calls = v.extractQwenXMLToolCalls(cleaned)
	return calls
}

func (v *ResponseValidator) stripThinkTags(text string) string {
	return thinkBlockPattern.ReplaceAllString(text, "")
}

// thinkBlockPattern matches a whole reasoning block for every pair in
// reasoning.TagPairs (think, mm:think, thinking, bracket THINK),
// case-insensitively and across newlines. It is built from the shared
// constants so the validator and the reasoning adapter recognize the same
// tags; never type a reasoning tag literally here (see go/reasoning/tags.go).
var thinkBlockPattern = func() *regexp.Regexp {
	pairs := reasoning.TagPairs()
	alts := make([]string, len(pairs))
	for i, p := range pairs {
		alts[i] = regexp.QuoteMeta(p.Open) + `.*?` + regexp.QuoteMeta(p.Close)
	}
	return regexp.MustCompile(`(?is)` + strings.Join(alts, "|"))
}()

func (v *ResponseValidator) stripPythonTag(text string) string {
	pythonTagPattern := regexp.MustCompile(`(?i)<\|python_tag\|>`)
	return pythonTagPattern.ReplaceAllString(text, "")
}

func (v *ResponseValidator) extractJSONToolCalls(text string) []ToolCall {
	codeFencePattern := regexp.MustCompile("```(?:json)?\\s*\\n?")
	cleaned := codeFencePattern.ReplaceAllString(text, "")

	cleaned = strings.Trim(cleaned, " \n\t")

	found := make([]ToolCall, 0)
	i := 0
	for i < len(cleaned) {
		if cleaned[i] == '{' {
			depth := 0
			var j int
			for j = i; j < len(cleaned); j++ {
				if cleaned[j] == '{' {
					depth++
				} else if cleaned[j] == '}' {
					depth--
					if depth == 0 {
						candidate := cleaned[i : j+1]
						if call := v.tryParseToolCall(candidate); call != nil {
							found = append(found, *call)
						}
						i = j + 1
						break
					}
				}
			}
			if depth != 0 {
				i++
			}
		} else {
			i++
		}
	}
	return found
}

func (v *ResponseValidator) tryParseToolCall(jsonStr string) *ToolCall {
	var data map[string]interface{}
	if err := json.Unmarshal([]byte(jsonStr), &data); err != nil {
		return nil
	}

	toolName, ok := data["tool"].(string)
	if !ok {
		toolName, ok = data["name"].(string)
	}
	if !ok {
		return nil
	}

	if !v.isValidTool(toolName) {
		return nil
	}

	args, ok := data["args"].(map[string]interface{})
	if !ok {
		args, ok = data["arguments"].(map[string]interface{})
		if !ok {
			args = make(map[string]interface{})
		}
	}

	return &ToolCall{Tool: toolName, Args: args}
}

var rehearsalPattern = regexp.MustCompile(`(\w+)\[ARGS\](\{.*\})`)

func (v *ResponseValidator) extractRehearsalToolCalls(text string) []ToolCall {
	found := make([]ToolCall, 0)

	matches := rehearsalPattern.FindAllStringSubmatch(text, -1)
	for _, match := range matches {
		if len(match) != 3 {
			continue
		}

		toolName := match[1]
		argsStr := match[2]

		if !v.isValidTool(toolName) {
			continue
		}

		var args map[string]interface{}
		if err := json.Unmarshal([]byte(argsStr), &args); err != nil {
			continue
		}

		if args != nil {
			found = append(found, ToolCall{Tool: toolName, Args: args})
		}
	}

	return found
}

func (v *ResponseValidator) extractQwenXMLToolCalls(text string) []ToolCall {
	return []ToolCall{}
}
