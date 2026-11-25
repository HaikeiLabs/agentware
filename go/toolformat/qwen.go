package toolformat

import (
	"bytes"
	"encoding/json"
	"fmt"
	"regexp"
	"strings"

	"github.com/haikeilabs/agentware/go/tools"
	"github.com/haikeilabs/agentware/go/toolspec"
)

// QwenFormatter implements the Hermes tool format and deprecated legacy parsing.
type QwenFormatter struct{}

var qwenSpan = regexp.MustCompile(`(?s)<tool_call>(.*?)</tool_call>`)
var qwenLegacy = regexp.MustCompile(`(?s)^<tool name="([^"]+)">\s*(.*?)\s*</tool>$`)

func qwenJSON(v any) string {
	var b bytes.Buffer
	e := json.NewEncoder(&b)
	e.SetEscapeHTML(false)
	if err := e.Encode(v); err != nil {
		return "null"
	}
	return strings.TrimSuffix(b.String(), "\n")
}
func (f *QwenFormatter) FormatToolDefinitions(t []tools.Tool) string {
	return f.formatToolDefinitions(adaptTools(t))
}
func (f *QwenFormatter) FormatToolSpecs(t []toolspec.ToolSpec) string {
	return f.formatToolDefinitions(adaptToolSpecs(t))
}
func (f *QwenFormatter) formatToolDefinitions(ts []formatTool) string {
	if len(ts) == 0 {
		return ""
	}
	lines := []string{}
	for _, t := range ts {
		fn := map[string]any{"name": t.Name(), "description": t.Description()}
		if et, ok := t.(interface{ InputSchema() map[string]any }); ok {
			if s := et.InputSchema(); s != nil {
				fn["parameters"] = s
			}
		}
		lines = append(lines, qwenJSON(map[string]any{"type": "function", "function": fn}))
	}
	return "# Tools\n\nYou may call one or more functions to assist with the user query.\n\nYou are provided with function signatures within <tools></tools> XML tags:\n<tools>\n" + strings.Join(lines, "\n") + "\n</tools>\n\nFor each function call, return a json object with function name and arguments within <tool_call></tool_call> XML tags:\n<tool_call>\n{\"name\": <function-name>, \"arguments\": <args-json-object>}\n</tool_call>"
}
func qwenDecode(body string) (string, map[string]any, bool) {
	var c struct {
		Name      string          `json:"name"`
		Arguments json.RawMessage `json:"arguments"`
	}
	if json.Unmarshal([]byte(body), &c) != nil || c.Name == "" {
		return "", nil, false
	}
	raw := c.Arguments
	if len(raw) > 0 && raw[0] == '"' {
		var s string
		if json.Unmarshal(raw, &s) != nil {
			return "", nil, false
		}
		raw = []byte(s)
	}
	var args map[string]any
	if json.Unmarshal(raw, &args) != nil || args == nil {
		return "", nil, false
	}
	return c.Name, args, true
}
func qwenSpans(response string) ([][]string, bool) {
	spans := qwenSpan.FindAllStringSubmatch(response, -1)
	for _, s := range spans {
		if !strings.HasPrefix(strings.TrimSpace(s[1]), "<tool") {
			return spans, true
		}
	}
	return spans, false
}
func (f *QwenFormatter) ParseToolCalls(response string) ([]ParsedToolCall, error) {
	spans, hermes := qwenSpans(response)
	var calls []ParsedToolCall
	for _, s := range spans {
		body := strings.TrimSpace(s[1])
		if hermes {
			if strings.HasPrefix(body, "<tool") {
				continue
			}
			name, args, ok := qwenDecode(body)
			if ok {
				calls = append(calls, ParsedToolCall{Name: name, Args: args, Raw: s[0]})
			}
		} else if m := qwenLegacy.FindStringSubmatch(body); m != nil {
			var args map[string]any
			if strings.TrimSpace(m[2]) == "" {
				args = map[string]any{}
			} else if json.Unmarshal([]byte(m[2]), &args) != nil || args == nil {
				continue
			}
			calls = append(calls, ParsedToolCall{Name: m[1], Args: args, Raw: s[0]})
		}
	}
	return calls, nil
}
func (f *QwenFormatter) FormatToolResult(_ string, r *tools.Result, _ ...string) string {
	var value any = r.Output
	if !r.Success {
		value = map[string]any{"error": r.Error}
	}
	return "<tool_response>\n" + qwenJSON(value) + "\n</tool_response>"
}
func (f *QwenFormatter) ModelFamily() string { return "qwen" }
func (f *QwenFormatter) ValidateFormat(response string) error {
	spans, hermes := qwenSpans(response)
	for _, s := range spans {
		body := strings.TrimSpace(s[1])
		if hermes {
			if strings.HasPrefix(body, "<tool") {
				continue
			}
			if _, _, ok := qwenDecode(body); !ok {
				return fmt.Errorf("invalid JSON tool call or arguments")
			}
		} else {
			m := qwenLegacy.FindStringSubmatch(body)
			if m == nil {
				return fmt.Errorf("tool call missing function name")
			}
			var args map[string]any
			if json.Unmarshal([]byte(m[2]), &args) != nil || args == nil {
				return fmt.Errorf("invalid JSON arguments")
			}
		}
	}
	return nil
}
