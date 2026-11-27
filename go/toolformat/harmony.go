package toolformat

// GPT-OSS Harmony format: render prompts and parse raw completions.
//
// GPT-OSS models are trained on the Harmony format. Over a chat-completions
// API the server applies it; over raw /v1/completions the caller renders the
// whole prompt and parses the completion text. HarmonyFormatter is the
// ToolFormatter for the family; RenderHarmonyPrompt renders a whole
// conversation; ParseHarmonyCompletion splits completion text into messages,
// with the analysis channel as the reasoning and the final channel as the
// answer.
//
// The rendering follows the reference renderer in openai/harmony
// (src/encoding.rs), including its JSON Schema to TypeScript mapping. One
// difference is Go's: a map[string]any has no key order, so object
// properties render required ones first (in "required" order) and the rest
// by name, where the reference keeps schema order. The shared fixture
// fixtures/toolformat/harmony-cases.json replays openai/harmony's test-data.

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"sort"
	"strings"

	"github.com/haikeilabs/agentware/go/tools"
	"github.com/haikeilabs/agentware/go/toolspec"
)

const (
	harmonyStart     = "<|start|>"
	harmonyEnd       = "<|end|>"
	harmonyMessage   = "<|message|>"
	harmonyChannel   = "<|channel|>"
	harmonyConstrain = "<|constrain|>"
	harmonyCall      = "<|call|>"
	harmonyReturn    = "<|return|>"

	harmonyFunctionsPrefix = "functions."

	// HarmonyJSONContentType is the content type of a JSON tool call.
	HarmonyJSONContentType = harmonyConstrain + "json"
	// DefaultHarmonyModelIdentity is the model identity GPT-OSS was trained with.
	DefaultHarmonyModelIdentity = "You are ChatGPT, a large language model trained by OpenAI."
)

// ErrInvalidHarmonyMessage reports a message RenderHarmonyMessage cannot render.
var ErrInvalidHarmonyMessage = errors.New("invalid harmony message")

// HarmonyStopTokens returns the stop sequences for a raw completion: the end
// of a final answer and the end of a tool call. Servers usually strip the
// stop sequence from the text, which the parser accepts.
func HarmonyStopTokens() []string { return []string{harmonyReturn, harmonyCall} }

var harmonyTerminators = []string{harmonyEnd, harmonyCall, harmonyReturn}

var harmonyRoles = map[string]bool{"system": true, "developer": true, "user": true, "assistant": true, "tool": true}

// HarmonyMessage is one Harmony message. Role is system, developer, user,
// assistant or tool. A tool message's Name is its author, such as
// functions.get_weather; for other roles a Name renders as role:name.
// Recipient is the to= target (functions.NAME for a call). ContentType
// follows the channel, such as HarmonyJSONContentType.
type HarmonyMessage struct {
	Role        string
	Content     string
	Channel     string
	Recipient   string
	ContentType string
	Name        string
}

// HarmonySystem is the system message. An empty field is left out of the
// rendering; DefaultHarmonySystem returns the defaults GPT-OSS expects.
type HarmonySystem struct {
	ModelIdentity   string
	KnowledgeCutoff string
	CurrentDate     string
	// ReasoningEffort is low, medium, high or empty.
	ReasoningEffort string
	ValidChannels   []string
}

// DefaultHarmonySystem returns the system message the openai/harmony
// renderer uses by default, with medium reasoning and no date.
func DefaultHarmonySystem() HarmonySystem {
	return HarmonySystem{
		ModelIdentity:   DefaultHarmonyModelIdentity,
		KnowledgeCutoff: "2024-06",
		ReasoningEffort: "medium",
		ValidChannels:   []string{"analysis", "commentary", "final"},
	}
}

// HarmonyPromptOptions configures RenderHarmonyPrompt. The zero value renders
// the default system message, drops finished analysis and opens an
// assistant turn.
type HarmonyPromptOptions struct {
	// System is the system message; nil means DefaultHarmonySystem.
	System *HarmonySystem
	// OmitSystem leaves the system message out.
	OmitSystem bool
	// Instructions and the tools go in the developer message.
	Instructions string
	Tools        []tools.Tool
	ToolSpecs    []toolspec.ToolSpec
	// KeepAnalysis turns off dropping analysis messages before the first
	// final message once the last assistant message is a final one.
	KeepAnalysis bool
	// NextRole opens the turn to sample; empty means assistant.
	NextRole string
	// OmitNextRole renders the conversation alone.
	OmitNextRole bool
}

// HarmonyCompletion is a parsed completion. Reasoning joins the assistant's
// analysis-channel messages and Content its final-channel (or channel-less)
// messages, each with a newline. ToolCalls are the calls to the functions
// namespace whose arguments are a JSON object; others are left out (fail
// closed), and ValidateFormat reports them.
type HarmonyCompletion struct {
	Messages  []HarmonyMessage
	Reasoning string
	Content   string
	ToolCalls []ParsedToolCall
}

// HarmonyFormatter implements the GPT-OSS Harmony tool format.
type HarmonyFormatter struct{}

// FormatToolDefinitions renders the developer message's tools section.
func (f *HarmonyFormatter) FormatToolDefinitions(t []tools.Tool) string {
	return harmonyToolsSection(adaptTools(t))
}

// FormatToolSpecs renders the developer message's tools section.
func (f *HarmonyFormatter) FormatToolSpecs(t []toolspec.ToolSpec) string {
	return harmonyToolsSection(adaptToolSpecs(t))
}

// ParseToolCalls returns the completion's calls to the functions namespace.
func (f *HarmonyFormatter) ParseToolCalls(response string) ([]ParsedToolCall, error) {
	return ParseHarmonyCompletion(response).ToolCalls, nil
}

// FormatToolResult renders a functions.NAME tool message addressed to the
// assistant. Harmony results carry no call id. A failure renders as
// {"error": ...}.
func (f *HarmonyFormatter) FormatToolResult(name string, r *tools.Result, _ ...string) string {
	text := r.Output
	if !r.Success {
		text = harmonyJSON(map[string]any{"error": r.Error})
	}
	out, _ := RenderHarmonyMessage(HarmonyMessage{
		Role: "tool", Content: text, Channel: "commentary", Recipient: "assistant", Name: harmonyFunctionsPrefix + name,
	})
	return out
}

func (f *HarmonyFormatter) ModelFamily() string { return "gpt-oss" }

// ValidateFormat reports a call to the functions namespace with no name or
// with arguments that are not a JSON object.
func (f *HarmonyFormatter) ValidateFormat(response string) error {
	for _, m := range parseHarmonyMessages(response) {
		if m.Role != "assistant" || !strings.HasPrefix(m.Recipient, harmonyFunctionsPrefix) {
			continue
		}
		name := strings.TrimPrefix(m.Recipient, harmonyFunctionsPrefix)
		if name == "" {
			return fmt.Errorf("tool call missing function name")
		}
		var args any
		if err := json.Unmarshal([]byte(m.Content), &args); err != nil {
			return fmt.Errorf("tool call %q has invalid JSON arguments: %w", name, err)
		}
		if _, ok := args.(map[string]any); !ok {
			return fmt.Errorf("tool call %q arguments must be a JSON object", name)
		}
	}
	return nil
}

// RenderHarmonyMessage renders one message. An assistant message with a
// recipient ends with the call token; every other message with the end token.
func RenderHarmonyMessage(m HarmonyMessage) (string, error) {
	if !harmonyRoles[m.Role] {
		return "", fmt.Errorf("%w: unknown role %q", ErrInvalidHarmonyMessage, m.Role)
	}
	var b strings.Builder
	b.WriteString(harmonyStart)
	if m.Role == "tool" {
		if m.Name == "" {
			return "", fmt.Errorf("%w: a tool message needs a name, such as functions.get_weather", ErrInvalidHarmonyMessage)
		}
		b.WriteString(m.Name)
	} else {
		b.WriteString(m.Role)
		if m.Name != "" {
			b.WriteString(":" + m.Name)
		}
	}
	if m.Recipient != "" && m.Recipient != "all" {
		b.WriteString(" to=" + m.Recipient)
	}
	if m.Channel != "" {
		b.WriteString(harmonyChannel + m.Channel)
	}
	if m.ContentType != "" {
		b.WriteString(" " + m.ContentType)
	}
	b.WriteString(harmonyMessage + m.Content)
	if m.Role == "assistant" && m.Recipient != "" {
		b.WriteString(harmonyCall)
	} else {
		b.WriteString(harmonyEnd)
	}
	return b.String(), nil
}

// RenderHarmonyPrompt renders a conversation as a Harmony prompt for a raw
// completion: the system message, a developer message holding the
// instructions and the tools namespace when either is given, then messages.
// Analysis messages before the first final message are dropped once the last
// assistant message is a final one, as the reference renderer does: the chain
// of thought of a finished answer is not sent back, while an unfinished
// tool-calling turn keeps it.
func RenderHarmonyPrompt(messages []HarmonyMessage, opts HarmonyPromptOptions) (string, error) {
	var b strings.Builder
	toolList := append(adaptTools(opts.Tools), adaptToolSpecs(opts.ToolSpecs)...)
	render := func(m HarmonyMessage) error {
		s, err := RenderHarmonyMessage(m)
		b.WriteString(s)
		return err
	}
	if !opts.OmitSystem {
		system := DefaultHarmonySystem()
		if opts.System != nil {
			system = *opts.System
		}
		text, err := harmonySystemText(system, len(toolList) > 0)
		if err != nil {
			return "", err
		}
		if err := render(HarmonyMessage{Role: "system", Content: text}); err != nil {
			return "", err
		}
	}
	if opts.Instructions != "" || len(toolList) > 0 {
		var sections []string
		if opts.Instructions != "" {
			sections = append(sections, "# Instructions", opts.Instructions)
		}
		if len(toolList) > 0 {
			sections = append(sections, harmonyToolsSection(toolList))
		}
		if err := render(HarmonyMessage{Role: "developer", Content: strings.Join(sections, "\n\n")}); err != nil {
			return "", err
		}
	}
	history := messages
	if !opts.KeepAnalysis {
		history = harmonyDropAnalysis(messages)
	}
	for _, m := range history {
		if err := render(m); err != nil {
			return "", err
		}
	}
	if !opts.OmitNextRole {
		role := opts.NextRole
		if role == "" {
			role = "assistant"
		}
		b.WriteString(harmonyStart + role)
	}
	return b.String(), nil
}

// ParseHarmonyCompletion parses completion text that follows a prompt ending
// in <|start|>assistant. The first message may have no header role (the
// prompt opened it). A message ends at an end, call or return token, or at
// the end of the text, since servers strip the stop sequence. A call's
// recipient is read from either header position: after the role (the
// reference renderer) or after the channel (what the model emits).
func ParseHarmonyCompletion(text string) HarmonyCompletion {
	c := HarmonyCompletion{Messages: parseHarmonyMessages(text)}
	var reasoning, content []string
	for _, m := range c.Messages {
		if m.Role != "assistant" {
			continue
		}
		switch {
		case strings.HasPrefix(m.Recipient, harmonyFunctionsPrefix):
			if call, ok := harmonyDecodeCall(m, text); ok {
				c.ToolCalls = append(c.ToolCalls, call)
			}
		case m.Channel == "analysis":
			reasoning = append(reasoning, m.Content)
		case m.Channel == "final" || (m.Channel == "" && m.Recipient == ""):
			content = append(content, m.Content)
		}
	}
	c.Reasoning = strings.Join(reasoning, "\n")
	c.Content = strings.Join(content, "\n")
	return c
}

func harmonyDecodeCall(m HarmonyMessage, raw string) (ParsedToolCall, bool) {
	name := strings.TrimPrefix(m.Recipient, harmonyFunctionsPrefix)
	if name == "" {
		return ParsedToolCall{}, false
	}
	var args map[string]any
	if json.Unmarshal([]byte(m.Content), &args) != nil || args == nil {
		return ParsedToolCall{}, false
	}
	return ParsedToolCall{Name: name, Args: args, Raw: raw}, true
}

func parseHarmonyMessages(text string) []HarmonyMessage {
	var messages []HarmonyMessage
	for i, chunk := range strings.Split(text, harmonyStart) {
		if chunk == "" {
			continue
		}
		if m, ok := parseHarmonyChunk(chunk, i == 0); ok {
			messages = append(messages, m)
		}
	}
	return messages
}

func parseHarmonyChunk(chunk string, first bool) (HarmonyMessage, bool) {
	header, body, found := strings.Cut(chunk, harmonyMessage)
	if !found {
		// Text before any header is a bare answer; a header with no message
		// token was cut off and carries no content.
		if first && !strings.Contains(chunk, harmonyChannel) && !strings.Contains(chunk, harmonyConstrain) &&
			!containsAny(chunk, harmonyTerminators) {
			return HarmonyMessage{Role: "assistant", Content: chunk}, true
		}
		return HarmonyMessage{}, false
	}
	cut := len(body)
	for _, t := range harmonyTerminators {
		if i := strings.Index(body, t); i >= 0 && i < cut {
			cut = i
		}
	}
	m := HarmonyMessage{Role: "assistant", Content: body[:cut]}

	if before, constrained, ok := strings.Cut(header, harmonyConstrain); ok {
		header = before
		m.ContentType = harmonyConstrain + strings.TrimSpace(constrained)
	}
	head, channelPart, _ := strings.Cut(header, harmonyChannel)
	var roleWords []string
	for _, w := range strings.Fields(head) {
		if r, ok := strings.CutPrefix(w, "to="); ok {
			m.Recipient = r
		} else {
			roleWords = append(roleWords, w)
		}
	}
	var extra []string
	if words := strings.Fields(channelPart); len(words) > 0 {
		m.Channel = words[0]
		for _, w := range words[1:] {
			if r, ok := strings.CutPrefix(w, "to="); ok {
				m.Recipient = r
			} else {
				extra = append(extra, w)
			}
		}
	}
	if m.ContentType == "" && len(extra) > 0 {
		m.ContentType = strings.Join(extra, " ")
	}
	if len(roleWords) > 0 {
		author := roleWords[0]
		base, suffix, _ := strings.Cut(author, ":")
		if harmonyRoles[base] && base != "tool" {
			m.Role, m.Name = base, suffix
		} else {
			m.Role, m.Name = "tool", author
		}
	}
	return m, true
}

func containsAny(s string, subs []string) bool {
	for _, sub := range subs {
		if strings.Contains(s, sub) {
			return true
		}
	}
	return false
}

func harmonyDropAnalysis(messages []HarmonyMessage) []HarmonyMessage {
	lastAssistantIsFinal := false
	for i := len(messages) - 1; i >= 0; i-- {
		if messages[i].Role == "assistant" {
			lastAssistantIsFinal = messages[i].Channel == "final"
			break
		}
	}
	firstFinal := -1
	for i, m := range messages {
		if m.Channel == "final" {
			firstFinal = i
			break
		}
	}
	if !lastAssistantIsFinal || firstFinal < 0 {
		return messages
	}
	kept := make([]HarmonyMessage, 0, len(messages))
	for i, m := range messages {
		if i < firstFinal && m.Channel == "analysis" {
			continue
		}
		kept = append(kept, m)
	}
	return kept
}

func harmonySystemText(s HarmonySystem, hasFunctionTools bool) (string, error) {
	switch s.ReasoningEffort {
	case "", "low", "medium", "high":
	default:
		return "", fmt.Errorf("%w: reasoning effort must be low, medium or high, not %q", ErrInvalidHarmonyMessage, s.ReasoningEffort)
	}
	var sections, top []string
	if s.ModelIdentity != "" {
		top = append(top, s.ModelIdentity)
	}
	if s.KnowledgeCutoff != "" {
		top = append(top, "Knowledge cutoff: "+s.KnowledgeCutoff)
	}
	if s.CurrentDate != "" {
		top = append(top, "Current date: "+s.CurrentDate)
	}
	if len(top) > 0 {
		sections = append(sections, strings.Join(top, "\n"))
	}
	if s.ReasoningEffort != "" {
		sections = append(sections, "Reasoning: "+s.ReasoningEffort)
	}
	if len(s.ValidChannels) > 0 {
		header := "# Valid channels: " + strings.Join(s.ValidChannels, ", ") + ". Channel must be included for every message."
		if hasFunctionTools {
			header += "\nCalls to these tools must go to the commentary channel: 'functions'."
		}
		sections = append(sections, header)
	}
	return strings.Join(sections, "\n\n"), nil
}

func harmonyToolsSection(ts []formatTool) string {
	if len(ts) == 0 {
		return ""
	}
	lines := []string{"## functions\n", "namespace functions {\n"}
	for _, t := range ts {
		for _, line := range harmonyLines(t.Description()) {
			lines = append(lines, "// "+line)
		}
		if schema := t.InputSchema(); schema != nil {
			lines = append(lines, "type "+t.Name()+" = (_: "+harmonyTypeScript(schema, "")+") => any;\n")
		} else {
			lines = append(lines, "type "+t.Name()+" = () => any;\n")
		}
	}
	lines = append(lines, "} // namespace functions")
	return "# Tools\n\n" + strings.Join(lines, "\n")
}

// harmonyLines splits like Rust's str::lines: no trailing empty line, and a
// trailing \r is dropped from each line.
func harmonyLines(text string) []string {
	if text == "" {
		return nil
	}
	parts := strings.Split(text, "\n")
	if parts[len(parts)-1] == "" {
		parts = parts[:len(parts)-1]
	}
	for i, p := range parts {
		parts[i] = strings.TrimSuffix(p, "\r")
	}
	return parts
}

func harmonyJSON(v any) string {
	var b bytes.Buffer
	e := json.NewEncoder(&b)
	e.SetEscapeHTML(false)
	if err := e.Encode(v); err != nil {
		return "null"
	}
	return strings.TrimSuffix(b.String(), "\n")
}

func harmonyGet(schema any, key string) (any, bool) {
	m, ok := schema.(map[string]any)
	if !ok {
		return nil, false
	}
	v, ok := m[key]
	return v, ok
}

func harmonyString(schema any, key string) (string, bool) {
	v, _ := harmonyGet(schema, key)
	s, ok := v.(string)
	return s, ok
}

func harmonyIsEnum(schema any) bool {
	v, _ := harmonyGet(schema, "enum")
	arr, ok := v.([]any)
	return ok && len(arr) > 0
}

// harmonyDefaultText renders a default beside a property: a string is quoted
// unless the schema is an enum.
func harmonyDefaultText(d, schema any) string {
	if s, ok := d.(string); ok {
		if harmonyIsEnum(schema) {
			return "default: " + s
		}
		return `default: "` + s + `"`
	}
	return "default: " + harmonyJSON(d)
}

func harmonyNullable(typeText string, schema any) string {
	if v, _ := harmonyGet(schema, "nullable"); v == true && !strings.Contains(typeText, "null") {
		return typeText + " | null"
	}
	return typeText
}

// harmonyTypeScript ports json_schema_to_typescript in openai/harmony
// src/encoding.rs.
func harmonyTypeScript(schema any, indent string) string {
	if v, _ := harmonyGet(schema, "oneOf"); v != nil {
		if variants, ok := v.([]any); ok {
			var b strings.Builder
			for _, variant := range variants {
				b.WriteString("\n" + indent + " | ")
				b.WriteString(harmonyNullable(harmonyTypeScript(variant, indent+"   "), variant))
				var comments []string
				if d, ok := harmonyString(variant, "description"); ok {
					comments = append(comments, d)
				}
				if d, ok := harmonyGet(variant, "default"); ok {
					// Here the reference quotes a string default even for an enum.
					if s, isString := d.(string); isString && !harmonyIsEnum(variant) {
						comments = append(comments, `default: "`+s+`"`)
					} else {
						comments = append(comments, "default: "+harmonyJSON(d))
					}
				}
				if len(comments) > 0 {
					b.WriteString(" // " + strings.Join(comments, " "))
				}
			}
			return b.String()
		}
	}
	kind, _ := harmonyGet(schema, "type")
	switch k := kind.(type) {
	case []any:
		var names []string
		for _, t := range k {
			if s, ok := t.(string); ok {
				if s == "integer" {
					s = "number"
				}
				names = append(names, s)
			}
		}
		if len(names) > 0 {
			return strings.Join(names, " | ")
		}
		return "any"
	case string:
		switch k {
		case "object":
			return harmonyTypeScriptObject(schema.(map[string]any), indent)
		case "string":
			v, _ := harmonyGet(schema, "enum")
			if arr, ok := v.([]any); ok {
				var values []string
				for _, e := range arr {
					if s, ok := e.(string); ok {
						values = append(values, `"`+s+`"`)
					}
				}
				if len(values) > 0 {
					return strings.Join(values, " | ")
				}
			}
			return "string"
		case "number", "integer":
			return "number"
		case "boolean":
			return "boolean"
		case "array":
			if items, ok := harmonyGet(schema, "items"); ok {
				return harmonyTypeScript(items, indent) + "[]"
			}
			return "Array<any>"
		}
	}
	return "any"
}

// harmonyPropertyOrder lists required properties first, in "required"
// order, then the rest by name: a Go map keeps no schema order.
func harmonyPropertyOrder(props map[string]any, required []string) []string {
	order := make([]string, 0, len(props))
	seen := map[string]bool{}
	for _, r := range required {
		if _, ok := props[r]; ok && !seen[r] {
			order = append(order, r)
			seen[r] = true
		}
	}
	var rest []string
	for k := range props {
		if !seen[k] {
			rest = append(rest, k)
		}
	}
	sort.Strings(rest)
	return append(order, rest...)
}

func harmonyRequired(schema map[string]any) []string {
	var required []string
	switch r := schema["required"].(type) {
	case []any:
		for _, v := range r {
			if s, ok := v.(string); ok {
				required = append(required, s)
			}
		}
	case []string:
		required = r
	}
	return required
}

func harmonyTypeScriptObject(schema map[string]any, indent string) string {
	var b strings.Builder
	if d, ok := schema["description"].(string); ok {
		b.WriteString(indent + "// " + d + "\n")
	}
	b.WriteString("{\n")
	if props, ok := schema["properties"].(map[string]any); ok {
		required := harmonyRequired(schema)
		isRequired := map[string]bool{}
		for _, r := range required {
			isRequired[r] = true
		}
		for _, key := range harmonyPropertyOrder(props, required) {
			value := props[key]
			optional := "?"
			if isRequired[key] {
				optional = ""
			}
			if title, ok := harmonyString(value, "title"); ok {
				b.WriteString(indent + "// " + title + "\n" + indent + "//\n")
			}
			oneOf, hasOneOf := harmonyGet(value, "oneOf")
			if !hasOneOf {
				if d, ok := harmonyString(value, "description"); ok {
					b.WriteString(indent + "// " + d + "\n")
				}
			}
			if ex, _ := harmonyGet(value, "examples"); ex != nil {
				if arr, ok := ex.([]any); ok && len(arr) > 0 {
					b.WriteString(indent + "// Examples:\n")
					for _, e := range arr {
						if s, ok := e.(string); ok {
							b.WriteString(indent + `// - "` + s + "\"\n")
						}
					}
				}
			}
			if variants, ok := oneOf.([]any); ok {
				b.WriteString(harmonyOneOfProperty(key, optional, value.(map[string]any), variants, indent))
				continue
			}
			b.WriteString(indent + key + optional + ": ")
			b.WriteString(harmonyNullable(harmonyTypeScript(value, indent+"    "), value) + ",")
			if !hasOneOf {
				if d, ok := harmonyGet(value, "default"); ok {
					b.WriteString(" // " + harmonyDefaultText(d, value))
				}
			}
			b.WriteString("\n")
		}
	}
	b.WriteString(indent + "}")
	return b.String()
}

func harmonyOneOfProperty(key, optional string, value map[string]any, variants []any, indent string) string {
	var b strings.Builder
	propDescription, hasPropDescription := value["description"].(string)
	skip := false
	if hasPropDescription && len(variants) > 0 {
		if first, ok := harmonyString(variants[0], "description"); ok && first == propDescription {
			skip = true
		}
	}
	describedAbove := false
	if hasPropDescription && !skip {
		b.WriteString(indent + "// " + propDescription + "\n")
		describedAbove = true
	}
	if d, ok := value["default"]; ok {
		b.WriteString(indent + "// " + harmonyDefaultText(d, value) + "\n")
	}
	b.WriteString(indent + key + optional + ":\n")
	for i, variant := range variants {
		b.WriteString(indent + " | " + harmonyNullable(harmonyTypeScript(variant, indent+"   "), variant))
		var comments []string
		if i != 0 || !describedAbove {
			if d, ok := harmonyString(variant, "description"); ok && (!hasPropDescription || d != propDescription) {
				comments = append(comments, d)
			}
		}
		if d, ok := harmonyGet(variant, "default"); ok {
			comments = append(comments, harmonyDefaultText(d, variant))
		}
		if len(comments) > 0 {
			b.WriteString(" // " + strings.Join(comments, " "))
		}
		b.WriteString("\n")
	}
	b.WriteString(indent + ",\n")
	return b.String()
}
