package guardrails

import (
	"encoding/json"
	"os"
	"reflect"
	"testing"
)

func TestProgressSharedFixture(t *testing.T) {
	raw, err := os.ReadFile("../../../fixtures/workflow/progress.v1.json")
	if err != nil {
		t.Fatal(err)
	}
	var fixture struct {
		Required []string `json:"required"`
		Max      int      `json:"max_no_progress"`
		Events   []struct {
			Kind       string   `json:"kind"`
			Key        string   `json:"key"`
			Present    bool     `json:"present"`
			Pending    []string `json:"pending"`
			Ready      bool     `json:"ready"`
			NoProgress int      `json:"no_progress"`
			Exhausted  bool     `json:"exhausted"`
		} `json:"events"`
	}
	if err := json.Unmarshal(raw, &fixture); err != nil {
		t.Fatal(err)
	}
	tracker, err := NewProgressTracker(fixture.Required, fixture.Max)
	if err != nil {
		t.Fatal(err)
	}
	for _, event := range fixture.Events {
		var got ProgressSnapshot
		if event.Kind == "failure" {
			got = tracker.Failure()
		} else {
			got, err = tracker.Observe(event.Key, event.Present)
			if err != nil {
				t.Fatal(err)
			}
		}
		if !reflect.DeepEqual(got.Pending, event.Pending) || got.Ready != event.Ready || got.NoProgress != event.NoProgress || got.Exhausted != event.Exhausted {
			t.Fatalf("%+v: %+v", event, got)
		}
	}
}

func TestProgressRejectsInvalidInputs(t *testing.T) {
	if _, err := NewProgressTracker([]string{"a", "a"}, 2); err == nil {
		t.Fatal("expected duplicate error")
	}
	if _, err := NewProgressTracker(nil, 2); err == nil {
		t.Fatal("expected empty error")
	}
	tracker, err := NewProgressTracker([]string{"a"}, 2)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := tracker.Observe("b", true); err == nil {
		t.Fatal("expected unknown error")
	}
}
