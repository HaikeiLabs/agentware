package runtimelink

import (
	"bytes"
	"encoding/json"
	"errors"
	"regexp"
	"strconv"
)

// Child line parse errors. Every other outcome is either an event or an
// ignored unknown event.
var (
	// ErrLineTooLong: the line exceeds MaxChildLineBytes; drop and count it.
	ErrLineTooLong = errors.New("runtimelink: child line too long")
	// ErrMalformedLine: not a JSON object, or a known event with a missing,
	// mistyped, or out-of-bounds field.
	ErrMalformedLine = errors.New("runtimelink: malformed child line")
	// ErrContractMismatch: unknown "v" or an outcome outside the closed set.
	ErrContractMismatch = errors.New("runtimelink: child contract mismatch")
)

// ChildEventKind discriminates ChildEvent.
type ChildEventKind string

const (
	ChildIdentity ChildEventKind = "identity"
	ChildBeat     ChildEventKind = "beat"
	ChildTerminal ChildEventKind = "terminal"
	// ChildIgnored is a well-formed v1 line with an event name this SDK does
	// not know; forward-compatible runtimes may add events.
	ChildIgnored ChildEventKind = "ignored"
)

// BeatEvent is one runtime→catalog heartbeat result as the runtime reports it.
type BeatEvent struct {
	RunID      string      `json:"run_id"`
	Seq        int64       `json:"seq"` // 0 when absent: the runtime omits seq on beats sent before identity
	At         string      `json:"at"`
	Outcome    BeatOutcome `json:"outcome"`
	HTTPStatus int         `json:"http_status"`
	LatencyMS  int64       `json:"latency_ms"`
	NextInMS   int64       `json:"next_in_ms"`
}

// TerminalEvent reports that the runtime is about to exit for good.
type TerminalEvent struct {
	RunID  string `json:"run_id"`
	Reason string `json:"reason"`
}

// ChildEvent is one parsed child stdout line. Exactly one of Identity, Beat,
// or Terminal is set, matching Kind; none is set for ChildIgnored. Only the
// allowlisted fields survive parsing: payloads, results, reasoning, tokens,
// or any other key the runtime might emit are dropped.
type ChildEvent struct {
	Kind     ChildEventKind
	Identity *RuntimeIdentity
	Beat     *BeatEvent
	Terminal *TerminalEvent
}

// Bounds on child event fields. Identifiers are opaque tokens, never text, so
// free-form content cannot ride along in an identity or reason field.
const (
	maxSeq        = 1<<53 - 1 // exact in every SDK language
	maxLatencyMS  = 3_600_000
	maxNextInMS   = 3_600_000
	minHTTPStatus = 100
	maxHTTPStatus = 599
)

var (
	idPattern        = regexp.MustCompile(`^[A-Za-z0-9._:-]{1,128}$`)
	reasonPattern    = regexp.MustCompile(`^[a-z0-9_]{1,64}$`)
	timestampPattern = regexp.MustCompile(`^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,9})?(Z|[+-]\d{2}:\d{2})$`)
)

// ParseChildLine parses one line of child stdout (a trailing "\n" or "\r\n"
// is ignored). It returns ErrLineTooLong, ErrContractMismatch, or
// ErrMalformedLine for lines the SDK must drop and count.
func ParseChildLine(line []byte) (ChildEvent, error) {
	line = bytes.TrimSuffix(bytes.TrimSuffix(line, []byte("\n")), []byte("\r"))
	if len(line) > MaxChildLineBytes {
		return ChildEvent{}, ErrLineTooLong
	}
	var raw map[string]json.RawMessage
	if err := json.Unmarshal(line, &raw); err != nil || raw == nil {
		return ChildEvent{}, ErrMalformedLine
	}
	if v, ok := raw["v"]; !ok || string(v) != strconv.Itoa(ContractVersion) {
		return ChildEvent{}, ErrContractMismatch
	}
	var event string
	if err := json.Unmarshal(raw["event"], &event); err != nil {
		return ChildEvent{}, ErrMalformedLine
	}
	f := fields(raw)
	switch ChildEventKind(event) {
	case ChildIdentity:
		id := RuntimeIdentity{
			RunID:          f.id("run_id", true),
			InstallationID: f.id("installation_id", true),
			OrgID:          f.id("org_id", true),
			WorkspaceID:    f.id("workspace_id", false),
			Platform:       f.id("platform", false),
			Status:         f.id("status", false),
			BindingStatus:  f.id("binding_status", false),
			RuntimeVersion: f.id("runtime_version", false),
		}
		if f.err != nil {
			return ChildEvent{}, f.err
		}
		return ChildEvent{Kind: ChildIdentity, Identity: &id}, nil
	case ChildBeat:
		b := BeatEvent{
			RunID:     f.id("run_id", true),
			Seq:       f.integer("seq", false, 0, maxSeq),
			At:        f.timestamp("at"),
			Outcome:   BeatOutcome(f.str("outcome", true)),
			LatencyMS: f.integer("latency_ms", false, 0, maxLatencyMS),
			NextInMS:  f.integer("next_in_ms", false, 0, maxNextInMS),
		}
		if status := f.integer("http_status", false, 0, maxHTTPStatus); status != 0 {
			if status < minHTTPStatus {
				f.fail(ErrMalformedLine)
			}
			b.HTTPStatus = int(status)
		}
		if f.err == nil && !b.Outcome.Valid() {
			f.fail(ErrContractMismatch)
		}
		if f.err != nil {
			return ChildEvent{}, f.err
		}
		return ChildEvent{Kind: ChildBeat, Beat: &b}, nil
	case ChildTerminal:
		t := TerminalEvent{RunID: f.id("run_id", true), Reason: f.str("reason", true)}
		if f.err == nil && !reasonPattern.MatchString(t.Reason) {
			f.fail(ErrMalformedLine)
		}
		if f.err != nil {
			return ChildEvent{}, f.err
		}
		return ChildEvent{Kind: ChildTerminal, Terminal: &t}, nil
	default:
		return ChildEvent{Kind: ChildIgnored}, nil
	}
}

// fieldReader extracts typed, bounded fields and records the first failure.
type fieldReader struct {
	raw map[string]json.RawMessage
	err error
}

func fields(raw map[string]json.RawMessage) *fieldReader { return &fieldReader{raw: raw} }

func (f *fieldReader) fail(err error) {
	if f.err == nil {
		f.err = err
	}
}

func (f *fieldReader) str(key string, required bool) string {
	v, ok := f.raw[key]
	if !ok {
		if required {
			f.fail(ErrMalformedLine)
		}
		return ""
	}
	var s string
	if err := json.Unmarshal(v, &s); err != nil {
		f.fail(ErrMalformedLine)
		return ""
	}
	return s
}

func (f *fieldReader) id(key string, required bool) string {
	s := f.str(key, required)
	if s == "" && !required {
		return ""
	}
	if !idPattern.MatchString(s) {
		f.fail(ErrMalformedLine)
		return ""
	}
	return s
}

func (f *fieldReader) timestamp(key string) string {
	s := f.str(key, true)
	if !timestampPattern.MatchString(s) {
		f.fail(ErrMalformedLine)
		return ""
	}
	return s
}

// integer accepts only JSON integers written without a fraction or exponent,
// so every SDK language agrees on which lines are valid.
func (f *fieldReader) integer(key string, required bool, lo, hi int64) int64 {
	v, ok := f.raw[key]
	if !ok {
		if required {
			f.fail(ErrMalformedLine)
		}
		return 0
	}
	n, err := strconv.ParseInt(string(v), 10, 64)
	if err != nil || n < lo || n > hi {
		f.fail(ErrMalformedLine)
		return 0
	}
	return n
}
