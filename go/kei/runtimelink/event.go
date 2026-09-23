package runtimelink

import (
	"encoding/json"
	"errors"
	"slices"
	"time"
)

// LifecycleEventName is the closed set of SDK lifecycle events (spec §5.3).
type LifecycleEventName string

const (
	EventStarted      LifecycleEventName = "runtime.link.started"
	EventConnected    LifecycleEventName = "runtime.link.connected"
	EventDegraded     LifecycleEventName = "runtime.link.degraded"
	EventReconnecting LifecycleEventName = "runtime.link.reconnecting"
	EventTerminal     LifecycleEventName = "runtime.link.terminal"
	EventStopped      LifecycleEventName = "runtime.link.stopped"
)

// LifecycleEventNames lists every valid LifecycleEventName in contract order.
var LifecycleEventNames = []LifecycleEventName{
	EventStarted, EventConnected, EventDegraded, EventReconnecting, EventTerminal, EventStopped,
}

// Valid reports whether n is in the closed set.
func (n LifecycleEventName) Valid() bool { return slices.Contains(LifecycleEventNames, n) }

// SDKLangs is the closed set of EventHarness.SDKLang values.
var SDKLangs = []string{"go", "python", "typescript"}

// ErrInvalidEvent is returned when a LinkEvent has a value outside its
// closed set or bounds.
var ErrInvalidEvent = errors.New("runtimelink: invalid lifecycle event")

// eventTimeLayout is the one wire format for LinkEvent.At in every SDK:
// UTC, millisecond precision, "Z" suffix.
const eventTimeLayout = "2006-01-02T15:04:05.000Z"

// EventHarness is the declared envelope carried on every lifecycle event.
type EventHarness struct {
	Kind          HarnessKind `json:"kind"`
	Version       string      `json:"version"`
	DeploymentEnv string      `json:"deployment_env"`
	SDKLang       string      `json:"sdk_lang"`
	SDKVersion    string      `json:"sdk_version"`
}

// LinkEvent is a redacted lifecycle record. It is redacted by construction:
// the type has no field that could carry a token, argv, env, child stderr,
// provider payload or result, reasoning, customer content, or a subject.
// Decoding drops every key outside this allowlist.
type LinkEvent struct {
	Name             LifecycleEventName `json:"name"`
	At               time.Time          `json:"-"`
	State            LinkState          `json:"state"`
	Reason           FailureClass       `json:"reason"`
	RunID            string             `json:"run_id"`
	Seq              int64              `json:"seq"`
	SDKInstanceID    string             `json:"sdk_instance_id"`
	Harness          EventHarness       `json:"harness"`
	Restarts         int                `json:"restarts"`
	ConsecutiveFails int                `json:"consecutive_fails"`
}

// wireLinkEvent fixes the serialized key order and the time format.
type wireLinkEvent struct {
	Name             LifecycleEventName `json:"name"`
	At               string             `json:"at"`
	State            LinkState          `json:"state"`
	Reason           FailureClass       `json:"reason"`
	RunID            string             `json:"run_id"`
	Seq              int64              `json:"seq"`
	SDKInstanceID    string             `json:"sdk_instance_id"`
	Harness          EventHarness       `json:"harness"`
	Restarts         int                `json:"restarts"`
	ConsecutiveFails int                `json:"consecutive_fails"`
}

// Validate checks every closed set and bound on the event.
func (e LinkEvent) Validate() error {
	h := e.Harness
	switch {
	case !e.Name.Valid(), !e.State.Valid(), e.At.IsZero():
		return ErrInvalidEvent
	case e.Reason != "" && !e.Reason.Valid():
		return ErrInvalidEvent
	case e.RunID != "" && !idPattern.MatchString(e.RunID):
		return ErrInvalidEvent
	case !idPattern.MatchString(e.SDKInstanceID):
		return ErrInvalidEvent
	case !h.Kind.Valid(), !slices.Contains(SDKLangs, h.SDKLang):
		return ErrInvalidEvent
	case h.Version != "" && !versionPattern.MatchString(h.Version):
		return ErrInvalidEvent
	case h.DeploymentEnv != "" && !envNamePattern.MatchString(h.DeploymentEnv):
		return ErrInvalidEvent
	case h.SDKVersion != "" && !versionPattern.MatchString(h.SDKVersion):
		return ErrInvalidEvent
	case e.Seq < 0 || e.Seq > maxSeq || e.Restarts < 0 || e.ConsecutiveFails < 0:
		return ErrInvalidEvent
	}
	return nil
}

// MarshalJSON validates the event and emits exactly the allowlisted keys.
func (e LinkEvent) MarshalJSON() ([]byte, error) {
	if err := e.Validate(); err != nil {
		return nil, err
	}
	return json.Marshal(wireLinkEvent{
		Name: e.Name, At: e.At.UTC().Format(eventTimeLayout), State: e.State, Reason: e.Reason,
		RunID: e.RunID, Seq: e.Seq, SDKInstanceID: e.SDKInstanceID, Harness: e.Harness,
		Restarts: e.Restarts, ConsecutiveFails: e.ConsecutiveFails,
	})
}

// UnmarshalJSON keeps only allowlisted keys and validates the result.
func (e *LinkEvent) UnmarshalJSON(data []byte) error {
	var w wireLinkEvent
	if err := json.Unmarshal(data, &w); err != nil {
		return errors.Join(ErrInvalidEvent, err)
	}
	if !timestampPattern.MatchString(w.At) {
		return ErrInvalidEvent
	}
	at, err := time.Parse(time.RFC3339Nano, w.At)
	if err != nil {
		return errors.Join(ErrInvalidEvent, err)
	}
	ev := LinkEvent{
		Name: w.Name, At: at, State: w.State, Reason: w.Reason, RunID: w.RunID, Seq: w.Seq,
		SDKInstanceID: w.SDKInstanceID, Harness: w.Harness, Restarts: w.Restarts,
		ConsecutiveFails: w.ConsecutiveFails,
	}
	if err := ev.Validate(); err != nil {
		return err
	}
	*e = ev
	return nil
}
