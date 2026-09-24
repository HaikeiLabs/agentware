package runtimelink

import (
	"bufio"
	"bytes"
	"context"
	"errors"
	"io"
	"os"
	"strings"
	"sync/atomic"
	"testing"
	"time"
)

func TestNewDisabled(t *testing.T) {
	cfg := DefaultConfig()
	cfg.Enabled = false
	rl, err := New(cfg)
	if err != nil {
		t.Fatalf("New disabled: %v", err)
	}
	if st := rl.Status(); st.State != StateDisabled {
		t.Fatalf("state: got %q, want %q", st.State, StateDisabled)
	}
	if _, ok := rl.Identity(); ok {
		t.Fatal("disabled link should not have identity")
	}
	if err := rl.Start(context.Background()); err != nil {
		t.Fatalf("Start disabled: %v", err)
	}
	if err := rl.Stop(context.Background()); err != nil {
		t.Fatalf("Stop disabled: %v", err)
	}
}

func TestNewProbeFallback(t *testing.T) {
	cfg := DefaultConfig()
	cfg.Enabled = true
	cfg.Binary.Path = "/nonexistent/binary"
	cfg.Harness.Kind = HarnessCLI
	cfg.ControlPlaneURL = "http://localhost:8080"
	_, err := New(cfg)
	if err != nil {
		t.Fatalf("New with missing binary: want soft fallback to legacy, got %v", err)
	}
}

func TestNewConfigError(t *testing.T) {
	cfg := DefaultConfig()
	cfg.Enabled = true
	cfg.Binary.Path = ""
	cfg.Harness.Kind = HarnessCLI
	cfg.ControlPlaneURL = "http://localhost:8080"
	_, err := New(cfg)
	if err == nil {
		t.Fatal("New with empty binary path: want error")
	}
	if !errors.Is(err, ErrConfigBinary) {
		t.Fatalf("New empty binary: want ErrConfigBinary, got %v", err)
	}
}

func TestNewWithMetricsSink(t *testing.T) {
	cfg := DefaultConfig()
	cfg.Enabled = false

	var gotMetrics atomic.Int32
	rl, err := New(cfg, WithMetricsSink(metricsFunc(func(ctx context.Context, state LinkState, reason FailureClass) {
		gotMetrics.Add(1)
	})))
	if err != nil {
		t.Fatalf("New: %v", err)
	}
	// disabled link should not emit metrics
	if gotMetrics.Load() != 0 {
		t.Fatal("disabled link emitted metrics")
	}
	if err := rl.Start(context.Background()); err != nil {
		t.Fatalf("Start: %v", err)
	}
	if gotMetrics.Load() != 0 {
		t.Fatal("disabled link emitted metrics after Start")
	}
}

func TestStateMachineIdentityDuringStart(t *testing.T) {
	l := testLink()
	defer func() { _ = l.Stop(context.Background()) }()

	l.transition(StateStarting, "")

	id := &RuntimeIdentity{
		RunID:          "run-1",
		InstallationID: "inst-1",
		OrgID:          "org-1",
		WorkspaceID:    "ws-1",
	}
	l.handleIdentity(id)

	st := l.Status()
	if st.State != StateConnected {
		t.Fatalf("after identity during start: state=%q, want %q", st.State, StateConnected)
	}
	gotID, ok := l.Identity()
	if !ok {
		t.Fatal("Identity() returned false after handleIdentity")
	}
	if gotID.RunID != "run-1" {
		t.Fatalf("Identity().RunID: got %q, want %q", gotID.RunID, "run-1")
	}
}

func TestStateMachineIdentityAfterDegraded(t *testing.T) {
	l := testLink()
	l.transition(StateConnected, "")
	l.handleBeat(&BeatEvent{Outcome: OutcomeCatalogUnreachable})
	st := l.Status()
	if st.State != StateDegraded {
		t.Fatalf("after bad beat: state=%q, want %q", st.State, StateDegraded)
	}

	id := &RuntimeIdentity{RunID: "run-2"}
	l.handleIdentity(id)
	st = l.Status()
	if st.State != StateDegraded {
		t.Fatalf("identity while degraded stays degraded: state=%q, want %q", st.State, StateDegraded)
	}
}

func TestStateMachineOKBeatFromDegraded(t *testing.T) {
	l := testLink()
	l.transition(StateDegraded, FailureCatalogUnreachable)

	l.handleBeat(&BeatEvent{Outcome: OutcomeOK})
	st := l.Status()
	if st.State != StateConnected {
		t.Fatalf("after ok beat from degraded: state=%q, want %q", st.State, StateConnected)
	}
	if st.Reason != "" {
		t.Fatalf("after recovery: reason=%q, want empty", st.Reason)
	}
}

func TestStateMachineOKBeatFromConnected(t *testing.T) {
	l := testLink()
	l.transition(StateConnected, "")
	l.handleIdentity(&RuntimeIdentity{RunID: "run-1"})

	l.handleBeat(&BeatEvent{Outcome: OutcomeOK})
	st := l.Status()
	if st.State != StateConnected {
		t.Fatalf("after ok beat from connected: state=%q, want %q", st.State, StateConnected)
	}
}

func TestStateMachineUnauthorizedBeat(t *testing.T) {
	l := testLink()
	l.transition(StateConnected, "")
	l.handleIdentity(&RuntimeIdentity{RunID: "run-1"})

	l.handleBeat(&BeatEvent{Outcome: OutcomeUnauthorized})
	st := l.Status()
	if st.State != StateTerminal {
		t.Fatalf("after unauthorized beat: state=%q, want %q", st.State, StateTerminal)
	}
	if st.Reason != FailureInstallationUnauthorized {
		t.Fatalf("after unauthorized: reason=%q, want %q", st.Reason, FailureInstallationUnauthorized)
	}
}

func TestStateMachineChildTerminal(t *testing.T) {
	l := testLink()
	l.transition(StateConnected, "")

	err := l.handleChildTerminal(&TerminalEvent{Reason: "unauthorized"})
	if !errors.Is(err, errTerminal) {
		t.Fatalf("handleChildTerminal: want errTerminal, got %v", err)
	}
	st := l.Status()
	if st.State != StateTerminal {
		t.Fatalf("after terminal: state=%q, want %q", st.State, StateTerminal)
	}
	if st.Reason != FailureInstallationUnauthorized {
		t.Fatalf("after terminal: reason=%q, want %q", st.Reason, FailureInstallationUnauthorized)
	}
}

func TestExitCodeMappingTerminal(t *testing.T) {
	tests := []struct {
		exitCode int
		reason   FailureClass
	}{
		{2, FailureContractMismatch},
		{3, FailureInstallationUnauthorized},
		{4, FailureConfigInvalid},
		{5, FailureConfigInvalid},
	}

	for _, tc := range tests {
		t.Run(string(tc.reason), func(t *testing.T) {
			l := testLink()
			l.transition(StateConnected, "")

			err := l.handleChildExit(tc.exitCode, errors.New("exit error"))
			if !errors.Is(err, errTerminal) {
				t.Fatalf("exit %d: want errTerminal, got %v", tc.exitCode, err)
			}
			st := l.Status()
			if st.State != StateTerminal {
				t.Fatalf("exit %d: state=%q, want %q", tc.exitCode, st.State, StateTerminal)
			}
			if st.Reason != tc.reason {
				t.Fatalf("exit %d: reason=%q, want %q", tc.exitCode, st.Reason, tc.reason)
			}
		})
	}
}

func TestExitCodeMappingTransient(t *testing.T) {
	l := testLink()
	l.transition(StateConnected, "")

	err := l.handleChildExit(1, errors.New("exit error"))
	if errors.Is(err, errTerminal) {
		t.Fatalf("exit 1: got errTerminal, want nil (transient)")
	}
	st := l.Status()
	if st.State != StateConnected {
		t.Fatalf("exit 1: state=%q, want %q (unchanged on transient)", st.State, StateConnected)
	}
	if l.restarts != 1 {
		t.Fatalf("exit 1: restarts=%d, want 1", l.restarts)
	}
}

func TestExitCodeZeroIsClean(t *testing.T) {
	l := testLink()
	l.transition(StateConnected, "")

	err := l.handleChildExit(0, nil)
	if err != nil {
		t.Fatalf("exit 0: want nil, got %v", err)
	}
}

func TestBuildCommandArgs(t *testing.T) {
	l := testLink()
	l.mode = ProbeJSONL
	cmd, err := l.buildCommand()
	if err != nil {
		t.Fatalf("buildCommand: %v", err)
	}
	if len(cmd.Args) < 3 || cmd.Args[len(cmd.Args)-2] != "--output" || cmd.Args[len(cmd.Args)-1] != "jsonl" {
		t.Fatalf("JSONL mode args: got %v, want ... --output jsonl", cmd.Args)
	}

	l.mode = ProbeLegacy
	cmd, err = l.buildCommand()
	if err != nil {
		t.Fatalf("buildCommand: %v", err)
	}
	for _, a := range cmd.Args {
		if a == "--output" {
			t.Fatalf("legacy mode should not have --output flag; got %v", cmd.Args)
		}
	}
}

func TestBuildCommandEnv(t *testing.T) {
	os.Clearenv()
	_ = os.Setenv("PATH", "/usr/bin:/bin")
	_ = os.Setenv("HOME", "/root")
	_ = os.Setenv("KEI_RUNTIME_TOKEN", "test-token")
	_ = os.Setenv("DISCORD_TOKEN", "should-not-leak")
	_ = os.Setenv("TWILIO_AUTH_TOKEN", "should-not-leak")

	l := testLink()
	cmd, err := l.buildCommand()
	if err != nil {
		t.Fatalf("buildCommand: %v", err)
	}

	envMap := make(map[string]string)
	for _, e := range cmd.Env {
		parts := strings.SplitN(e, "=", 2)
		if len(parts) == 2 {
			envMap[parts[0]] = parts[1]
		}
	}

	// allowlisted vars should be present
	if _, ok := envMap["PATH"]; !ok {
		t.Fatal("PATH missing from child env")
	}
	if v := envMap["KEI_RUNTIME_TOKEN"]; v != "test-token" {
		t.Fatalf("KEI_RUNTIME_TOKEN: got %q, want %q", v, "test-token")
	}

	// canary secrets must be absent
	if _, ok := envMap["DISCORD_TOKEN"]; ok {
		t.Fatal("DISCORD_TOKEN leaked to child env")
	}
	if _, ok := envMap["TWILIO_AUTH_TOKEN"]; ok {
		t.Fatal("TWILIO_AUTH_TOKEN leaked to child env")
	}

	// SDK identity vars should be injected
	if v := envMap["KEI_AGENTWARE_SDK_LANG"]; v != SDKLang {
		t.Fatalf("KEI_AGENTWARE_SDK_LANG: got %q, want %q", v, SDKLang)
	}
	if _, ok := envMap["KEI_AGENTWARE_SDK_VERSION"]; !ok {
		t.Fatal("KEI_AGENTWARE_SDK_VERSION missing from child env")
	}
}

func TestBuildCommandEnvCount(t *testing.T) {
	l := testLink()
	cmd, err := l.buildCommand()
	if err != nil {
		t.Fatalf("buildCommand: %v", err)
	}

	// count should be len(allowlist) + 2 injected vars
	want := len(ChildEnvAllowlist) + 2
	got := len(cmd.Env)
	// some allowlisted vars may not be in the current env, so we get fewer
	if got > want {
		t.Fatalf("env entries: got %d, want at most %d", got, want)
	}
}

func TestReadLineNormal(t *testing.T) {
	r := bufioReader("hello\nworld\n")
	got1, err := readLine(r)
	if err != nil {
		t.Fatalf("first line: %v", err)
	}
	if string(got1) != "hello" {
		t.Fatalf("first line: got %q, want %q", string(got1), "hello")
	}
	got2, err := readLine(r)
	if err != nil {
		t.Fatalf("second line: %v", err)
	}
	if string(got2) != "world" {
		t.Fatalf("second line: got %q, want %q", string(got2), "world")
	}
}

func TestReadLineEmpty(t *testing.T) {
	r := bufio.NewReader(bytes.NewReader(nil))
	_, err := readLine(r)
	if !errors.Is(err, io.EOF) {
		t.Fatalf("empty buffer: want EOF, got %v", err)
	}
}

func TestReadLineTooLong(t *testing.T) {
	line := strings.Repeat("x", MaxChildLineBytes+10)
	r := bufioReader(line + "\nrest\n")

	_, err := readLine(r)
	if !errors.Is(err, ErrLineTooLong) {
		t.Fatalf("oversize line: want ErrLineTooLong, got %v", err)
	}

	// next line should be readable
	got, err := readLine(r)
	if err != nil {
		t.Fatalf("after oversize: read error: %v", err)
	}
	if string(got) != "rest" {
		t.Fatalf("after oversize: got %q, want %q", string(got), "rest")
	}
}

func TestReadLineAtBoundary(t *testing.T) {
	line := strings.Repeat("x", MaxChildLineBytes)
	r := bufioReader(line + "\n")
	got, err := readLine(r)
	if err != nil {
		t.Fatalf("at-boundary line: %v", err)
	}
	if len(got) != MaxChildLineBytes {
		t.Fatalf("at-boundary line length: got %d, want %d", len(got), MaxChildLineBytes)
	}
}

func TestLifecycleEventEmitted(t *testing.T) {
	l := testLink()

	captured := make(chan LinkEvent, 1)
	l.auditSink = auditSinkFunc(func(ctx context.Context, ev LinkEvent) {
		select {
		case captured <- ev:
		default:
		}
	})

	l.transition(StateConnected, "")

	select {
	case ev := <-captured:
		if ev.Name != EventConnected {
			t.Fatalf("event name: got %q, want %q", ev.Name, EventConnected)
		}
		if ev.State != StateConnected {
			t.Fatalf("event state: got %q, want %q", ev.State, StateConnected)
		}
		if ev.Harness.Kind != HarnessCLI {
			t.Fatalf("event harness kind: got %q, want %q", ev.Harness.Kind, HarnessCLI)
		}
		if ev.SDKInstanceID == "" {
			t.Fatal("event missing SDKInstanceID")
		}
	case <-time.After(time.Second):
		t.Fatal("timed out waiting for lifecycle event")
	}
}

func TestSlowSinkDoesNotBlock(t *testing.T) {
	l := testLink()

	// a blocking sink
	blocking := auditSinkFunc(func(ctx context.Context, ev LinkEvent) {
		<-ctx.Done()
	})
	l.auditSink = blocking

	done := make(chan struct{})
	go func() {
		l.transition(StateConnected, "")
		close(done)
	}()

	select {
	case <-done:
		// transition completed despite blocking sink
	case <-time.After(time.Second):
		t.Fatal("transition blocked on slow audit sink")
	}
}

func TestEventsChannel(t *testing.T) {
	l := testLink()
	l.cfg.Enabled = true

	// transition multiple times; events should appear on channel
	l.transition(StateStarting, "")
	l.transition(StateConnected, "")

	ev1 := <-l.Events()
	if ev1.Name != EventStarted {
		t.Fatalf("first event: got %q, want %q", ev1.Name, EventStarted)
	}
	ev2 := <-l.Events()
	if ev2.Name != EventConnected {
		t.Fatalf("second event: got %q, want %q", ev2.Name, EventConnected)
	}
}

func TestEventsBounded(t *testing.T) {
	l := testLink()

	// fill the event ring
	for i := 0; i < EventBufferSize*2; i++ {
		l.emitLifecycleEvent(EventStarted, StateStarting, "")
	}

	// the ring should have at most EventBufferSize events
	count := 0
	for {
		select {
		case _, ok := <-l.Events():
			if !ok {
				return
			}
			count++
		case <-time.After(time.Second):
			// no more events to drain
			if count > EventBufferSize {
				t.Fatalf("event ring: got %d events, want at most %d", count, EventBufferSize)
			}
			return
		}
	}
}

func TestMetricsSinkCalled(t *testing.T) {
	l := testLink()

	var transitions int32
	l.metricsSink = metricsFunc(func(ctx context.Context, state LinkState, reason FailureClass) {
		atomic.AddInt32(&transitions, 1)
	})

	l.transition(StateStarting, "")
	l.transition(StateConnected, "")

	if n := atomic.LoadInt32(&transitions); n < 2 {
		t.Fatalf("metrics sink called %d times, want at least 2", n)
	}
}

func TestClassifyBeatOutcome(t *testing.T) {
	tests := []struct {
		outcome BeatOutcome
		want    FailureClass
	}{
		{OutcomeCatalogUnreachable, FailureCatalogUnreachable},
		{OutcomeCatalogTimeout, FailureCatalogTimeout},
		{OutcomeCatalogError, FailureCatalogError},
		{OutcomeCatalogBackpressure, FailureCatalogBackpressure},
		{OutcomeCatalogRejected, FailureContractMismatch},
		{OutcomeUnauthorized, FailureRuntimeUnavailable}, // unlikely path, but maps to generic
	}
	for _, tc := range tests {
		t.Run(string(tc.outcome), func(t *testing.T) {
			if got := classifyBeatOutcome(tc.outcome); got != tc.want {
				t.Fatalf("classifyBeatOutcome(%q) = %q, want %q", tc.outcome, got, tc.want)
			}
		})
	}
}

func TestLinkStatusLockFree(t *testing.T) {
	l := testLink()
	l.transition(StateConnected, "")

	// Status should not need a lock
	done := make(chan struct{})
	go func() {
		for i := 0; i < 100; i++ {
			l.transition(StateStarting, "")
			l.transition(StateConnected, "")
		}
		close(done)
	}()

	for i := 0; i < 100; i++ {
		_ = l.Status()
	}
	<-done
}

// --- helpers ---

// testLink creates a link in a known testable state.
func testLink() *link {
	cfg := DefaultConfig()
	cfg.Enabled = true
	cfg.Binary.Path = "/test/binary"
	cfg.Harness.Kind = HarnessCLI
	cfg.Harness.Version = "1.0.0"
	cfg.Harness.DeploymentEnv = "test"
	now := time.Date(2026, 9, 23, 18, 0, 0, 0, time.UTC)
	timeNow = func() time.Time { return now }

	l := &link{
		cfg:    cfg,
		mode:   ProbeJSONL,
		sdkID:  "sdk-test-1",
		state:  StateDisabled,
		events: make(chan LinkEvent, EventBufferSize),
		done:   make(chan struct{}),
	}
	l.ctx, l.cancel = context.WithCancel(context.Background())
	l.updateStatusLocked()
	// close done immediately since no supervisor goroutine runs in tests
	close(l.done)
	return l
}

type metricsFunc func(context.Context, LinkState, FailureClass)

func (f metricsFunc) EmitBeat(ctx context.Context, beat BeatEvent)                     {}
func (f metricsFunc) EmitRestart(ctx context.Context, attempt int, cause FailureClass) {}
func (f metricsFunc) EmitLifecycle(ctx context.Context, state LinkState, reason FailureClass) {
	f(ctx, state, reason)
}

type auditSinkFunc func(context.Context, LinkEvent)

func (f auditSinkFunc) RecordLifecycle(ctx context.Context, ev LinkEvent) { f(ctx, ev) }

func bufioReader(s string) *bufio.Reader {
	return bufio.NewReader(strings.NewReader(s))
}
