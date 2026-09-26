package runtimelink

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io/fs"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"
	"time"
)

// fixtureDir holds the cross-language fixtures. They live at the repository
// root, outside the go/ module, so they are not part of the module zip.
const fixtureDir = "../../../testing/contracts/runtime-link"

func loadFixture(t *testing.T, name string, v any) {
	t.Helper()
	data, err := os.ReadFile(filepath.Join(fixtureDir, name))
	if errors.Is(err, fs.ErrNotExist) && inModuleCache(t) {
		t.Skipf("contract fixtures ship with the repository, not the module zip: %v", err)
	}
	if err != nil {
		t.Fatalf("read fixture %s: %v", name, err)
	}
	if err := json.Unmarshal(data, v); err != nil {
		t.Fatalf("decode fixture %s: %v", name, err)
	}
}

// inModuleCache reports whether the package runs from a downloaded module
// (a "<module>@<version>" directory), where `go test all` in a consumer would
// otherwise fail on the missing repository fixtures. Inside the repository a
// missing fixture is still a failure.
func inModuleCache(t *testing.T) bool {
	t.Helper()
	wd, err := os.Getwd()
	if err != nil {
		t.Fatalf("getwd: %v", err)
	}
	return strings.Contains(filepath.ToSlash(wd), "@v")
}

func asStrings[T ~string](in []T) []string {
	out := make([]string, len(in))
	for i, v := range in {
		out[i] = string(v)
	}
	return out
}

func TestEnumsMatchContract(t *testing.T) {
	var enums struct {
		ContractVersion   int      `json:"contract_version"`
		HarnessKinds      []string `json:"harness_kinds"`
		LinkStates        []string `json:"link_states"`
		FailureClasses    []string `json:"failure_classes"`
		BeatOutcomes      []string `json:"beat_outcomes"`
		LifecycleEvents   []string `json:"lifecycle_events"`
		SDKLangs          []string `json:"sdk_langs"`
		ConfigErrorCodes  []string `json:"config_error_codes"`
		ChildEnvAllowlist []string `json:"child_env_allowlist"`
		MaxChildLineBytes int      `json:"max_child_line_bytes"`
		EventBufferSize   int      `json:"event_buffer_size"`
	}
	loadFixture(t, "enums.json", &enums)

	codes := make([]string, 0, len(codeSentinels))
	for _, c := range enums.ConfigErrorCodes {
		if _, ok := codeSentinels[c]; ok {
			codes = append(codes, c)
		}
	}
	checks := []struct {
		name      string
		got, want any
	}{
		{"contract_version", ContractVersion, enums.ContractVersion},
		{"harness_kinds", asStrings(HarnessKinds), enums.HarnessKinds},
		{"link_states", asStrings(LinkStates), enums.LinkStates},
		{"failure_classes", asStrings(FailureClasses), enums.FailureClasses},
		{"beat_outcomes", asStrings(BeatOutcomes), enums.BeatOutcomes},
		{"lifecycle_events", asStrings(LifecycleEventNames), enums.LifecycleEvents},
		{"sdk_langs", SDKLangs, enums.SDKLangs},
		{"config_error_codes", codes, enums.ConfigErrorCodes},
		{"config_error_code_count", len(codeSentinels), len(enums.ConfigErrorCodes)},
		{"child_env_allowlist", ChildEnvAllowlist, enums.ChildEnvAllowlist},
		{"max_child_line_bytes", MaxChildLineBytes, enums.MaxChildLineBytes},
		{"event_buffer_size", EventBufferSize, enums.EventBufferSize},
	}
	for _, c := range checks {
		if !reflect.DeepEqual(c.got, c.want) {
			t.Errorf("%s: got %v, want %v", c.name, c.got, c.want)
		}
	}
}

type contractConfig struct {
	Enabled         bool   `json:"enabled"`
	TokenEnv        string `json:"token_env"`
	ControlPlaneURL string `json:"control_plane_url"`
	BinaryPath      string `json:"binary_path"`
	BinarySHA256    string `json:"binary_sha256"`
	IntervalS       int    `json:"interval_s"`
	BeatTimeoutS    int    `json:"beat_timeout_s"`
	GraceS          int    `json:"grace_s"`
	RestartMinS     int    `json:"restart_min_s"`
	RestartMaxS     int    `json:"restart_max_s"`
	StableResetS    int    `json:"stable_reset_s"`
	StopTimeoutS    int    `json:"stop_timeout_s"`
	LogCount        int    `json:"log_count"`
	Harness         struct {
		Kind          string `json:"kind"`
		Version       string `json:"version"`
		DeploymentEnv string `json:"deployment_env"`
	} `json:"harness"`
}

func toContract(c Config) contractConfig {
	var out contractConfig
	out.Enabled = c.Enabled
	out.TokenEnv = c.Token.Env
	out.ControlPlaneURL = c.ControlPlaneURL
	out.BinaryPath = c.Binary.Path
	out.BinarySHA256 = c.Binary.SHA256
	out.IntervalS = int(c.Interval / time.Second)
	out.BeatTimeoutS = int(c.BeatTimeout / time.Second)
	out.GraceS = int(c.Grace / time.Second)
	out.RestartMinS = int(c.Restart.Min / time.Second)
	out.RestartMaxS = int(c.Restart.Max / time.Second)
	out.StableResetS = int(c.Restart.StableReset / time.Second)
	out.StopTimeoutS = int(c.StopTimeout / time.Second)
	out.LogCount = c.LogCount
	out.Harness.Kind = string(c.Harness.Kind)
	out.Harness.Version = c.Harness.Version
	out.Harness.DeploymentEnv = c.Harness.DeploymentEnv
	return out
}

func TestConfigFromEnvContract(t *testing.T) {
	var fx struct {
		TokenCanary string `json:"token_canary"`
		Cases       []struct {
			Name    string            `json:"name"`
			Env     map[string]string `json:"env"`
			Harness struct {
				Kind          string `json:"kind"`
				Version       string `json:"version"`
				DeploymentEnv string `json:"deployment_env"`
			} `json:"harness"`
			Expect struct {
				OK     bool           `json:"ok"`
				Error  string         `json:"error"`
				Config contractConfig `json:"config"`
			} `json:"expect"`
		} `json:"cases"`
	}
	loadFixture(t, "config-cases.json", &fx)

	for _, tc := range fx.Cases {
		t.Run(tc.Name, func(t *testing.T) {
			lookup := func(k string) (string, bool) { v, ok := tc.Env[k]; return v, ok }
			harness := HarnessEnvelope{
				Kind: HarnessKind(tc.Harness.Kind), Version: tc.Harness.Version, DeploymentEnv: tc.Harness.DeploymentEnv,
			}
			cfg, err := ConfigFromEnv(lookup, harness)
			if !tc.Expect.OK {
				if err == nil {
					t.Fatalf("want error %q, got config %+v", tc.Expect.Error, cfg)
				}
				if got := ErrorCode(err); got != tc.Expect.Error {
					t.Fatalf("error code: got %q (%v), want %q", got, err, tc.Expect.Error)
				}
				if !errors.Is(err, ErrConfig) || !errors.Is(err, codeSentinels[tc.Expect.Error]) {
					t.Fatalf("error %v does not wrap its sentinels", err)
				}
				if strings.Contains(err.Error(), fx.TokenCanary) {
					t.Fatalf("error leaks token: %v", err)
				}
				return
			}
			if err != nil {
				t.Fatalf("unexpected error: %v", err)
			}
			if got := toContract(cfg); !reflect.DeepEqual(got, tc.Expect.Config) {
				t.Fatalf("config:\n got  %+v\n want %+v", got, tc.Expect.Config)
			}
			if strings.Contains(strings.ReplaceAll(toString(cfg), " ", ""), fx.TokenCanary) {
				t.Fatal("config retains the token value")
			}
		})
	}
}

func toString(v any) string {
	b, _ := json.Marshal(v)
	return string(b)
}

func TestBackoffContract(t *testing.T) {
	var fx struct {
		Cases []struct {
			MinMS   int64   `json:"min_ms"`
			MaxMS   int64   `json:"max_ms"`
			Attempt int     `json:"attempt"`
			Rand    float64 `json:"rand"`
			DelayMS int64   `json:"delay_ms"`
		} `json:"cases"`
	}
	loadFixture(t, "backoff-cases.json", &fx)
	for _, tc := range fx.Cases {
		b := Backoff{Min: time.Duration(tc.MinMS) * time.Millisecond, Max: time.Duration(tc.MaxMS) * time.Millisecond}
		if got := b.Delay(tc.Attempt, tc.Rand).Milliseconds(); got != tc.DelayMS {
			t.Errorf("Delay(min=%d max=%d attempt=%d r=%v) = %dms, want %dms",
				tc.MinMS, tc.MaxMS, tc.Attempt, tc.Rand, got, tc.DelayMS)
		}
	}
}

type childFixture struct {
	Cases []struct {
		Name   string `json:"name"`
		Line   string `json:"line"`
		Expect struct {
			Kind          string         `json:"kind"`
			Fields        map[string]any `json:"fields"`
			DroppedAgents int            `json:"dropped_agents"`
		} `json:"expect"`
	} `json:"cases"`
	Synthesized []struct {
		Name   string `json:"name"`
		Bytes  int    `json:"bytes"`
		Expect struct {
			Kind string `json:"kind"`
		} `json:"expect"`
	} `json:"synthesized"`
}

// padLine builds {"v":1,"event":"pad","pad":"xxx"} exactly n bytes long.
func padLine(n int) string {
	const head, tail = `{"v":1,"event":"pad","pad":"`, `"}`
	return head + strings.Repeat("x", n-len(head)-len(tail)) + tail
}

func childKind(ev ChildEvent, err error) (string, any) {
	switch {
	case errors.Is(err, ErrLineTooLong):
		return "line_too_long", nil
	case errors.Is(err, ErrContractMismatch):
		return "contract_mismatch", nil
	case errors.Is(err, ErrMalformedLine):
		return "malformed", nil
	case err != nil:
		return "error:" + err.Error(), nil
	}
	switch ev.Kind {
	case ChildIdentity:
		return string(ev.Kind), ev.Identity
	case ChildBeat:
		return string(ev.Kind), ev.Beat
	case ChildTerminal:
		return string(ev.Kind), ev.Terminal
	}
	return string(ev.Kind), nil
}

func TestParseChildLineContract(t *testing.T) {
	var fx childFixture
	loadFixture(t, "wire/child-events.json", &fx)
	for _, tc := range fx.Cases {
		t.Run(tc.Name, func(t *testing.T) {
			ev, err := ParseChildLine([]byte(tc.Line))
			kind, payload := childKind(ev, err)
			if kind != tc.Expect.Kind {
				t.Fatalf("kind: got %q, want %q", kind, tc.Expect.Kind)
			}
			if ev.DroppedAgents != tc.Expect.DroppedAgents {
				t.Fatalf("dropped agents: got %d, want %d", ev.DroppedAgents, tc.Expect.DroppedAgents)
			}
			if tc.Expect.Fields == nil {
				return
			}
			var got map[string]any
			if err := json.Unmarshal([]byte(toString(payload)), &got); err != nil {
				t.Fatal(err)
			}
			if !reflect.DeepEqual(got, tc.Expect.Fields) {
				t.Fatalf("fields:\n got  %v\n want %v", got, tc.Expect.Fields)
			}
		})
	}
	for _, tc := range fx.Synthesized {
		t.Run(tc.Name, func(t *testing.T) {
			line := padLine(tc.Bytes)
			if len(line) != tc.Bytes {
				t.Fatalf("padLine built %d bytes, want %d", len(line), tc.Bytes)
			}
			if kind, _ := childKind(ParseChildLine([]byte(line + "\n"))); kind != tc.Expect.Kind {
				t.Fatalf("kind: got %q, want %q", kind, tc.Expect.Kind)
			}
		})
	}
}

func TestLifecycleRedactionContract(t *testing.T) {
	var fx struct {
		RecordKeys  []string `json:"record_keys"`
		HarnessKeys []string `json:"harness_keys"`
		Canaries    []string `json:"canaries"`
		Cases       []struct {
			Name   string          `json:"name"`
			Input  json.RawMessage `json:"input"`
			Expect struct {
				OK         bool           `json:"ok"`
				Serialized map[string]any `json:"serialized"`
			} `json:"expect"`
		} `json:"cases"`
	}
	loadFixture(t, "lifecycle-redaction.json", &fx)

	for _, tc := range fx.Cases {
		t.Run(tc.Name, func(t *testing.T) {
			var ev LinkEvent
			err := json.Unmarshal(tc.Input, &ev)
			if !tc.Expect.OK {
				if !errors.Is(err, ErrInvalidEvent) {
					t.Fatalf("want ErrInvalidEvent, got %v", err)
				}
				return
			}
			if err != nil {
				t.Fatalf("decode: %v", err)
			}
			out, err := json.Marshal(ev)
			if err != nil {
				t.Fatalf("encode: %v", err)
			}
			for _, canary := range fx.Canaries {
				if strings.Contains(string(out), canary) {
					t.Errorf("serialized event leaks %q: %s", canary, out)
				}
			}
			var got map[string]any
			if err := json.Unmarshal(out, &got); err != nil {
				t.Fatal(err)
			}
			if !reflect.DeepEqual(got, tc.Expect.Serialized) {
				t.Fatalf("serialized:\n got  %v\n want %v", got, tc.Expect.Serialized)
			}
			var keys struct {
				Top     map[string]json.RawMessage
				Harness map[string]json.RawMessage
			}
			_ = json.Unmarshal(out, &keys.Top)
			_ = json.Unmarshal(keys.Top["harness"], &keys.Harness)
			if len(keys.Top) != len(fx.RecordKeys) || len(keys.Harness) != len(fx.HarnessKeys) {
				t.Fatalf("key sets: got %d/%d, want %d/%d",
					len(keys.Top), len(keys.Harness), len(fx.RecordKeys), len(fx.HarnessKeys))
			}
			if !strings.HasPrefix(string(out), `{"name":`) {
				t.Fatalf("serialized key order changed: %s", out)
			}
		})
	}
}

func TestLinkEventMarshalRejectsInvalid(t *testing.T) {
	ev := LinkEvent{Name: EventStarted, State: StateStarting, SDKInstanceID: "sdk-1",
		Harness: EventHarness{Kind: HarnessCLI, SDKLang: SDKLang}}
	if _, err := json.Marshal(ev); !errors.Is(err, ErrInvalidEvent) {
		t.Fatalf("zero timestamp: want ErrInvalidEvent, got %v", err)
	}
	ev.At = time.Date(2026, 9, 23, 18, 0, 0, 0, time.FixedZone("x", 2*3600))
	out, err := json.Marshal(ev)
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(string(out), `"at":"2026-09-23T16:00:00.000Z"`) {
		t.Fatalf("timestamp not normalized to UTC millis: %s", out)
	}
	ev.Harness.Kind = "telegram"
	if _, err := json.Marshal(ev); !errors.Is(err, ErrInvalidEvent) {
		t.Fatalf("unknown kind: want ErrInvalidEvent, got %v", err)
	}
}

func TestNormalizeConfigDirect(t *testing.T) {
	base := DefaultConfig()
	base.Enabled = true
	base.ControlPlaneURL = "http://localhost:8080"
	base.Harness.Kind = HarnessCLI

	if _, err := NormalizeConfig(base); err != nil {
		t.Fatalf("valid config rejected: %v", err)
	}
	tests := []struct {
		name   string
		mutate func(*Config)
		code   string
	}{
		{"unknown kind", func(c *Config) { c.Harness.Kind = "telegram" }, CodeHarnessKind},
		{"legacy token env", func(c *Config) { c.Token.Env = EnvLegacyToken }, CodeLegacyToken},
		{"other token env", func(c *Config) { c.Token.Env = "DISCORD_TOKEN" }, CodeInvalidValue},
		{"timeout equal to half interval", func(c *Config) { c.Interval = 20 * time.Second; c.BeatTimeout = 10 * time.Second }, CodeBeatTimeout},
		{"negative grace", func(c *Config) { c.Grace = -time.Second }, CodeOutOfRange},
		{"grace too long", func(c *Config) { c.Grace = MaxGrace + time.Second }, CodeOutOfRange},
		{"stop timeout zero", func(c *Config) { c.StopTimeout = 0 }, CodeOutOfRange},
		{"stop timeout too long", func(c *Config) { c.StopTimeout = 2 * time.Minute }, CodeOutOfRange},
		{"stable reset zero", func(c *Config) { c.Restart.StableReset = 0 }, CodeOutOfRange},
		{"empty binary", func(c *Config) { c.Binary.Path = "" }, CodeBinary},
		{"url without host", func(c *Config) { c.ControlPlaneURL = "https://" }, CodeControlPlaneURL},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			cfg := base
			tt.mutate(&cfg)
			if _, err := NormalizeConfig(cfg); ErrorCode(err) != tt.code {
				t.Fatalf("got %v, want code %q", err, tt.code)
			}
		})
	}

	disabled := DefaultConfig()
	disabled.Interval = 0
	disabled.BeatTimeout = 5 * time.Second
	got, err := NormalizeConfig(disabled)
	if err != nil {
		t.Fatalf("disabled config rejected: %v", err)
	}
	if got.Interval != MinInterval {
		t.Fatalf("interval not clamped: %v", got.Interval)
	}
}

func TestExitCodeMappingContract(t *testing.T) {
	var fx struct {
		Note  string `json:"note"`
		Cases []struct {
			ExitCode     int    `json:"exit_code"`
			FailureClass string `json:"failure_class"`
			Terminal     bool   `json:"terminal"`
			Restart      bool   `json:"restart"`
		} `json:"cases"`
	}
	loadFixture(t, "exit-code-cases.json", &fx)

	for _, tc := range fx.Cases {
		t.Run(fmt.Sprintf("exit_%d", tc.ExitCode), func(t *testing.T) {
			l := testLink()
			l.transition(StateConnected, "")

			var waitErr error
			if tc.ExitCode != 0 {
				waitErr = errors.New("exit error")
			}

			err := l.handleChildExit(tc.ExitCode, waitErr)

			if tc.ExitCode == 0 {
				if err != nil {
					t.Fatalf("exit 0: want nil error, got %v", err)
				}
				if l.Status().State != StateConnected {
					t.Fatalf("exit 0: state changed to %q, want %q", l.Status().State, StateConnected)
				}
				return
			}

			st := l.Status()
			if tc.Terminal {
				if !errors.Is(err, errTerminal) {
					t.Fatalf("exit %d: want errTerminal, got %v", tc.ExitCode, err)
				}
				if st.State != StateTerminal {
					t.Fatalf("exit %d: state=%q, want %q", tc.ExitCode, st.State, StateTerminal)
				}
			} else {
				if err != nil {
					t.Fatalf("exit %d: want nil error, got %v", tc.ExitCode, err)
				}
				if st.State == StateTerminal {
					t.Fatalf("exit %d: should not be terminal, but state is terminal", tc.ExitCode)
				}
			}

			if tc.FailureClass != "" && string(st.Reason) != tc.FailureClass {
				t.Fatalf("exit %d: reason=%q, want %q", tc.ExitCode, st.Reason, tc.FailureClass)
			}
		})
	}
}

func TestChildEnvAllowlistContract(t *testing.T) {
	var fx struct {
		Note      string   `json:"note"`
		Allowlist []string `json:"allowlist"`
		Canaries  []string `json:"canaries"`
		Injected  []string `json:"sdk_injected"`
		EnvMax    int      `json:"env_count_max"`
	}
	loadFixture(t, "child-env-allowlist.json", &fx)

	// Save and restore the environment so changes do not leak to other tests.
	saved := os.Environ()
	defer func() {
		os.Clearenv()
		for _, e := range saved {
			_ = os.Setenv(e[:strings.IndexByte(e, '=')], e[strings.IndexByte(e, '=')+1:])
		}
	}()

	os.Clearenv()
	for _, k := range fx.Allowlist {
		_ = os.Setenv(k, k+"-value")
	}
	for _, k := range fx.Canaries {
		_ = os.Setenv(k, k+"-should-not-leak")
	}
	// Add a few non-allowlisted vars to confirm they are stripped.
	_ = os.Setenv("IRRELEVANT_VAR", "must-not-appear")
	_ = os.Setenv("ANOTHER_UNRELATED_KEY", "also-must-not-appear")

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

	// All allowlisted vars that we set should be present.
	for _, k := range fx.Allowlist {
		if _, ok := envMap[k]; !ok {
			t.Errorf("allowlisted var %q missing from child env", k)
		}
	}

	// Canary secrets must be absent.
	for _, k := range fx.Canaries {
		if _, ok := envMap[k]; ok {
			t.Errorf("canary secret %q leaked to child env", k)
		}
	}

	// Non-allowlisted vars must be absent.
	if _, ok := envMap["IRRELEVANT_VAR"]; ok {
		t.Error("non-allowlisted IRRELEVANT_VAR leaked to child env")
	}
	if _, ok := envMap["ANOTHER_UNRELATED_KEY"]; ok {
		t.Error("non-allowlisted ANOTHER_UNRELATED_KEY leaked to child env")
	}

	// SDK injected vars must be present.
	for _, k := range fx.Injected {
		if _, ok := envMap[k]; !ok {
			t.Errorf("SDK injected var %q missing from child env", k)
		}
	}

	if n := len(cmd.Env); n > fx.EnvMax {
		t.Errorf("env entries: got %d, want at most %d", n, fx.EnvMax)
	}
}

func TestBackoffWaitCancellation(t *testing.T) {
	b := Backoff{Min: time.Hour, Max: time.Hour}

	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	start := time.Now()
	if err := b.Wait(ctx, 0, 0.5); !errors.Is(err, context.Canceled) {
		t.Fatalf("pre-cancelled: got %v, want context.Canceled", err)
	}

	ctx, cancel = context.WithCancel(context.Background())
	go func() { time.Sleep(20 * time.Millisecond); cancel() }()
	if err := b.Wait(ctx, 0, 0.5); !errors.Is(err, context.Canceled) {
		t.Fatalf("cancelled mid-wait: got %v, want context.Canceled", err)
	}
	if elapsed := time.Since(start); elapsed > 2*time.Second {
		t.Fatalf("cancellation took %v; Wait must return promptly", elapsed)
	}

	ctx, cancel = context.WithTimeout(context.Background(), 10*time.Millisecond)
	defer cancel()
	if err := b.Wait(ctx, 0, 0.5); !errors.Is(err, context.DeadlineExceeded) {
		t.Fatalf("deadline: got %v, want context.DeadlineExceeded", err)
	}

	short := Backoff{Min: time.Millisecond, Max: time.Millisecond}
	if err := short.Wait(context.Background(), 0, 0.5); err != nil {
		t.Fatalf("elapsed wait: %v", err)
	}
}
