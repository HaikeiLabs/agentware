package runtimelink

import (
	"bufio"
	"bytes"
	"context"
	"errors"
	"fmt"
	"io"
	"os"
	"os/exec"
	"sync"
	"sync/atomic"
	"time"
)

// CapabilityProbeMode selects the child launch mode.
type CapabilityProbeMode int

const (
	// ProbeJSONL means --output jsonl is supported.
	ProbeJSONL CapabilityProbeMode = iota + 1
	// ProbeLegacy means the binary does not support --output jsonl.
	ProbeLegacy
)

// defaultProbeTimeout is how long the capability probe may take.
const defaultProbeTimeout = 5 * time.Second

// crashloopThreshold is the restart count that triggers crashloop detection.
const crashloopThreshold = 5

// crashloopWindow is the sliding window for crashloop detection.
const crashloopWindow = 15 * time.Minute

// linkOption is a configuration function for New.
type linkOption func(*link)

// WithMetricsSink sets the optional metrics sink.
func WithMetricsSink(s MetricsSink) linkOption {
	return func(l *link) { l.metricsSink = s }
}

// WithLifecycleAuditSink sets the optional lifecycle audit sink.
func WithLifecycleAuditSink(s LifecycleAuditSink) linkOption {
	return func(l *link) { l.auditSink = s }
}

// New creates a RuntimeLink from a validated Config. It runs the capability
// probe synchronously. The returned link is not started; call Start(ctx).
func New(cfg Config, opts ...linkOption) (RuntimeLink, error) {
	cfg, err := NormalizeConfig(cfg)
	if err != nil {
		return nil, err
	}
	if !cfg.Enabled {
		return &link{cfg: cfg, state: StateDisabled}, nil
	}

	mode, err := probeCapability(cfg.Binary.Path)
	if err != nil {
		return nil, ErrConfigBinary
	}

	l := &link{
		cfg:       cfg,
		mode:      mode,
		state:     StateDisabled,
		sdkID:     newSDKInstanceID(),
		events:    make(chan LinkEvent, EventBufferSize),
		done:      make(chan struct{}),
		crashTime: make([]time.Time, 0, crashloopThreshold),
	}
	for _, o := range opts {
		o(l)
	}
	return l, nil
}

// probeCapability checks whether the binary supports --output jsonl.
// Any error during probing (binary not found, exits non-zero, times out)
// is treated as legacy fallback.
func probeCapability(binary string) (CapabilityProbeMode, error) {
	ctx, cancel := context.WithTimeout(context.Background(), defaultProbeTimeout)
	defer cancel()

	cmd := exec.CommandContext(ctx, binary, "--output", "jsonl", "--version")
	if err := cmd.Run(); err != nil {
		return ProbeLegacy, nil
	}
	return ProbeJSONL, nil
}

// link implements RuntimeLink.
type link struct {
	cfg   Config
	mode  CapabilityProbeMode
	sdkID string

	// supervisor lifecycle
	ctx       context.Context
	cancel    context.CancelFunc
	done      chan struct{}
	startOnce sync.Once
	stopOnce  sync.Once

	// child process management
	cmd     *exec.Cmd
	childMu sync.Mutex

	// state machine
	mu       sync.Mutex
	state    LinkState
	reason   FailureClass
	lastBeat time.Time
	lastOK   time.Time
	fails    int
	restarts int
	runID    string
	identity *RuntimeIdentity

	// crashloop tracking
	crashTime []time.Time
	crashMu   sync.Mutex

	// events ring
	events chan LinkEvent

	// sinks (set at construction, immutable after)
	metricsSink MetricsSink
	auditSink   LifecycleAuditSink

	// cached status for lock-free reads
	statusCache atomic.Value
}

func (l *link) Start(ctx context.Context) error {
	if !l.cfg.Enabled {
		return nil
	}
	var err error
	l.startOnce.Do(func() {
		l.ctx, l.cancel = context.WithCancel(ctx)
		go l.supervise()
	})
	return err
}

func (l *link) Stop(ctx context.Context) error {
	if !l.cfg.Enabled {
		return nil
	}
	l.stopOnce.Do(func() {
		l.transition(StateStopped, "")
		if l.cancel != nil {
			l.cancel()
		}
		l.killChild()
	})
	select {
	case <-l.done:
	case <-ctx.Done():
		return ctx.Err()
	}
	return nil
}

func (l *link) Status() LinkStatus {
	if v := l.statusCache.Load(); v != nil {
		return v.(LinkStatus)
	}
	l.mu.Lock()
	defer l.mu.Unlock()
	return l.buildStatus()
}

func (l *link) Identity() (RuntimeIdentity, bool) {
	l.mu.Lock()
	defer l.mu.Unlock()
	if l.identity == nil {
		return RuntimeIdentity{}, false
	}
	return *l.identity, true
}

func (l *link) Events() <-chan LinkEvent {
	return l.events
}

// supervise is the main supervisor goroutine.
func (l *link) supervise() {
	defer close(l.done)
	defer l.killChild()

	for {
		select {
		case <-l.ctx.Done():
			return
		default:
		}

		// backoff if this is a restart
		if l.restarts > 0 {
			l.transition(StateReconnecting, "")
			if err := l.cfg.Restart.Wait(l.ctx, l.restarts-1, randFloat()); err != nil {
				return
			}
		}

		if err := l.spawnAndWatch(); err != nil {
			if errors.Is(err, context.Canceled) {
				return
			}
			// terminal errors stop the supervisor
			if errors.Is(err, errTerminal) {
				return
			}
			// transient: retry with backoff
			l.restarts++
			l.crashMu.Lock()
			l.crashTime = append(l.crashTime, time.Now())
			if len(l.crashTime) > crashloopThreshold {
				l.crashTime = l.crashTime[1:]
			}
			crashloop := len(l.crashTime) >= crashloopThreshold &&
				l.crashTime[len(l.crashTime)-1].Sub(l.crashTime[0]) <= crashloopWindow
			l.crashMu.Unlock()

			if crashloop {
				l.transition(StateTerminal, FailureRuntimeCrashloop)
				return
			}
		}
	}
}

var errTerminal = errors.New("runtimelink: terminal child exit")

// spawnAndWatch starts the child and watches it until exit or context done.
func (l *link) spawnAndWatch() error {
	l.transition(StateStarting, "")

	cmd, err := l.buildCommand()
	if err != nil {
		l.reason = FailureRuntimeUnavailable
		return err
	}

	l.childMu.Lock()
	l.cmd = cmd
	l.childMu.Unlock()

	stdout, err := cmd.StdoutPipe()
	if err != nil {
		return fmt.Errorf("stdout pipe: %w", err)
	}

	stderr, err := cmd.StderrPipe()
	if err != nil {
		return fmt.Errorf("stderr pipe: %w", err)
	}

	stdin, err := cmd.StdinPipe()
	if err != nil {
		return fmt.Errorf("stdin pipe: %w", err)
	}

	if err := cmd.Start(); err != nil {
		l.reason = FailureRuntimeUnavailable
		return fmt.Errorf("start child: %w", err)
	}

	l.emitLifecycleEvent(EventStarted, StateStarting, "")
	l.emitMetrics(StateStarting, "")

	// close parent stdin so child sees EOF on its stdin
	stdin.Close()

	// read stderr concurrently; capture last N lines
	stderrCtx, stderrCancel := context.WithCancel(l.ctx)
	stderrLines := make(chan string, l.cfg.LogCount)
	go readStderr(stderrCtx, stderr, stderrLines, max(1, l.cfg.LogCount))

	err = l.watchChild(stdout, cmd)
	stderrCancel()
	_ = drainLines(stderrLines)

	// drain remaining stderr
	io.Copy(io.Discard, stderr)

	exitCode := 0
	if err != nil {
		// child exited with an error
		var exitErr *exec.ExitError
		if errors.As(err, &exitErr) {
			exitCode = exitErr.ExitCode()
		}
	}

	return l.handleChildExit(exitCode, err)
}

// watchChild reads child stdout and handles events.
func (l *link) watchChild(stdout io.Reader, cmd *exec.Cmd) error {
	reader := bufio.NewReaderSize(stdout, MaxChildLineBytes+1)

	// identity timeout
	identityTimer := time.NewTimer(l.cfg.Grace)
	defer identityTimer.Stop()

	// heartbeat watchdog
	beatTimer := time.NewTimer(l.cfg.Interval + l.cfg.Grace)
	beatTimer.Stop() // don't fire until we have identity

	for {
		line, err := readLine(reader)
		if err != nil {
			if errors.Is(err, io.EOF) {
				// child closed stdout; check if it exited
				return cmd.Wait()
			}
			return err
		}

		ev, parseErr := ParseChildLine(line)
		if parseErr != nil {
			// count parse errors but keep reading
			l.fails++
			l.updateStatusLocked()
			if errors.Is(parseErr, ErrLineTooLong) || errors.Is(parseErr, ErrMalformedLine) {
				continue
			}
			if errors.Is(parseErr, ErrContractMismatch) {
				l.fails++
				l.updateStatusLocked()
				continue
			}
			continue
		}

		switch ev.Kind {
		case ChildIdentity:
			l.handleIdentity(ev.Identity)

			if !identityTimer.Stop() {
				select {
				case <-identityTimer.C:
				default:
				}
			}
			beatTimer.Reset(l.cfg.Interval + l.cfg.Grace)

		case ChildBeat:
			l.handleBeat(ev.Beat)
			beatTimer.Reset(l.cfg.Interval + l.cfg.Grace)

		case ChildTerminal:
			return l.handleChildTerminal(ev.Terminal)

		case ChildIgnored:
			// forward-compatible; ignore
		}

		select {
		case <-l.ctx.Done():
			return context.Canceled
		case <-identityTimer.C:
			// didn't get identity within grace period
			l.reason = FailureRuntimeUnresponsive
			l.killChild()
			return cmd.Wait()
		case <-beatTimer.C:
			// no beat within grace period after identity
			l.reason = FailureRuntimeUnresponsive
			l.killChild()
			return cmd.Wait()
		default:
		}
	}
}

func readLine(reader *bufio.Reader) ([]byte, error) {
	var buf bytes.Buffer
	buf.Grow(MaxChildLineBytes)
	for {
		b, err := reader.ReadByte()
		if err != nil {
			if buf.Len() > 0 {
				return buf.Bytes(), nil
			}
			return nil, err
		}
		if b == '\n' {
			return buf.Bytes(), nil
		}
		if buf.Len() >= MaxChildLineBytes {
			// line is too long; drain to end of line
			for {
				c, err := reader.ReadByte()
				if err != nil || c == '\n' {
					break
				}
			}
			return nil, ErrLineTooLong
		}
		buf.WriteByte(b)
	}
}

func (l *link) handleIdentity(id *RuntimeIdentity) {
	l.mu.Lock()
	defer l.mu.Unlock()

	l.identity = id
	l.runID = id.RunID
	l.fails = 0

	if l.state == StateStarting {
		l.state = StateConnected
		l.reason = ""
		l.emitLifecycleEvent(EventConnected, StateConnected, "")
		l.emitMetrics(StateConnected, "")
	} else {
		l.state = StateDegraded
		l.emitLifecycleEvent(EventDegraded, StateDegraded, "")
		l.emitMetrics(StateDegraded, "")
	}
	l.updateStatusLocked()
}

func (l *link) handleBeat(b *BeatEvent) {
	l.mu.Lock()
	defer l.mu.Unlock()

	l.lastBeat = timeNow()
	if b.RunID != "" {
		l.runID = b.RunID
	}

	if l.metricsSink != nil {
		l.metricsSink.EmitBeat(l.ctx, *b)
	}

	switch b.Outcome {
	case OutcomeOK:
		l.lastOK = l.lastBeat
		l.fails = 0
		if l.state == StateDegraded || l.state == StateReconnecting {
			l.state = StateConnected
			l.reason = ""
			l.emitLifecycleEvent(EventConnected, StateConnected, "")
			l.emitMetrics(StateConnected, "")
		}
	case OutcomeUnauthorized:
		l.state = StateTerminal
		l.reason = FailureInstallationUnauthorized
		l.emitLifecycleEvent(EventTerminal, StateTerminal, FailureInstallationUnauthorized)
		l.emitMetrics(StateTerminal, FailureInstallationUnauthorized)
	default:
		l.fails++
		if l.state == StateConnected {
			l.state = StateDegraded
			l.reason = classifyBeatOutcome(b.Outcome)
			l.emitLifecycleEvent(EventDegraded, StateDegraded, l.reason)
			l.emitMetrics(StateDegraded, l.reason)
		}
	}
	l.updateStatusLocked()
}

func (l *link) handleChildTerminal(t *TerminalEvent) error {
	l.mu.Lock()
	defer l.mu.Unlock()

	l.state = StateTerminal
	switch t.Reason {
	case "unauthorized":
		l.reason = FailureInstallationUnauthorized
	case "contract":
		l.reason = FailureContractMismatch
	default:
		l.reason = FailureRuntimeUnavailable
	}
	l.emitLifecycleEvent(EventTerminal, StateTerminal, l.reason)
	l.emitMetrics(StateTerminal, l.reason)
	l.updateStatusLocked()
	return errTerminal
}

func (l *link) handleChildExit(exitCode int, waitErr error) error {
	l.mu.Lock()
	defer l.mu.Unlock()

	if waitErr != nil && !errors.Is(waitErr, context.Canceled) {
		// map exit code to failure class
		switch {
		case exitCode == 0:
			// clean exit
			return nil
		case exitCode == 2:
			l.reason = FailureContractMismatch
		case exitCode == 3:
			l.reason = FailureInstallationUnauthorized
		case exitCode >= 4:
			l.reason = FailureConfigInvalid
		case exitCode >= 1:
			l.reason = FailureRuntimeUnavailable
		}
	}

	if l.reason == FailureInstallationUnauthorized || l.reason == FailureContractMismatch {
		l.state = StateTerminal
		l.emitLifecycleEvent(EventTerminal, StateTerminal, l.reason)
		l.emitMetrics(StateTerminal, l.reason)
		l.updateStatusLocked()
		return errTerminal
	}

	if l.reason == FailureConfigInvalid {
		l.state = StateTerminal
		l.emitLifecycleEvent(EventTerminal, StateTerminal, l.reason)
		l.emitMetrics(StateTerminal, l.reason)
		l.updateStatusLocked()
		return errTerminal
	}

	// transient: will be retried
	l.restarts++
	if l.metricsSink != nil {
		l.metricsSink.EmitRestart(l.ctx, l.restarts, l.reason)
	}
	l.updateStatusLocked()
	return nil
}

// buildCommand constructs the child exec.Cmd with pinned args and env.
func (l *link) buildCommand() (*exec.Cmd, error) {
	args := []string{"runtime", "heartbeat"}
	if l.mode == ProbeJSONL {
		args = append(args, "--output", "jsonl")
	}

	cmd := exec.CommandContext(l.ctx, l.cfg.Binary.Path, args...)
	setSysProcAttr(cmd)

	// build env from allowlist only
	env := make([]string, 0, len(ChildEnvAllowlist))
	for _, name := range ChildEnvAllowlist {
		if v, ok := os.LookupEnv(name); ok {
			env = append(env, name+"="+v)
		}
	}
	// inject SDK identity vars
	env = append(env,
		"KEI_AGENTWARE_SDK_LANG="+SDKLang,
		"KEI_AGENTWARE_SDK_VERSION="+sdkVersion,
	)
	cmd.Env = env

	return cmd, nil
}

// killChild kills the child process group.
func (l *link) killChild() {
	l.childMu.Lock()
	defer l.childMu.Unlock()
	if l.cmd != nil && l.cmd.Process != nil {
		killProcessGroup(l.cmd)
		l.cmd = nil
	}
}

// transition updates the state and reason, emits lifecycle event and metrics.
func (l *link) transition(state LinkState, reason FailureClass) {
	l.mu.Lock()
	defer l.mu.Unlock()
	l.state = state
	l.reason = FailureClass(reason)

	var eventName LifecycleEventName
	switch state {
	case StateStarting:
		eventName = EventStarted
	case StateConnected:
		eventName = EventConnected
	case StateDegraded:
		eventName = EventDegraded
	case StateReconnecting:
		eventName = EventReconnecting
	case StateTerminal:
		eventName = EventTerminal
	case StateStopped:
		eventName = EventStopped
	default:
		return
	}

	l.emitLifecycleEvent(eventName, state, reason)
	l.emitMetrics(state, reason)
	l.updateStatusLocked()
}

func (l *link) emitLifecycleEvent(name LifecycleEventName, state LinkState, reason FailureClass) {
	now := timeNow()
	ev := LinkEvent{
		Name:          name,
		At:            now,
		State:         state,
		Reason:        FailureClass(reason),
		RunID:         l.runID,
		Seq:           now.UnixMilli(),
		SDKInstanceID: l.sdkID,
		Harness: EventHarness{
			Kind:          l.cfg.Harness.Kind,
			Version:       l.cfg.Harness.Version,
			DeploymentEnv: l.cfg.Harness.DeploymentEnv,
			SDKLang:       SDKLang,
			SDKVersion:    sdkVersion,
		},
		Restarts:         l.restarts,
		ConsecutiveFails: l.fails,
	}

	// push to event ring (non-blocking)
	select {
	case l.events <- ev:
	default:
	}

	// send to audit sink (non-blocking in goroutine)
	if l.auditSink != nil {
		go l.auditSink.RecordLifecycle(l.ctx, ev)
	}
}

func (l *link) emitMetrics(state LinkState, reason FailureClass) {
	if l.metricsSink != nil {
		l.metricsSink.EmitLifecycle(l.ctx, state, reason)
	}
}

func (l *link) buildStatus() LinkStatus {
	return LinkStatus{
		State:            l.state,
		Reason:           l.reason,
		LastBeatAt:       l.lastBeat,
		LastCatalogOKAt:  l.lastOK,
		ConsecutiveFails: l.fails,
		RunID:            l.runID,
		Restarts:         l.restarts,
	}
}

func (l *link) updateStatusLocked() {
	l.statusCache.Store(l.buildStatus())
}

func classifyBeatOutcome(o BeatOutcome) FailureClass {
	switch o {
	case OutcomeCatalogUnreachable:
		return FailureCatalogUnreachable
	case OutcomeCatalogTimeout:
		return FailureCatalogTimeout
	case OutcomeCatalogError:
		return FailureCatalogError
	case OutcomeCatalogBackpressure:
		return FailureCatalogBackpressure
	case OutcomeCatalogRejected:
		return FailureContractMismatch
	default:
		return FailureRuntimeUnavailable
	}
}

// drainLines drains and discards remaining lines from the channel.
func drainLines(ch <-chan string) int {
	n := 0
	for range ch {
		n++
	}
	return n
}

// readStderr reads child stderr into a bounded channel.
func readStderr(ctx context.Context, r io.Reader, lines chan string, max int) {
	scanner := bufio.NewScanner(r)
	for scanner.Scan() && ctx.Err() == nil {
		select {
		case lines <- scanner.Text():
		default:
			// channel full; drain one then retry
			select {
			case <-lines:
			default:
			}
			select {
			case lines <- scanner.Text():
			default:
			}
		}
	}
}

// newSDKInstanceID returns a new SDK instance identifier.
var newSDKInstanceID = func() string {
	return fmt.Sprintf("sdk-%d", time.Now().UnixNano())
}

// sdkVersion is set at build time or inferred.
var sdkVersion = "0.0.0"

// timeNow is overridable in tests.
var timeNow = time.Now

// randFloat returns a random float64 in [0, 1).
var randFloat = func() float64 {
	return float64(timeNow().UnixNano()%1e9) / 1e9
}
