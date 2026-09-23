package runtimelink

import (
	"errors"
	"fmt"
	"net/url"
	"os"
	"regexp"
	"strconv"
	"strings"
	"time"
)

// Environment variable names (spec §3.2). KEI_PROXY_* keep their legacy
// names because they are the runtime's contract.
const (
	EnvEnabled         = "KEI_RUNTIME_ENABLED"
	EnvToken           = "KEI_RUNTIME_TOKEN"
	EnvLegacyToken     = "KEI_HARNESS_TOKEN"
	EnvControlPlaneURL = "KEI_RUNTIME_CONTROL_PLANE_URL"
	EnvProxyPath       = "KEI_PROXY_PATH"
	EnvProxySHA        = "KEI_PROXY_SHA"
	EnvInterval        = "KEI_HEARTBEAT_INTERVAL"
	EnvTimeout         = "KEI_HEARTBEAT_TIMEOUT"
	EnvRestartMin      = "KEI_HEARTBEAT_RESTART_MIN"
	EnvRestartMax      = "KEI_HEARTBEAT_RESTART_MAX"
	EnvStableSeconds   = "KEI_HEARTBEAT_STABLE_SECONDS"
	EnvLogCount        = "KEI_HEARTBEAT_LOG_COUNT"
	EnvHarnessKind     = "KEI_HARNESS_KIND"
	EnvHarnessVersion  = "KEI_HARNESS_VERSION"
	EnvDeploymentEnv   = "KEI_DEPLOYMENT_ENV"
)

// DefaultBinaryPath is the runtime binary name; the distribution keeps the
// kei-proxy name even though the repository is kei-connector-runtime.
const DefaultBinaryPath = "kei-proxy"

// Bounds (spec §3.1). Interval is clamped; everything else is rejected when
// out of range so a typo fails closed instead of silently changing behavior.
const (
	MinInterval     = 15 * time.Second
	MaxInterval     = 300 * time.Second
	MaxGrace        = 300 * time.Second
	MinRestart      = 1 * time.Second
	MaxRestart      = 3600 * time.Second
	MinStableReset  = 1 * time.Second
	MaxStableReset  = 3600 * time.Second
	MinStopTimeout  = 1 * time.Second
	MaxStopTimeout  = 60 * time.Second
	MaxLogCount     = 100
	maxEnvIntDigits = 9
)

// Config error codes. They are shared with Python and TypeScript through
// testing/contracts/runtime-link/enums.json.
const (
	CodeInvalidValue    = "invalid_value"
	CodeOutOfRange      = "out_of_range"
	CodeBeatTimeout     = "beat_timeout"
	CodeHarnessKind     = "harness_kind"
	CodeEnvelope        = "envelope"
	CodeLegacyToken     = "legacy_token"
	CodeTokenMissing    = "token_missing"
	CodeControlPlaneURL = "control_plane_url"
	CodeBinary          = "binary"
)

// ErrConfig is the root of every configuration error; each code has its own
// sentinel wrapping it.
var ErrConfig = errors.New("runtimelink: invalid config")

var (
	ErrConfigInvalidValue    = fmt.Errorf("%w: %s", ErrConfig, CodeInvalidValue)
	ErrConfigOutOfRange      = fmt.Errorf("%w: %s", ErrConfig, CodeOutOfRange)
	ErrConfigBeatTimeout     = fmt.Errorf("%w: %s", ErrConfig, CodeBeatTimeout)
	ErrConfigHarnessKind     = fmt.Errorf("%w: %s", ErrConfig, CodeHarnessKind)
	ErrConfigEnvelope        = fmt.Errorf("%w: %s", ErrConfig, CodeEnvelope)
	ErrConfigLegacyToken     = fmt.Errorf("%w: %s", ErrConfig, CodeLegacyToken)
	ErrConfigTokenMissing    = fmt.Errorf("%w: %s", ErrConfig, CodeTokenMissing)
	ErrConfigControlPlaneURL = fmt.Errorf("%w: %s", ErrConfig, CodeControlPlaneURL)
	ErrConfigBinary          = fmt.Errorf("%w: %s", ErrConfig, CodeBinary)
)

var codeSentinels = map[string]error{
	CodeInvalidValue:    ErrConfigInvalidValue,
	CodeOutOfRange:      ErrConfigOutOfRange,
	CodeBeatTimeout:     ErrConfigBeatTimeout,
	CodeHarnessKind:     ErrConfigHarnessKind,
	CodeEnvelope:        ErrConfigEnvelope,
	CodeLegacyToken:     ErrConfigLegacyToken,
	CodeTokenMissing:    ErrConfigTokenMissing,
	CodeControlPlaneURL: ErrConfigControlPlaneURL,
	CodeBinary:          ErrConfigBinary,
}

// ConfigError names the offending field and a stable code. It never carries
// the offending value, so a misplaced secret cannot leak through an error.
type ConfigError struct {
	Code  string
	Field string
}

func (e *ConfigError) Error() string {
	return fmt.Sprintf("runtimelink: invalid config: %s: %s", e.Field, e.Code)
}

func (e *ConfigError) Unwrap() error { return codeSentinels[e.Code] }

// ErrorCode returns the contract code of a configuration error, or "".
func ErrorCode(err error) string {
	if ce, ok := errors.AsType[*ConfigError](err); ok {
		return ce.Code
	}
	return ""
}

func configErr(code, field string) error { return &ConfigError{Code: code, Field: field} }

// BinaryRef names the runtime binary and its expected SHA-256 (lowercase hex).
type BinaryRef struct {
	Path   string
	SHA256 string
}

// SecretSource names where the runtime token lives. It holds the env var
// name only; the token value is never read into Config.
type SecretSource struct {
	Env string
}

// Backoff configures child restart backoff with full jitter.
type Backoff struct {
	Min         time.Duration
	Max         time.Duration
	StableReset time.Duration
}

// HarnessEnvelope is declared, non-authoritative harness metadata. It labels
// heartbeats and lifecycle events and is never used for authorization.
type HarnessEnvelope struct {
	Kind          HarnessKind
	Version       string
	DeploymentEnv string
}

// Config configures a RuntimeLink. Start from DefaultConfig or
// ConfigFromEnv; NormalizeConfig validates and clamps it.
type Config struct {
	Enabled         bool
	Binary          BinaryRef
	ControlPlaneURL string
	Token           SecretSource
	Interval        time.Duration
	BeatTimeout     time.Duration
	Grace           time.Duration
	Restart         Backoff
	StopTimeout     time.Duration
	LogCount        int
	Harness         HarnessEnvelope
}

// DefaultConfig returns the spec defaults with the link disabled.
func DefaultConfig() Config {
	return Config{
		Binary:      BinaryRef{Path: DefaultBinaryPath},
		Token:       SecretSource{Env: EnvToken},
		Interval:    60 * time.Second,
		BeatTimeout: 10 * time.Second,
		Grace:       15 * time.Second,
		Restart:     Backoff{Min: 1 * time.Second, Max: 300 * time.Second, StableReset: 300 * time.Second},
		StopTimeout: 10 * time.Second,
		LogCount:    3,
	}
}

var (
	versionPattern = regexp.MustCompile(`^[A-Za-z0-9._+-]{1,64}$`)
	envNamePattern = regexp.MustCompile(`^[A-Za-z0-9._-]{1,32}$`)
	sha256Pattern  = regexp.MustCompile(`^[0-9a-f]{64}$`)
)

// NormalizeConfig clamps Interval to [MinInterval, MaxInterval] and validates
// every other field, returning a ConfigError on the first violation. A
// disabled link (local-only mode) is valid, but its declared envelope and
// timings are still checked so typos fail loudly.
func NormalizeConfig(cfg Config) (Config, error) {
	if cfg.Harness.Kind != "" && !cfg.Harness.Kind.Valid() {
		return Config{}, configErr(CodeHarnessKind, "harness.kind")
	}
	if cfg.Enabled && cfg.Harness.Kind == "" {
		return Config{}, configErr(CodeHarnessKind, "harness.kind")
	}
	if cfg.Harness.Version != "" && !versionPattern.MatchString(cfg.Harness.Version) {
		return Config{}, configErr(CodeEnvelope, "harness.version")
	}
	if cfg.Harness.DeploymentEnv != "" && !envNamePattern.MatchString(cfg.Harness.DeploymentEnv) {
		return Config{}, configErr(CodeEnvelope, "harness.deployment_env")
	}

	if cfg.Enabled {
		switch cfg.Token.Env {
		case EnvToken:
		case EnvLegacyToken:
			return Config{}, configErr(CodeLegacyToken, "token")
		default:
			return Config{}, configErr(CodeInvalidValue, "token")
		}
		if err := validateControlPlaneURL(cfg.ControlPlaneURL); err != nil {
			return Config{}, err
		}
		if cfg.Binary.Path == "" || strings.ContainsRune(cfg.Binary.Path, 0) {
			return Config{}, configErr(CodeBinary, "binary.path")
		}
		if cfg.Binary.SHA256 == "" && cfg.Harness.DeploymentEnv == "prod" {
			return Config{}, configErr(CodeBinary, "binary.sha256")
		}
	}
	if cfg.Binary.SHA256 != "" && !sha256Pattern.MatchString(cfg.Binary.SHA256) {
		return Config{}, configErr(CodeBinary, "binary.sha256")
	}

	cfg.Interval = min(max(cfg.Interval, MinInterval), MaxInterval)
	if cfg.BeatTimeout <= 0 || 2*cfg.BeatTimeout >= cfg.Interval {
		return Config{}, configErr(CodeBeatTimeout, "beat_timeout")
	}
	if cfg.Grace < 0 || cfg.Grace > MaxGrace {
		return Config{}, configErr(CodeOutOfRange, "grace")
	}
	r := cfg.Restart
	if r.Min < MinRestart || r.Max > MaxRestart || r.Min > r.Max {
		return Config{}, configErr(CodeOutOfRange, "restart")
	}
	if r.StableReset < MinStableReset || r.StableReset > MaxStableReset {
		return Config{}, configErr(CodeOutOfRange, "restart.stable_reset")
	}
	if cfg.StopTimeout < MinStopTimeout || cfg.StopTimeout > MaxStopTimeout {
		return Config{}, configErr(CodeOutOfRange, "stop_timeout")
	}
	if cfg.LogCount < 0 || cfg.LogCount > MaxLogCount {
		return Config{}, configErr(CodeOutOfRange, "log_count")
	}
	return cfg, nil
}

func validateControlPlaneURL(raw string) error {
	u, err := url.Parse(raw)
	if err != nil || (u.Scheme != "http" && u.Scheme != "https") || u.Host == "" || u.User != nil {
		return configErr(CodeControlPlaneURL, "control_plane_url")
	}
	return nil
}

// LookupFunc reads one environment variable, like os.LookupEnv.
type LookupFunc func(key string) (string, bool)

// ConfigFromEnv builds a normalized Config from the spec §3.2 variables.
// Non-empty fields of harness override the KEI_HARNESS_* variables. The
// runtime token is only checked for presence; its value is never retained.
// A nil lookup reads the process environment.
func ConfigFromEnv(lookup LookupFunc, harness HarnessEnvelope) (Config, error) {
	if lookup == nil {
		lookup = os.LookupEnv
	}
	get := func(key string) string {
		v, _ := lookup(key)
		return strings.TrimSpace(v)
	}

	cfg := DefaultConfig()
	durations := []struct {
		env string
		dst *time.Duration
	}{
		{EnvInterval, &cfg.Interval},
		{EnvTimeout, &cfg.BeatTimeout},
		{EnvRestartMin, &cfg.Restart.Min},
		{EnvRestartMax, &cfg.Restart.Max},
		{EnvStableSeconds, &cfg.Restart.StableReset},
	}
	for _, d := range durations {
		if raw := get(d.env); raw != "" {
			n, err := parseEnvInt(raw)
			if err != nil {
				return Config{}, configErr(CodeInvalidValue, d.env)
			}
			*d.dst = time.Duration(n) * time.Second
		}
	}
	if raw := get(EnvLogCount); raw != "" {
		n, err := parseEnvInt(raw)
		if err != nil {
			return Config{}, configErr(CodeInvalidValue, EnvLogCount)
		}
		cfg.LogCount = n
	}

	hasToken := get(EnvToken) != ""
	if !hasToken && get(EnvLegacyToken) != "" {
		return Config{}, configErr(CodeLegacyToken, EnvLegacyToken)
	}
	cfg.Enabled = hasToken
	if raw := get(EnvEnabled); raw != "" {
		enabled, err := parseEnvBool(raw)
		if err != nil {
			return Config{}, configErr(CodeInvalidValue, EnvEnabled)
		}
		if enabled && !hasToken {
			return Config{}, configErr(CodeTokenMissing, EnvToken)
		}
		cfg.Enabled = enabled
	}

	cfg.ControlPlaneURL = get(EnvControlPlaneURL)
	if p := get(EnvProxyPath); p != "" {
		cfg.Binary.Path = p
	}
	cfg.Binary.SHA256 = get(EnvProxySHA)
	cfg.Harness = HarnessEnvelope{
		Kind:          HarnessKind(firstNonEmpty(string(harness.Kind), get(EnvHarnessKind))),
		Version:       firstNonEmpty(harness.Version, get(EnvHarnessVersion)),
		DeploymentEnv: firstNonEmpty(harness.DeploymentEnv, get(EnvDeploymentEnv)),
	}
	return NormalizeConfig(cfg)
}

func parseEnvInt(raw string) (int, error) {
	if len(raw) > maxEnvIntDigits {
		return 0, ErrConfigInvalidValue
	}
	for _, r := range raw {
		if r < '0' || r > '9' {
			return 0, ErrConfigInvalidValue
		}
	}
	n, err := strconv.Atoi(raw)
	if err != nil {
		return 0, fmt.Errorf("parse integer: %w", err)
	}
	return n, nil
}

func parseEnvBool(raw string) (bool, error) {
	switch strings.ToLower(raw) {
	case "true", "1":
		return true, nil
	case "false", "0":
		return false, nil
	}
	return false, ErrConfigInvalidValue
}

func firstNonEmpty(a, b string) string {
	if a != "" {
		return a
	}
	return b
}
