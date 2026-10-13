// Package toolsync registers a harness's governed tool manifest with the Kei
// control plane.
//
// It is the Go port of pedro_agentware.kei.tool_sync; all SDKs replay the
// shared fixtures in testing/contracts/runtime-socket/tool-sync-cases.json.
// Kei's policy bundle only allows tools registered in the installation's tool
// catalog, so a harness registers its tools before it serves:
// PUT $KEI_RUNTIME_CONTROL_PLANE_URL/api/v1/runtime/tools with the
// application/vnd.kei.tool-manifest.v2+json document, authenticated with
// KEI_RUNTIME_TOKEN, the two variables kei-proxy itself reads.
//
//   - The PUT replaces the installation's tools (it is idempotent: a restart
//     sends the same manifest and the catalog converges on it). So a manifest
//     with no tools is never sent: it would unregister every tool. When tools
//     are expected that is an error; when they are not, the sync is skipped.
//   - A network failure or non-2xx answer is retried with exponential backoff;
//     once the attempts run out the error wraps ErrSyncFailed, and the harness
//     must refuse to serve (fail closed).
//   - The control plane is reached the way kei-proxy reaches it: through
//     KEI_PROXY_EGRESS_PROXY unless KEI_PROXY_NO_PROXY bypasses the host. With
//     an egress proxy configured, ambient HTTP(S)_PROXY variables are ignored;
//     without one they apply, bypassed by NO_PROXY. The proxy is set on this
//     client only, never exported.
//   - Token rule: the token goes in the Authorization header and nowhere else:
//     not in a log line, an error message or Config.String().
package toolsync

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"net/url"
	"os"
	"sort"
	"strings"
	"time"
	"unicode/utf8"

	"github.com/soypete/pedro-agentware/go/kei/evaluator"
)

// Names and values shared with the Python and TypeScript ports.
const (
	ToolsPath          = "/api/v1/runtime/tools"
	ManifestMediaType  = "application/vnd.kei.tool-manifest.v2+json"
	ControlPlaneURLEnv = "KEI_RUNTIME_CONTROL_PLANE_URL"
	EgressProxyEnv     = "KEI_PROXY_EGRESS_PROXY"
	NoProxyEnv         = "KEI_PROXY_NO_PROXY"

	DefaultAttempts = 5
	DefaultBackoff  = time.Second
	MaxBackoff      = 16 * time.Second
	DefaultTimeout  = 30 * time.Second

	// ErrorBodyLimit bounds how much of a refusal body an error quotes.
	ErrorBodyLimit = 300
	// Redaction replaces the token wherever it would appear in an error.
	Redaction = "[redacted]"
)

// maxCountsBytes bounds the success body read for counts.
const maxCountsBytes = 1 << 20

// Sentinel errors. Every error this package returns wraps ErrSyncFailed.
var (
	// ErrSyncFailed means the manifest was not registered with Kei. The
	// harness must not serve.
	ErrSyncFailed = errors.New("tool manifest sync failed")
	// ErrInvalidConfig is a missing or malformed control-plane setting.
	ErrInvalidConfig = errors.New("invalid tool sync config")
	// ErrInvalidManifest is a manifest that is not a JSON object.
	ErrInvalidManifest = errors.New("invalid tool manifest")
	// ErrEmptyManifest is a manifest with no tools while tools are expected.
	ErrEmptyManifest = errors.New("empty tool manifest")
)

// syncError carries a message built by this package (never the token) and
// matches its kind and ErrSyncFailed.
type syncError struct {
	kind error
	msg  string
}

func (e *syncError) Error() string   { return e.msg }
func (e *syncError) Unwrap() []error { return []error{e.kind, ErrSyncFailed} }

func newError(kind error, format string, args ...any) error {
	return &syncError{kind: kind, msg: fmt.Sprintf(format, args...)}
}

// Config says where and how to register the manifest.
type Config struct {
	ControlPlaneURL string
	Token           string
	// EgressProxy, when set, is the only proxy considered (KEI_PROXY_*).
	EgressProxy string
	// NoProxy lists hosts that bypass EgressProxy.
	NoProxy string
	// Attempts is the number of PUTs before giving up. Default DefaultAttempts.
	Attempts int
	// Backoff is the first retry delay, doubled per attempt up to MaxBackoff.
	// Default DefaultBackoff.
	Backoff time.Duration
	// Timeout bounds one PUT. Default DefaultTimeout.
	Timeout time.Duration
	// Env is where the ambient HTTP(S)_PROXY / NO_PROXY variables are read
	// when EgressProxy is unset. Nil means the process environment.
	Env map[string]string
	// Sleep waits between attempts. Default: a timer that honours ctx.
	Sleep func(ctx context.Context, d time.Duration) error
	// Logger receives one info line on success and a warning per retry.
	// Default slog.Default().
	Logger *slog.Logger
}

// ConfigFromEnv reads the variables kei-proxy reads. A nil env means the
// process environment. Errors wrap ErrInvalidConfig.
func ConfigFromEnv(env map[string]string) (Config, error) {
	get := func(k string) string {
		if env != nil {
			return strings.TrimSpace(env[k])
		}
		return strings.TrimSpace(os.Getenv(k))
	}
	rawURL, token := get(ControlPlaneURLEnv), get(evaluator.RuntimeTokenEnv)
	if rawURL == "" {
		return Config{}, newError(ErrInvalidConfig, "%s is not set", ControlPlaneURLEnv)
	}
	if token == "" {
		return Config{}, newError(ErrInvalidConfig, "%s is not set", evaluator.RuntimeTokenEnv)
	}
	if u, err := url.Parse(rawURL); err != nil || (u.Scheme != "http" && u.Scheme != "https") || u.Host == "" {
		return Config{}, newError(ErrInvalidConfig, "%s is not an http(s) URL", ControlPlaneURLEnv)
	}
	return Config{
		ControlPlaneURL: rawURL,
		Token:           token,
		EgressProxy:     get(EgressProxyEnv),
		NoProxy:         get(NoProxyEnv),
		Env:             env,
	}, nil
}

// String shows the config with the token and any proxy password redacted.
func (c Config) String() string {
	token := ""
	if c.Token != "" {
		token = Redaction
	}
	return fmt.Sprintf("toolsync.Config{ControlPlaneURL: %q, Token: %q, EgressProxy: %q, NoProxy: %q, Attempts: %d, Backoff: %s, Timeout: %s}",
		redactURL(c.ControlPlaneURL), token, redactURL(c.EgressProxy), c.NoProxy, c.Attempts, c.Backoff, c.Timeout)
}

// GoString is String: the token never prints.
func (c Config) GoString() string { return c.String() }

func redactURL(raw string) string {
	if u, err := url.Parse(raw); err == nil && u.User != nil {
		return u.Redacted()
	}
	return raw
}

// ToolsURL is the PUT target.
func (c Config) ToolsURL() string {
	return strings.TrimRight(c.ControlPlaneURL, "/") + ToolsPath
}

func (c Config) lookup(k string) string {
	if c.Env != nil {
		return c.Env[k]
	}
	return os.Getenv(k)
}

// ProxyURL returns the proxy a request to target goes through, or ok=false
// for a direct connection. EgressProxy, when set, decides alone: NoProxy
// bypasses it and the ambient variables are ignored (a bypass goes direct; it
// does not fall back to them). Without it, HTTPS_PROXY / https_proxy (or
// HTTP_PROXY / http_proxy for an http target) apply, bypassed by NO_PROXY /
// no_proxy.
func (c Config) ProxyURL(target string) (proxy string, ok bool) {
	u, err := url.Parse(target)
	if err != nil {
		return "", false
	}
	host := u.Hostname()
	if c.EgressProxy != "" {
		if bypass(host, c.NoProxy) {
			return "", false
		}
		return c.EgressProxy, true
	}
	keys := []string{"HTTPS_PROXY", "https_proxy"}
	if strings.EqualFold(u.Scheme, "http") {
		keys = []string{"HTTP_PROXY", "http_proxy"}
	}
	for _, k := range keys {
		if p := c.lookup(k); p != "" {
			noProxy := c.lookup("NO_PROXY")
			if noProxy == "" {
				noProxy = c.lookup("no_proxy")
			}
			if bypass(host, noProxy) {
				return "", false
			}
			return p, true
		}
	}
	return "", false
}

// bypass reports whether host matches a no-proxy list: comma-separated
// entries, case-insensitive, matching the host exactly or as a domain suffix
// (a leading "." is optional); "*" matches every host.
func bypass(host, noProxy string) bool {
	host = strings.ToLower(strings.TrimSuffix(host, "."))
	for _, entry := range strings.Split(noProxy, ",") {
		entry = strings.ToLower(strings.TrimSpace(entry))
		if entry == "*" {
			return true
		}
		entry = strings.TrimLeft(entry, ".")
		if entry == "" {
			continue
		}
		if host == entry || strings.HasSuffix(host, "."+entry) {
			return true
		}
	}
	return false
}

// Result is what a sync did: whether it sent, how many tools, and the counts
// Kei answered.
type Result struct {
	Sent      bool
	ToolCount int
	Counts    map[string]any
}

// ManifestToolCount is the number of entries in the manifest's "tools" list
// (0 when absent or not a list).
func ManifestToolCount(manifest map[string]any) int {
	tools, _ := manifest["tools"].([]any)
	return len(tools)
}

// Sync registers manifest (tool manifest v2 JSON text) with Kei in replace
// mode. An empty manifest is never sent: with expectTools it is an
// ErrEmptyManifest error, otherwise Result{Sent: false}.
func Sync(ctx context.Context, manifest []byte, cfg Config, expectTools bool) (Result, error) {
	var parsed any
	if err := json.Unmarshal(manifest, &parsed); err != nil {
		return Result{}, newError(ErrInvalidManifest, "the tool manifest is not JSON")
	}
	doc, ok := parsed.(map[string]any)
	if !ok {
		return Result{}, newError(ErrInvalidManifest, "the tool manifest is not a JSON object")
	}
	return syncManifest(ctx, doc, bytes.TrimSpace(manifest), cfg, expectTools)
}

// SyncDocument is Sync for a decoded manifest.
func SyncDocument(ctx context.Context, manifest map[string]any, cfg Config, expectTools bool) (Result, error) {
	if manifest == nil {
		manifest = map[string]any{}
	}
	body, err := json.Marshal(manifest)
	if err != nil {
		return Result{}, newError(ErrInvalidManifest, "the tool manifest is not JSON: %v", err)
	}
	return syncManifest(ctx, manifest, body, cfg, expectTools)
}

func syncManifest(ctx context.Context, doc map[string]any, body []byte, cfg Config, expectTools bool) (Result, error) {
	logger := cfg.Logger
	if logger == nil {
		logger = slog.Default()
	}
	count := ManifestToolCount(doc)
	if count == 0 {
		if expectTools {
			return Result{}, newError(ErrEmptyManifest,
				"refusing to PUT an empty manifest to %s: it would unregister every tool", ToolsPath)
		}
		logger.Info("tool sync: no tools configured; nothing sent", "path", ToolsPath)
		return Result{Counts: map[string]any{}}, nil
	}

	attempts := cfg.Attempts
	if attempts <= 0 {
		attempts = DefaultAttempts
	}
	backoff := cfg.Backoff
	if backoff <= 0 {
		backoff = DefaultBackoff
	}
	sleep := cfg.Sleep
	if sleep == nil {
		sleep = sleepContext
	}
	client := newHTTPClient(cfg)
	defer client.CloseIdleConnections()

	last := "no attempt made"
	for attempt := 1; attempt <= attempts; attempt++ {
		counts, failure := put(ctx, client, cfg, body)
		if failure == "" {
			logger.Info("tool sync: registered tools with Kei", "tools", count, "path", ToolsPath, "counts", formatCounts(counts))
			return Result{Sent: true, ToolCount: count, Counts: counts}, nil
		}
		last = scrub(failure, cfg.Token)
		if attempt < attempts {
			delay := backoffDelay(backoff, attempt)
			logger.Warn("tool sync attempt failed; retrying",
				"attempt", attempt, "attempts", attempts, "error", last, "retry_in", delay)
			if err := sleep(ctx, delay); err != nil {
				return Result{}, fmt.Errorf("%w: interrupted after %d attempt(s): %w", ErrSyncFailed, attempt, err)
			}
		}
	}
	return Result{}, newError(ErrSyncFailed, "PUT %s failed after %d attempt(s): %s", ToolsPath, attempts, last)
}

// put sends one PUT and returns the counts, or a failure description ("" on
// success) that may still contain the token.
func put(ctx context.Context, client *http.Client, cfg Config, body []byte) (map[string]any, string) {
	req, err := http.NewRequestWithContext(ctx, http.MethodPut, cfg.ToolsURL(), bytes.NewReader(body))
	if err != nil {
		return nil, fmt.Sprintf("invalid request: %T", err)
	}
	req.Header.Set("Authorization", "Bearer "+cfg.Token)
	req.Header.Set("Content-Type", ManifestMediaType)
	req.Header.Set("Accept", ManifestMediaType)
	res, err := client.Do(req)
	if err != nil {
		var ue *url.Error
		if errors.As(err, &ue) {
			err = ue.Err
		}
		return nil, fmt.Sprintf("network error: %T", err)
	}
	defer func() { _ = res.Body.Close() }()
	if res.StatusCode < 200 || res.StatusCode > 299 {
		// Read past the limit by the token's length so a token straddling the
		// cut is redacted whole before the quote is truncated.
		quote, _ := io.ReadAll(io.LimitReader(res.Body, int64(ErrorBodyLimit+len(cfg.Token))))
		text := scrub(string(quote), cfg.Token)
		if len(text) > ErrorBodyLimit {
			text = text[:ErrorBodyLimit]
		}
		return nil, fmt.Sprintf("HTTP %d: %s", res.StatusCode, strings.ToValidUTF8(text, string(utf8.RuneError)))
	}
	data, _ := io.ReadAll(io.LimitReader(res.Body, maxCountsBytes))
	var counts map[string]any
	if json.Unmarshal(data, &counts) != nil || counts == nil {
		counts = map[string]any{}
	}
	return counts, ""
}

// newHTTPClient never follows redirects and routes through cfg.ProxyURL.
func newHTTPClient(cfg Config) *http.Client {
	transport := http.DefaultTransport.(*http.Transport).Clone()
	transport.Proxy = func(req *http.Request) (*url.URL, error) {
		raw, ok := cfg.ProxyURL(req.URL.String())
		if !ok {
			return nil, nil
		}
		if !strings.Contains(raw, "://") {
			raw = "http://" + raw
		}
		u, err := url.Parse(raw)
		if err != nil {
			return nil, errors.New("the proxy URL is invalid")
		}
		return u, nil
	}
	timeout := cfg.Timeout
	if timeout <= 0 {
		timeout = DefaultTimeout
	}
	return &http.Client{
		Transport:     transport,
		Timeout:       timeout,
		CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse },
	}
}

// backoffDelay is min(backoff * 2^(attempt-1), MaxBackoff).
func backoffDelay(backoff time.Duration, attempt int) time.Duration {
	d := backoff
	for i := 1; i < attempt && d < MaxBackoff; i++ {
		d *= 2
	}
	return min(d, MaxBackoff)
}

func sleepContext(ctx context.Context, d time.Duration) error {
	t := time.NewTimer(d)
	defer t.Stop()
	select {
	case <-t.C:
		return nil
	case <-ctx.Done():
		return ctx.Err()
	}
}

func scrub(text, token string) string {
	if token == "" {
		return text
	}
	return strings.ReplaceAll(text, token, Redaction)
}

func formatCounts(counts map[string]any) string {
	if len(counts) == 0 {
		return "no counts returned"
	}
	keys := make([]string, 0, len(counts))
	for k := range counts {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	parts := make([]string, len(keys))
	for i, k := range keys {
		parts[i] = fmt.Sprintf("%s=%v", k, counts[k])
	}
	return strings.Join(parts, ", ")
}
