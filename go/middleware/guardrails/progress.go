package guardrails

import (
	"errors"
	"strings"
)

// ProgressSnapshot describes application-verified workflow requirements.
// It never grants authorization or proves a downstream operation succeeded.
type ProgressSnapshot struct {
	Pending    []string
	Ready      bool
	NoProgress int
	Exhausted  bool
}

// ProgressTracker tracks observed facts for one workflow run. Callers must
// serialize access and validate observations outside the model.
type ProgressTracker struct {
	required      []string
	satisfied     map[string]bool
	maxNoProgress int
	failures      int
}

// NewProgressTracker constructs a tracker with distinct nonempty requirements.
func NewProgressTracker(required []string, maxNoProgress int) (*ProgressTracker, error) {
	if len(required) == 0 || maxNoProgress < 0 {
		return nil, errors.New("invalid progress configuration")
	}
	seen := make(map[string]bool, len(required))
	for _, key := range required {
		if strings.TrimSpace(key) == "" || seen[key] {
			return nil, errors.New("requirements must be distinct and nonempty")
		}
		seen[key] = true
	}
	return &ProgressTracker{required: append([]string(nil), required...), satisfied: map[string]bool{}, maxNoProgress: maxNoProgress}, nil
}

// Observe records or invalidates a verified fact. Only a new fact resets failures.
func (p *ProgressTracker) Observe(key string, present bool) (ProgressSnapshot, error) {
	known := false
	for _, candidate := range p.required {
		if candidate == key {
			known = true
			break
		}
	}
	if !known {
		return ProgressSnapshot{}, errors.New("unknown requirement")
	}
	if present && !p.satisfied[key] {
		p.satisfied[key] = true
		p.failures = 0
	} else if !present {
		delete(p.satisfied, key)
	}
	return p.Snapshot(), nil
}

// Failure records an unsuccessful attempt. A separate total budget is required.
func (p *ProgressTracker) Failure() ProgressSnapshot { p.failures++; return p.Snapshot() }

// Snapshot returns a copy of the pending requirements and retry status.
func (p *ProgressTracker) Snapshot() ProgressSnapshot {
	pending := make([]string, 0)
	for _, key := range p.required {
		if !p.satisfied[key] {
			pending = append(pending, key)
		}
	}
	return ProgressSnapshot{Pending: pending, Ready: len(pending) == 0, NoProgress: p.failures, Exhausted: p.failures > p.maxNoProgress}
}
