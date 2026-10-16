package toolsync

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/haikeilabs/agentware/go/kei/evaluator"
)

const testToken = "kei_rt_unit-canary-0123456789abcdef"

var twoTools = map[string]any{
	"schema": "kei.tool-manifest/v2",
	"tools":  []any{map[string]any{"name": "a"}, map[string]any{"name": "b"}},
}

func noSleep(context.Context, time.Duration) error { return nil }

func TestConfigNeverPrintsTheToken(t *testing.T) {
	cfg := Config{ControlPlaneURL: "https://kei.example.test", Token: testToken, EgressProxy: "http://user:secret@egress.test:3128"}
	for _, s := range []string{cfg.String(), fmt.Sprintf("%v", cfg), fmt.Sprintf("%+v", cfg), fmt.Sprintf("%#v", cfg), fmt.Sprint(&cfg)} {
		if strings.Contains(s, testToken) || strings.Contains(s, "secret") || !strings.Contains(s, Redaction) {
			t.Errorf("repr %q", s)
		}
	}
}

func TestConfigFromProcessEnv(t *testing.T) {
	t.Setenv(ControlPlaneURLEnv, " https://kei.example.test ")
	t.Setenv(evaluator.RuntimeTokenEnv, testToken)
	t.Setenv(EgressProxyEnv, "")
	cfg, err := ConfigFromEnv(nil)
	if err != nil || cfg.ToolsURL() != "https://kei.example.test"+ToolsPath || cfg.Token != testToken {
		t.Errorf("got %v, %v", cfg, err)
	}
}

func TestManifestToolCount(t *testing.T) {
	for _, tc := range []struct {
		doc  map[string]any
		want int
	}{
		{twoTools, 2},
		{map[string]any{"tools": []any{}}, 0},
		{map[string]any{"tools": "x"}, 0},
		{map[string]any{}, 0},
	} {
		if got := ManifestToolCount(tc.doc); got != tc.want {
			t.Errorf("ManifestToolCount(%v) = %d, want %d", tc.doc, got, tc.want)
		}
	}
}

func TestBackoffDelay(t *testing.T) {
	for attempt, want := range map[int]time.Duration{1: time.Second, 2: 2 * time.Second, 5: MaxBackoff, 64: MaxBackoff} {
		if got := backoffDelay(time.Second, attempt); got != want {
			t.Errorf("attempt %d: %s, want %s", attempt, got, want)
		}
	}
	if got := backoffDelay(time.Minute, 1); got != MaxBackoff {
		t.Errorf("a first backoff above the cap: %s", got)
	}
}

func TestFailureNeverLogsOrReportsTheToken(t *testing.T) {
	// The token straddles the 300-byte quote limit: it must be redacted whole.
	body := strings.Repeat("x", ErrorBodyLimit-5) + testToken
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusUnauthorized)
		_, _ = w.Write([]byte(body))
	}))
	defer srv.Close()
	var logs bytes.Buffer
	cfg := Config{
		ControlPlaneURL: srv.URL, Token: testToken, Attempts: 2, Env: map[string]string{},
		Sleep: noSleep, Logger: slog.New(slog.NewTextHandler(&logs, nil)),
	}
	_, err := SyncDocument(context.Background(), twoTools, cfg, true)
	if err == nil || !errors.Is(err, ErrSyncFailed) {
		t.Fatalf("got %v", err)
	}
	for _, s := range []string{err.Error(), logs.String()} {
		if strings.Contains(s, testToken) || strings.Contains(s, testToken[:12]) {
			t.Errorf("token leaked: %q", s)
		}
	}
	if !strings.Contains(logs.String(), "retrying") {
		t.Errorf("no retry warning logged: %q", logs.String())
	}
}

func TestNetworkErrorNamesOnlyTheType(t *testing.T) {
	srv := httptest.NewServer(http.NotFoundHandler())
	srv.Close() // nothing listens any more
	cfg := Config{ControlPlaneURL: srv.URL, Token: testToken, Attempts: 1, Env: map[string]string{}, Sleep: noSleep}
	_, err := SyncDocument(context.Background(), twoTools, cfg, true)
	if err == nil || !strings.Contains(err.Error(), "failed after 1 attempt(s): network error: ") {
		t.Errorf("got %v", err)
	}
}

func TestRedirectsAreNotFollowed(t *testing.T) {
	var hits atomic.Int32
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		hits.Add(1)
		http.Redirect(w, r, "/elsewhere", http.StatusTemporaryRedirect)
	}))
	defer srv.Close()
	cfg := Config{ControlPlaneURL: srv.URL, Token: testToken, Attempts: 1, Env: map[string]string{}, Sleep: noSleep}
	if _, err := SyncDocument(context.Background(), twoTools, cfg, true); err == nil || !strings.Contains(err.Error(), "HTTP 307") || hits.Load() != 1 {
		t.Errorf("got %v after %d hits", err, hits.Load())
	}
}

func TestACancelledWaitStopsTheRetries(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusServiceUnavailable)
	}))
	defer srv.Close()
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	cfg := Config{ControlPlaneURL: srv.URL, Token: testToken, Attempts: 3, Env: map[string]string{}}
	_, err := SyncDocument(ctx, twoTools, cfg, true)
	if !errors.Is(err, context.Canceled) || !errors.Is(err, ErrSyncFailed) {
		t.Errorf("got %v", err)
	}
}

func TestTheEgressProxyCarriesTheRequest(t *testing.T) {
	var proxied atomic.Value
	proxy := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		proxied.Store(r.URL.String())
		w.WriteHeader(http.StatusOK)
	}))
	defer proxy.Close()
	cfg := Config{
		ControlPlaneURL: "http://control-plane.test", Token: testToken, EgressProxy: proxy.URL,
		Env: map[string]string{"HTTP_PROXY": "http://127.0.0.1:1"}, Attempts: 1, Sleep: noSleep,
	}
	res, err := SyncDocument(context.Background(), twoTools, cfg, true)
	if err != nil || !res.Sent {
		t.Fatalf("got %+v, %v", res, err)
	}
	if got, _ := proxied.Load().(string); got != "http://control-plane.test"+ToolsPath {
		t.Errorf("the proxy saw %q", got)
	}
}

func TestSyncTextTrimsAndSendsTheManifest(t *testing.T) {
	var got atomic.Value
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		buf := new(bytes.Buffer)
		_, _ = buf.ReadFrom(r.Body)
		got.Store(buf.String())
		_, _ = w.Write([]byte(`{"created":2}`))
	}))
	defer srv.Close()
	text := "\n  {\"tools\": [{\"name\": \"a\"}]}\n"
	cfg := Config{ControlPlaneURL: srv.URL + "/", Token: testToken, Env: map[string]string{}, Sleep: noSleep}
	res, err := Sync(context.Background(), []byte(text), cfg, true)
	if err != nil || res.ToolCount != 1 || res.Counts["created"] != float64(2) {
		t.Fatalf("got %+v, %v", res, err)
	}
	if got.Load() != strings.TrimSpace(text) {
		t.Errorf("body %q", got.Load())
	}
}
