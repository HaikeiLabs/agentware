package toolformat

import (
	"fmt"
	"regexp"
	"strings"
	"unicode"

	"github.com/haikeilabs/agentware/go/tools"
	"github.com/haikeilabs/agentware/go/toolspec"
)

// MiniMaxVersion selects the MiniMax tool-definition prompt.
type MiniMaxVersion string

const (
	// MiniMaxM2 is the M2 prompt (parameter tags). The zero value means M2.
	MiniMaxM2 MiniMaxVersion = "m2"
	// MiniMaxM3 is the M3 prompt (namespaced, nested elements).
	MiniMaxM3 MiniMaxVersion = "m3"
)

// MiniMaxM3Namespace is the marker MiniMax M3 puts before every structural
// tag of a tool call.
const MiniMaxM3Namespace = "]<]minimax[>["

// MiniMaxFormatter renders and parses MiniMax M2 and M3 tool calls as the
// official chat templates render them. Version picks the tool-definition
// prompt; parsing and validation accept both syntaxes, and a tool result is a
// response block in both. Parameter values are not coerced to the tool
// schema: they arrive as strings (M3 nested elements as objects, repeated
// names as lists), the same as vLLM's parsers without a request schema.
type MiniMaxFormatter struct {
	// Version is MiniMaxM2 (also the zero value) or MiniMaxM3. Any other
	// value renders the M2 prompt.
	Version MiniMaxVersion
}

const (
	mm2Block      = "<minimax:tool_call>"
	mm2InvokeOpen = "<invoke name="
	mm3ToolCall   = MiniMaxM3Namespace + "<tool_call>"
	mm3Invoke     = MiniMaxM3Namespace + "<invoke name="
	mm3InvokeEnd  = MiniMaxM3Namespace + "</invoke>"
	mm3Element    = MiniMaxM3Namespace + "<"
	mm3MaxDepth   = 128
	mm3MixedText  = "$text"

	mmToolsHeader = "# Tools\nYou may call one or more tools to assist with the user query.\n" +
		"Here are the tools available in JSONSchema format:\n\n<tools>\n"
	mm2Instructions = "When making tool calls, use XML format to invoke tools and pass parameters:\n\n" +
		"<minimax:tool_call>\n<invoke name=\"tool-name-1\">\n" +
		"<parameter name=\"param-key-1\">param-value-1</parameter>\n" +
		"<parameter name=\"param-key-2\">param-value-2</parameter>\n" +
		"...\n</invoke>\n</minimax:tool_call>"
)

func mm3Instructions() string {
	ns := MiniMaxM3Namespace
	return "To call tools, wrap all invocations in a single " + ns + "<tool_call>" + ns + "</tool_call> block. " +
		"Parameter values containing nested objects or arrays are recursively expanded into XML elements. Example:\n\n" +
		ns + "<tool_call>\n" +
		ns + `<invoke name="tool-name-1">` + ns + "<param-1>value-1" + ns + "</param-1>" + ns + "<param-2>" +
		ns + "<item>" + ns + "<key-a>val-a" + ns + "</key-a>" + ns + "<key-b>val-b" + ns + "</key-b>" + ns + "</item>" +
		ns + "</param-2>" + ns + "</invoke>\n" +
		ns + `<invoke name="tool-name-2">` + ns + "<param-1>value-1" + ns + "</param-1>" + ns + "</invoke>\n" +
		ns + "</tool_call>"
}

var (
	mm2InvokeRe     = regexp.MustCompile(`(?s)<invoke name=(?:"([^"]*)"|'([^']*)'|([^>\s]*))>(.*?)</invoke>`)
	mm2ParamOpenRe  = regexp.MustCompile(`<\s*parameter\s+name\s*=\s*(?:"([^"]*)"|'([^']*)'|([^>\s]+))\s*>`)
	mm2ParamCloseRe = regexp.MustCompile(`<\s*/\s*parameter\s*>`)
)

func (f *MiniMaxFormatter) version() MiniMaxVersion {
	if f.Version == MiniMaxM3 {
		return MiniMaxM3
	}
	return MiniMaxM2
}

func (f *MiniMaxFormatter) FormatToolDefinitions(toolsList []tools.Tool) string {
	return f.formatToolDefinitions(adaptTools(toolsList))
}

// FormatToolSpecs formats the model-facing fields from shared ToolSpecs.
func (f *MiniMaxFormatter) FormatToolSpecs(specs []toolspec.ToolSpec) string {
	return f.formatToolDefinitions(adaptToolSpecs(specs))
}

func (f *MiniMaxFormatter) formatToolDefinitions(toolsList []formatTool) string {
	if len(toolsList) == 0 {
		return ""
	}
	lines := make([]string, 0, len(toolsList))
	for _, t := range toolsList {
		fn := map[string]any{"name": t.Name(), "description": t.Description()}
		if s := t.InputSchema(); s != nil {
			fn["parameters"] = s
		}
		// qwenJSON is compact, key-sorted and does not HTML-escape.
		lines = append(lines, "<tool>"+qwenJSON(fn)+"</tool>")
	}
	instructions := mm2Instructions
	if f.version() == MiniMaxM3 {
		instructions = mm3Instructions()
	}
	return mmToolsHeader + strings.Join(lines, "\n") + "\n</tools>\n\n" + instructions
}

type minimaxInvoke struct {
	name     string
	args     map[string]any
	raw      string
	complete bool
}

func firstNonEmpty(groups ...string) string {
	for _, g := range groups {
		if g != "" {
			return g
		}
	}
	return ""
}

// mm2Params parses M2 parameter tags; complete is false if one was dropped
// unterminated. A value runs to its closing tag or, as in vLLM, to the next
// parameter tag. Values stay verbatim strings: the template writes strings as
// is and other values as JSON, and with no schema the two cannot be told apart.
func mm2Params(body string) (map[string]any, bool) {
	openers := mm2ParamOpenRe.FindAllStringSubmatchIndex(body, -1)
	params := map[string]any{}
	complete := true
	for i, op := range openers {
		end := len(body)
		if i+1 < len(openers) {
			end = openers[i+1][0]
		}
		region := body[op[1]:end]
		var value string
		if loc := mm2ParamCloseRe.FindStringIndex(region); loc != nil {
			value = region[:loc[0]]
		} else if i+1 < len(openers) {
			value = region
		} else {
			complete = false
			continue
		}
		group := func(n int) string {
			if op[2*n] < 0 {
				return ""
			}
			return body[op[2*n]:op[2*n+1]]
		}
		if name := strings.TrimSpace(firstNonEmpty(group(1), group(2), group(3))); name != "" {
			params[name] = value
		}
	}
	return params, complete
}

// mm3Node is an M3 element value: its text, or its children in order.
type mm3Node struct {
	text     string
	children []mm3Child
}

type mm3Child struct {
	name  string
	value mm3Node
}

// mm3ParseElement parses the M3 element starting at pos; ok is false if it is
// malformed.
func mm3ParseElement(text string, pos, depth int) (name string, node mm3Node, next int, ok bool) {
	if depth > mm3MaxDepth {
		return "", mm3Node{}, 0, false
	}
	nameStart := pos + len(mm3Element)
	rel := strings.Index(text[nameStart:], ">")
	if rel <= 0 {
		return "", mm3Node{}, 0, false
	}
	nameEnd := nameStart + rel
	name = text[nameStart:nameEnd]
	if strings.HasPrefix(name, "/") || strings.TrimSpace(name) == "" {
		return "", mm3Node{}, 0, false
	}
	closeTag := MiniMaxM3Namespace + "</" + name + ">"
	pos = nameEnd + 1
	var body strings.Builder
	var children []mm3Child
	for {
		rel := strings.Index(text[pos:], MiniMaxM3Namespace)
		if rel < 0 {
			return "", mm3Node{}, 0, false
		}
		marker := pos + rel
		body.WriteString(text[pos:marker])
		if strings.HasPrefix(text[marker:], closeTag) {
			pos = marker + len(closeTag)
			break
		}
		if !strings.HasPrefix(text[marker:], mm3Element) {
			return "", mm3Node{}, 0, false
		}
		childName, child, childNext, childOK := mm3ParseElement(text, marker, depth+1)
		if !childOK {
			return "", mm3Node{}, 0, false
		}
		children = append(children, mm3Child{name: childName, value: child})
		pos = childNext
	}
	bodyText := body.String()
	if len(children) == 0 {
		return name, mm3Node{text: bodyText}, pos, true
	}
	if strings.TrimSpace(bodyText) != "" {
		field := mm3MixedText
		for mm3HasChild(children, field) {
			field = "$" + field
		}
		children = append(children, mm3Child{name: field, value: mm3Node{text: bodyText}})
	}
	return name, mm3Node{children: children}, pos, true
}

func mm3HasChild(children []mm3Child, name string) bool {
	for _, c := range children {
		if c.name == name {
			return true
		}
	}
	return false
}

// mm3Params parses an M3 invoke body; complete is false if parsing stopped at
// a malformed element (the elements before it are kept).
func mm3Params(body string) ([]mm3Child, bool) {
	var params []mm3Child
	pos := 0
	for {
		for pos < len(body) && unicode.IsSpace(rune(body[pos])) {
			pos++
		}
		if pos == len(body) {
			return params, true
		}
		if !strings.HasPrefix(body[pos:], mm3Element) {
			return params, false
		}
		name, node, next, ok := mm3ParseElement(body, pos, 1)
		if !ok {
			return params, false
		}
		params = append(params, mm3Child{name: name, value: node})
		pos = next
	}
}

// mm3Object converts elements to an object without a schema; a repeated name
// collects into a list.
func mm3Object(elements []mm3Child) map[string]any {
	result := map[string]any{}
	repeated := map[string]bool{}
	for _, el := range elements {
		var value any = el.value.text
		if el.value.children != nil {
			value = mm3Object(el.value.children)
		}
		existing, seen := result[el.name]
		switch {
		case !seen:
			result[el.name] = value
		case repeated[el.name]:
			result[el.name] = append(existing.([]any), value)
		default:
			result[el.name] = []any{existing, value}
			repeated[el.name] = true
		}
	}
	return result
}

func mm3Invokes(response string) ([]minimaxInvoke, error) {
	start := strings.Index(response, mm3ToolCall)
	if start < 0 {
		if strings.Contains(response, mm3Invoke) {
			return nil, fmt.Errorf("MiniMax M3 invoke outside a tool_call block")
		}
		return nil, nil
	}
	var invokes []minimaxInvoke
	pos := start + len(mm3ToolCall)
	for {
		rel := strings.Index(response[pos:], mm3Invoke)
		if rel < 0 {
			break
		}
		begin := pos + rel
		nameStart := begin + len(mm3Invoke)
		var nameEnd, bodyStart int
		if nameStart < len(response) && (response[nameStart] == '"' || response[nameStart] == '\'') {
			quote := response[nameStart : nameStart+1]
			nameStart++
			nameEnd = strings.Index(response[nameStart:], quote+">")
			if nameEnd >= 0 {
				nameEnd += nameStart
			}
			bodyStart = nameEnd + 2
		} else {
			nameEnd = strings.Index(response[nameStart:], ">")
			if nameEnd >= 0 {
				nameEnd += nameStart
			}
			bodyStart = nameEnd + 1
		}
		if nameEnd < 0 {
			return invokes, fmt.Errorf("unterminated MiniMax invoke")
		}
		endRel := strings.Index(response[bodyStart:], mm3InvokeEnd)
		if endRel < 0 {
			return invokes, fmt.Errorf("unterminated MiniMax invoke")
		}
		end := bodyStart + endRel
		params, complete := mm3Params(response[bodyStart:end])
		pos = end + len(mm3InvokeEnd)
		invokes = append(invokes, minimaxInvoke{
			name:     strings.TrimSpace(response[nameStart:nameEnd]),
			args:     mm3Object(params),
			raw:      response[begin:pos],
			complete: complete,
		})
	}
	if len(invokes) == 0 {
		return nil, fmt.Errorf("MiniMax tool_call block has no invoke")
	}
	return invokes, nil
}

// mm2Invokes finds M2 invokes anywhere in the response, as vLLM does.
func mm2Invokes(response string) ([]minimaxInvoke, error) {
	var invokes []minimaxInvoke
	for _, m := range mm2InvokeRe.FindAllStringSubmatch(response, -1) {
		args, complete := mm2Params(m[4])
		invokes = append(invokes, minimaxInvoke{
			name:     strings.TrimSpace(firstNonEmpty(m[1], m[2], m[3])),
			args:     args,
			raw:      m[0],
			complete: complete,
		})
	}
	if strings.Count(response, mm2InvokeOpen) != len(invokes) {
		return invokes, fmt.Errorf("unterminated MiniMax invoke")
	}
	if len(invokes) == 0 && strings.Contains(response, mm2Block) {
		return nil, fmt.Errorf("MiniMax tool_call block has no invoke")
	}
	return invokes, nil
}

// minimaxInvokes parses a response with the M3 namespace marker as M3 and any
// other as M2.
func minimaxInvokes(response string) ([]minimaxInvoke, error) {
	if strings.Contains(response, MiniMaxM3Namespace) {
		return mm3Invokes(response)
	}
	return mm2Invokes(response)
}

// ParseToolCalls returns the well-formed calls; an invoke without a name is
// skipped. Format errors are reported by ValidateFormat, not here.
func (f *MiniMaxFormatter) ParseToolCalls(response string) ([]ParsedToolCall, error) {
	invokes, _ := minimaxInvokes(response)
	var calls []ParsedToolCall
	for _, inv := range invokes {
		if inv.name != "" {
			calls = append(calls, ParsedToolCall{Name: inv.name, Args: inv.args, Raw: inv.raw})
		}
	}
	return calls, nil
}

// FormatToolResult renders the result inside response tags. Output goes in
// verbatim: both chat templates emit a tool message's content unchanged, and
// structured data is already JSON text. A failure renders as {"error": ...}.
func (f *MiniMaxFormatter) FormatToolResult(_ string, result *tools.Result, _ ...string) string {
	text := result.Output
	if !result.Success {
		text = qwenJSON(map[string]any{"error": result.Error})
	}
	return "<response>" + text + "</response>"
}

func (f *MiniMaxFormatter) ModelFamily() string {
	return "minimax"
}

func (f *MiniMaxFormatter) ValidateFormat(response string) error {
	invokes, err := minimaxInvokes(response)
	if err != nil {
		return err
	}
	for _, inv := range invokes {
		if inv.name == "" {
			return fmt.Errorf("MiniMax invoke missing tool name")
		}
		if !inv.complete {
			return fmt.Errorf("MiniMax invoke %q has a malformed or unterminated parameter", inv.name)
		}
	}
	return nil
}
