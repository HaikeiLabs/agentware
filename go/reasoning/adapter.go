package reasoning

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"regexp"
	"strings"
)

// Sentinel errors returned by the reasoning adapter. Every one represents a
// fail-closed outcome: malformed or unbounded input never produces a partial
// tree or partially-stripped content.
var (
	// ErrMalformedInput is returned when the raw input has no usable shape.
	ErrMalformedInput = errors.New("reasoning: malformed input")
	// ErrMalformedJSON is returned when input that looks like JSON cannot be
	// decoded.
	ErrMalformedJSON = errors.New("reasoning: malformed JSON")
	// ErrMalformedReasoningField is returned when a reasoning field is not a
	// string (e.g. reasoning_content is an object or array).
	ErrMalformedReasoningField = errors.New("reasoning: reasoning field is not a string")
	// ErrMalformedContent is returned when a structured content field is
	// neither a string nor null.
	ErrMalformedContent = errors.New("reasoning: content field is not a string")
	// ErrUnbalancedThinking is returned when a thinking tag is opened but
	// never closed, so the rest of the output cannot be trusted.
	ErrUnbalancedThinking = errors.New("reasoning: unbalanced thinking tag")
	// ErrReasoningTooLarge is returned when the extracted reasoning exceeds
	// the configured byte cap (unbounded input).
	ErrReasoningTooLarge = errors.New("reasoning: reasoning exceeds maximum size")
	// ErrTooManyNodes is returned when the reasoning would produce more nodes
	// than configured.
	ErrTooManyNodes = errors.New("reasoning: too many reasoning nodes")
	// ErrTooDeep is returned when the reasoning would produce a deeper chain
	// than configured.
	ErrTooDeep = errors.New("reasoning: reasoning tree too deep")
)

// analysis is the shared result of inspecting a raw model output: the
// extracted reasoning text, the clean content left after stripping, and the
// input format that produced them.
type analysis struct {
	reasoning string
	clean     string
	format    Format
}

// Adapter is a model-agnostic reasoning adapter. It is safe for concurrent
// use after construction; model field registration is read-only afterwards.
type Adapter struct {
	opts       Options
	modelField map[string]string
}

// knownReasoningFields are the field names recognized as reasoning on a
// structured response, in priority order. reasoning_content is the de-facto
// native field (DeepSeek, Qwen/QwQ, Kimi, GLM); the others cover common
// model-specific names.
var knownReasoningFields = []string{
	"reasoning_content",
	"thinking",
	"thinking_content",
	"reasoning",
}

// recognizedKeys are the keys on a structured response that make it worth
// entering the structured path. A mapping without any of these is treated as
// opaque text and returned unchanged.
var recognizedKeys = map[string]bool{
	"message": true, "content": true,
	"reasoning_content": true, "thinking": true,
	"thinking_content": true, "reasoning": true,
}

// New returns a reasoning Adapter with the given options (defaults when none
// are supplied).
func New(opts ...Options) *Adapter {
	o := DefaultOptions()
	if len(opts) > 0 {
		o = opts[0].normalize()
	} else {
		o = o.normalize()
	}
	return &Adapter{
		opts:       o,
		modelField: make(map[string]string),
	}
}

// RegisterModelField maps a model name to a model-specific reasoning field
// name. Fields registered here are consulted before the known field set, so
// callers can teach the adapter names like Spark's thinking payload field.
func (a *Adapter) RegisterModelField(model, field string) {
	if model != "" && field != "" {
		a.modelField[strings.ToLower(model)] = field
	}
}

// Options returns a copy of the adapter's effective options.
func (a *Adapter) Options() Options {
	return a.opts
}

// Strip removes reasoning from raw and returns the clean content that
// ordinary tool-call parsers should see. It fails closed: malformed or
// unbounded input returns an error instead of partially-stripped output.
func (a *Adapter) Strip(raw any) (string, error) {
	an, err := a.analyze(raw, "")
	if err != nil {
		return "", err
	}
	return an.clean, nil
}

// Extract normalizes reasoning found in raw into a versioned context tree.
// Raw reasoning is never stored on the tree: nodes carry bounded summaries.
// The tree has no nodes (RootID "") when raw contains no reasoning.
func (a *Adapter) Extract(raw any, model, backend string) (*ContextTree, error) {
	an, err := a.analyze(raw, model)
	if err != nil {
		return nil, err
	}

	now := a.opts.Clock().UTC()
	tree := &ContextTree{
		Version:   ContractVersion,
		Format:    an.format,
		Model:     model,
		Backend:   backend,
		CreatedAt: now,
		Nodes:     []Node{},
	}

	if strings.TrimSpace(an.reasoning) == "" {
		return tree, nil
	}

	nodes, err := a.parseNodes(an.reasoning, now)
	if err != nil {
		return nil, err
	}
	tree.Nodes = nodes
	if len(nodes) > 0 {
		tree.RootID = nodes[0].ID
	}
	return tree, nil
}

// Parse is Strip and Extract in one call: it returns the clean content and
// the normalized context tree for raw.
func (a *Adapter) Parse(raw any, model, backend string) (string, *ContextTree, error) {
	an, err := a.analyze(raw, model)
	if err != nil {
		return "", nil, err
	}
	tree, err := a.Extract(raw, model, backend)
	if err != nil {
		return "", nil, err
	}
	return an.clean, tree, nil
}

// analyze inspects raw and splits it into reasoning text and clean content.
func (a *Adapter) analyze(raw any, model string) (*analysis, error) {
	switch v := raw.(type) {
	case string:
		return a.analyzeString(v, model)
	case []byte:
		return a.analyzeBytes(v, model)
	case map[string]any:
		return a.analyzeStructured(v, model)
	default:
		return nil, fmt.Errorf("%w: unsupported raw type %T", ErrMalformedInput, raw)
	}
}

func (a *Adapter) analyzeString(s, model string) (*analysis, error) {
	trimmed := strings.TrimSpace(s)
	if strings.HasPrefix(trimmed, "{") {
		var m map[string]any
		err := json.Unmarshal([]byte(trimmed), &m)
		if err == nil && hasRecognizedKey(m) {
			return a.analyzeStructured(m, model)
		}
		if err != nil {
			return nil, fmt.Errorf("%w: %v", ErrMalformedJSON, err)
		}
	}
	return a.analyzeText(s)
}

func (a *Adapter) analyzeBytes(b []byte, model string) (*analysis, error) {
	trimmed := bytes.TrimSpace(b)
	if len(trimmed) > 0 && (trimmed[0] == '{' || trimmed[0] == '[') {
		var v any
		if err := json.Unmarshal(trimmed, &v); err != nil {
			return nil, fmt.Errorf("%w: %v", ErrMalformedJSON, err)
		}
		if m, ok := v.(map[string]any); ok && hasRecognizedKey(m) {
			return a.analyzeStructured(m, model)
		}
	}
	return a.analyzeText(string(b))
}

// analyzeStructured inspects a decoded JSON object. It descends into a
// "message" key (OpenAI-style, including the choices[0].message nesting of a
// full OpenAI-compatible response) and looks for reasoning fields, then for
// the clean content. Content may be a string, null, or (Anthropic Messages
// API) a list of typed parts.
func (a *Adapter) analyzeStructured(m map[string]any, model string) (*analysis, error) {
	if msg, ok := m["message"].(map[string]any); ok {
		return a.analyzeStructured(msg, model)
	}
	if msg := firstChoiceMessage(m); msg != nil {
		return a.analyzeStructured(msg, model)
	}

	// Content first: a string, null, or a list of typed parts. Fail closed on
	// any other shape.
	var content string
	var partReasoning string
	if c, ok := m["content"]; ok {
		switch v := c.(type) {
		case nil:
			content = ""
		case string:
			content = v
		case []any:
			var err error
			content, partReasoning, err = extractContentParts(v)
			if err != nil {
				return nil, err
			}
		default:
			return nil, fmt.Errorf("%w: got %T", ErrMalformedContent, c)
		}
	}

	reasoning, format, err := a.extractReasoningField(m, model)
	if err != nil {
		return nil, err
	}
	// A native reasoning field takes priority; otherwise use the reasoning
	// carried by Anthropic-style thinking parts.
	if reasoning == "" && partReasoning != "" {
		reasoning, format = partReasoning, FormatNative
	}

	// A native reasoning field means the content is already clean, but
	// thinking tags may still be embedded in it.
	if reasoning != "" {
		clean, rerr := stripThinkingTags(content)
		if rerr != nil {
			return nil, rerr
		}
		return &analysis{reasoning: reasoning, clean: clean, format: format}, nil
	}

	// No native reasoning field: fall back to thinking tags in the content.
	clean, tags, rerr := splitThinkingTags(content)
	if rerr != nil {
		return nil, rerr
	}
	if tags != "" {
		return &analysis{reasoning: tags, clean: clean, format: FormatThinking}, nil
	}
	return &analysis{reasoning: "", clean: content, format: FormatNone}, nil
}

// firstChoiceMessage returns the message object of the first choice of a
// full OpenAI-compatible response ({choices: [{message: {...}}]}), or nil
// when m is not shaped that way.
func firstChoiceMessage(m map[string]any) map[string]any {
	choices, ok := m["choices"].([]any)
	if !ok || len(choices) == 0 {
		return nil
	}
	choice, ok := choices[0].(map[string]any)
	if !ok {
		return nil
	}
	msg, ok := choice["message"].(map[string]any)
	if !ok {
		return nil
	}
	return msg
}

// extractContentParts splits an Anthropic Messages API content list into the
// clean text (joined "text" parts) and the reasoning (joined "thinking"
// parts). Other part types (tool_use, tool_result, ...) are ignored: they
// carry neither clean text nor reasoning.
func extractContentParts(parts []any) (content, reasoning string, err error) {
	var textParts, thinkingParts []string
	for _, p := range parts {
		part, ok := p.(map[string]any)
		if !ok {
			return "", "", fmt.Errorf("%w: content part is not an object", ErrMalformedContent)
		}
		switch part["type"] {
		case "text":
			text, ok := part["text"].(string)
			if !ok {
				return "", "", fmt.Errorf("%w: text part has no string text", ErrMalformedContent)
			}
			textParts = append(textParts, text)
		case "thinking":
			thinking, ok := part["thinking"].(string)
			if !ok {
				return "", "", fmt.Errorf("%w: thinking part has no string thinking", ErrMalformedReasoningField)
			}
			thinkingParts = append(thinkingParts, thinking)
		}
	}
	return strings.Join(textParts, "\n"), strings.Join(thinkingParts, "\n"), nil
}

// extractReasoningField pulls the reasoning text out of a structured object,
// consulting the registered model-specific field first, then the known field
// set in priority order. It returns the detected format.
func (a *Adapter) extractReasoningField(m map[string]any, model string) (string, Format, error) {
	if model != "" {
		if field := a.modelField[strings.ToLower(model)]; field != "" {
			if v, ok := m[field]; ok && v != nil {
				s, ok := v.(string)
				if !ok {
					return "", "", fmt.Errorf("%w: field %q is %T", ErrMalformedReasoningField, field, v)
				}
				if s != "" {
					return s, FormatModelSpecific, nil
				}
			}
		}
	}
	for _, field := range knownReasoningFields {
		v, ok := m[field]
		if !ok || v == nil {
			continue
		}
		s, ok := v.(string)
		if !ok {
			return "", "", fmt.Errorf("%w: field %q is %T", ErrMalformedReasoningField, field, v)
		}
		if s != "" {
			return s, FormatNative, nil
		}
	}
	return "", FormatNone, nil
}

// analyzeText inspects plain text for generic thinking tags.
func (a *Adapter) analyzeText(s string) (*analysis, error) {
	clean, tags, err := splitThinkingTags(s)
	if err != nil {
		return nil, err
	}
	if tags != "" {
		return &analysis{reasoning: tags, clean: clean, format: FormatThinking}, nil
	}
	return &analysis{reasoning: "", clean: s, format: FormatNone}, nil
}

// hasRecognizedKey reports whether a structured object carries any key that
// makes it a reasoning-shaped response.
func hasRecognizedKey(m map[string]any) bool {
	for k := range m {
		if recognizedKeys[k] {
			return true
		}
	}
	return false
}

// thinkingOpen matches the generic thinking open tags, case-insensitively.
var thinkingOpen = regexp.MustCompile(`(?i)<thinking>|\[THINK\]`)

// thinkingClose matches the generic thinking close tags, case-insensitively.
var thinkingClose = regexp.MustCompile(`(?i)</thinking>|\[/THINK\]`)

// splitThinkingTags extracts embedded thinking blocks from text and returns
// the clean text and the joined reasoning. It fails closed on unbalanced
// tags: an open tag without a matching close is an error, not partial output.
func splitThinkingTags(s string) (clean, reasoning string, err error) {
	if !thinkingOpen.MatchString(s) && !thinkingClose.MatchString(s) {
		return s, "", nil
	}

	var b strings.Builder
	var reason strings.Builder
	rest := s
	for {
		loc := thinkingOpen.FindStringIndex(rest)
		if loc == nil {
			// No more open tags; any remaining close tag is unbalanced.
			if thinkingClose.MatchString(rest) {
				return "", "", ErrUnbalancedThinking
			}
			b.WriteString(rest)
			break
		}
		b.WriteString(rest[:loc[0]])
		inner := rest[loc[1]:]
		closeLoc := thinkingClose.FindStringIndex(inner)
		if closeLoc == nil {
			return "", "", ErrUnbalancedThinking
		}
		body := inner[:closeLoc[0]]
		if reason.Len() > 0 {
			reason.WriteByte('\n')
		}
		reason.WriteString(body)
		rest = inner[closeLoc[1]:]
	}
	return b.String(), strings.TrimSpace(reason.String()), nil
}

// stripThinkingTags removes thinking blocks and returns the remaining text.
func stripThinkingTags(s string) (string, error) {
	clean, _, err := splitThinkingTags(s)
	return clean, err
}

// nodeID derives a stable, deterministic node ID from a node's content and
// position so identical reasoning produces identical trees across ports and
// runs.
func nodeID(kind NodeKind, parentID, summary string) string {
	h := sha256.Sum256([]byte(string(kind) + "\x00" + parentID + "\x00" + summary))
	return hex.EncodeToString(h[:])[:16]
}
