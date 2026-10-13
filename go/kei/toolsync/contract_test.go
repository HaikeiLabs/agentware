package toolsync

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"io/fs"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/soypete/pedro-agentware/go/kei/evaluator"
)

// fixtureDir holds the cross-language fixtures. They live at the repository
// root, outside the go/ module, so they are not part of the module zip.
const fixtureDir = "../../../testing/contracts/runtime-socket"

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

func TestConstantsMatchContract(t *testing.T) {
	var c struct {
		ToolSync struct {
			Path               string `json:"path"`
			MediaType          string `json:"media_type"`
			ControlPlaneURLEnv string `json:"control_plane_url_env"`
			TokenEnv           string `json:"token_env"`
			EgressProxyEnv     string `json:"egress_proxy_env"`
			NoProxyEnv         string `json:"no_proxy_env"`
			DefaultAttempts    int    `json:"default_attempts"`
			DefaultBackoffMS   int64  `json:"default_backoff_ms"`
			MaxBackoffMS       int64  `json:"max_backoff_ms"`
			DefaultTimeoutMS   int64  `json:"default_timeout_ms"`
			ErrorBodyLimit     int    `json:"error_body_limit"`
			Redaction          string `json:"redaction"`
		} `json:"tool_sync"`
	}
	loadFixture(t, "constants.json", &c)
	ms := func(v int64) time.Duration { return time.Duration(v) * time.Millisecond }
	ts := c.ToolSync
	checks := []struct {
		name      string
		got, want any
	}{
		{"path", ToolsPath, ts.Path},
		{"media_type", ManifestMediaType, ts.MediaType},
		{"control_plane_url_env", ControlPlaneURLEnv, ts.ControlPlaneURLEnv},
		{"token_env", evaluator.RuntimeTokenEnv, ts.TokenEnv},
		{"egress_proxy_env", EgressProxyEnv, ts.EgressProxyEnv},
		{"no_proxy_env", NoProxyEnv, ts.NoProxyEnv},
		{"default_attempts", DefaultAttempts, ts.DefaultAttempts},
		{"default_backoff", DefaultBackoff, ms(ts.DefaultBackoffMS)},
		{"max_backoff", MaxBackoff, ms(ts.MaxBackoffMS)},
		{"default_timeout", DefaultTimeout, ms(ts.DefaultTimeoutMS)},
		{"error_body_limit", ErrorBodyLimit, ts.ErrorBodyLimit},
		{"redaction", Redaction, ts.Redaction},
	}
	for _, ch := range checks {
		if !reflect.DeepEqual(ch.got, ch.want) {
			t.Errorf("%s = %v, contract says %v", ch.name, ch.got, ch.want)
		}
	}
}

type syncFixture struct {
	ContractVersion int    `json:"contract_version"`
	Token           string `json:"token"`
	ExpectedRequest struct {
		Method  string            `json:"method"`
		Path    string            `json:"path"`
		Headers map[string]string `json:"headers"`
	} `json:"expected_request"`
	SyncCases []struct {
		Name         string          `json:"name"`
		Manifest     json.RawMessage `json:"manifest"`
		ManifestText *string         `json:"manifest_text"`
		ExpectTools  bool            `json:"expect_tools"`
		Attempts     int             `json:"attempts"`
		BackoffMS    int64           `json:"backoff_ms"`
		Responses    []struct {
			Status       int             `json:"status"`
			Body         json.RawMessage `json:"body"`
			Raw          *string         `json:"raw"`
			NetworkError bool            `json:"network_error"`
		} `json:"responses"`
		Expect struct {
			Sent              *bool          `json:"sent"`
			ToolCount         int            `json:"tool_count"`
			Counts            map[string]any `json:"counts"`
			ErrorContains     string         `json:"error_contains"`
			ErrorContainsAlso string         `json:"error_contains_also"`
		} `json:"expect"`
		ExpectRequests int     `json:"expect_requests"`
		ExpectSleepsMS []int64 `json:"expect_sleeps_ms"`
	} `json:"sync_cases"`
	ConfigCases []struct {
		Name   string            `json:"name"`
		Env    map[string]string `json:"env"`
		Expect struct {
			ToolsURL      string `json:"tools_url"`
			EgressProxy   string `json:"egress_proxy"`
			NoProxy       string `json:"no_proxy"`
			ErrorContains string `json:"error_contains"`
		} `json:"expect"`
	} `json:"config_cases"`
	ProxyCases []struct {
		Name        string            `json:"name"`
		Layer       string            `json:"layer"`
		URL         string            `json:"url"`
		Env         map[string]string `json:"env"`
		ExpectProxy *string           `json:"expect_proxy"`
	} `json:"proxy_cases"`
}

type seenRequest struct {
	method, path string
	header       http.Header
	body         []byte
}

func TestSyncContractCases(t *testing.T) {
	var f syncFixture
	loadFixture(t, "tool-sync-cases.json", &f)
	if f.ContractVersion != 1 || len(f.SyncCases) == 0 {
		t.Fatalf("unexpected fixture: version %d", f.ContractVersion)
	}
	for _, tc := range f.SyncCases {
		t.Run(tc.Name, func(t *testing.T) {
			var (
				mu   sync.Mutex
				seen []seenRequest
			)
			srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				body, _ := io.ReadAll(r.Body)
				mu.Lock()
				i := len(seen)
				seen = append(seen, seenRequest{r.Method, r.URL.Path, r.Header.Clone(), body})
				mu.Unlock()
				if i >= len(tc.Responses) {
					w.WriteHeader(http.StatusTeapot)
					return
				}
				resp := tc.Responses[i]
				switch {
				case resp.NetworkError:
					conn, _, err := http.NewResponseController(w).Hijack()
					if err == nil {
						_ = conn.Close()
					}
				case resp.Raw != nil:
					w.WriteHeader(resp.Status)
					_, _ = io.WriteString(w, *resp.Raw)
				case len(resp.Body) > 0:
					w.Header().Set("Content-Type", "application/json")
					w.WriteHeader(resp.Status)
					_, _ = w.Write(resp.Body)
				default:
					w.WriteHeader(resp.Status)
				}
			}))
			defer srv.Close()

			var sleeps []int64
			cfg := Config{
				ControlPlaneURL: srv.URL,
				Token:           f.Token,
				Attempts:        tc.Attempts,
				Backoff:         time.Duration(tc.BackoffMS) * time.Millisecond,
				Env:             map[string]string{},
				Sleep: func(_ context.Context, d time.Duration) error {
					sleeps = append(sleeps, d.Milliseconds())
					return nil
				},
			}
			var (
				result   Result
				err      error
				manifest any
			)
			if tc.ManifestText != nil {
				_ = json.Unmarshal([]byte(*tc.ManifestText), &manifest)
				result, err = Sync(context.Background(), []byte(*tc.ManifestText), cfg, tc.ExpectTools)
			} else {
				var doc map[string]any
				if e := json.Unmarshal(tc.Manifest, &doc); e != nil {
					t.Fatalf("manifest: %v", e)
				}
				manifest = doc
				result, err = SyncDocument(context.Background(), doc, cfg, tc.ExpectTools)
			}

			if tc.Expect.ErrorContains != "" {
				if err == nil {
					t.Fatalf("want an error containing %q, got %+v", tc.Expect.ErrorContains, result)
				}
				msg := err.Error()
				if !strings.Contains(msg, tc.Expect.ErrorContains) || !strings.Contains(msg, tc.Expect.ErrorContainsAlso) {
					t.Errorf("error %q lacks %q / %q", msg, tc.Expect.ErrorContains, tc.Expect.ErrorContainsAlso)
				}
				if strings.Contains(msg, f.Token) {
					t.Errorf("error leaks the token: %q", msg)
				}
				if !errors.Is(err, ErrSyncFailed) {
					t.Errorf("error does not wrap ErrSyncFailed: %v", err)
				}
			} else {
				if err != nil {
					t.Fatalf("unexpected error: %v", err)
				}
				want := Result{Sent: *tc.Expect.Sent, ToolCount: tc.Expect.ToolCount, Counts: tc.Expect.Counts}
				got := result
				got.Counts = roundTrip(t, got.Counts)
				if !reflect.DeepEqual(got, want) {
					t.Errorf("result %+v, want %+v", got, want)
				}
			}

			if len(seen) != tc.ExpectRequests {
				t.Fatalf("%d requests, want %d", len(seen), tc.ExpectRequests)
			}
			for i, r := range seen {
				if r.method != f.ExpectedRequest.Method || r.path != f.ExpectedRequest.Path {
					t.Errorf("request %d: %s %s", i, r.method, r.path)
				}
				for k, v := range f.ExpectedRequest.Headers {
					if got := r.header.Get(k); got != v {
						t.Errorf("request %d header %s = %q, want %q", i, k, got, v)
					}
				}
				var body any
				if err := json.Unmarshal(r.body, &body); err != nil || !reflect.DeepEqual(body, manifest) {
					t.Errorf("request %d body %s, want the manifest", i, r.body)
				}
			}
			if want := tc.ExpectSleepsMS; (len(sleeps) != 0 || len(want) != 0) && !reflect.DeepEqual(sleeps, want) {
				t.Errorf("sleeps %v ms, want %v", sleeps, want)
			}
		})
	}
}

func roundTrip(t *testing.T, m map[string]any) map[string]any {
	t.Helper()
	data, err := json.Marshal(m)
	if err != nil {
		t.Fatal(err)
	}
	var out map[string]any
	if err := json.Unmarshal(data, &out); err != nil {
		t.Fatal(err)
	}
	return out
}

func TestConfigContractCases(t *testing.T) {
	var f syncFixture
	loadFixture(t, "tool-sync-cases.json", &f)
	for _, tc := range f.ConfigCases {
		t.Run(tc.Name, func(t *testing.T) {
			env := tc.Env
			if env == nil {
				env = map[string]string{}
			}
			cfg, err := ConfigFromEnv(env)
			if tc.Expect.ErrorContains != "" {
				if err == nil || !strings.Contains(err.Error(), tc.Expect.ErrorContains) {
					t.Fatalf("got %v, want an error containing %q", err, tc.Expect.ErrorContains)
				}
				if !errors.Is(err, ErrInvalidConfig) || !errors.Is(err, ErrSyncFailed) {
					t.Errorf("error does not wrap ErrInvalidConfig and ErrSyncFailed: %v", err)
				}
				return
			}
			if err != nil {
				t.Fatalf("unexpected error: %v", err)
			}
			if cfg.ToolsURL() != tc.Expect.ToolsURL || cfg.EgressProxy != tc.Expect.EgressProxy || cfg.NoProxy != tc.Expect.NoProxy {
				t.Errorf("got %s / %s", cfg.ToolsURL(), cfg)
			}
			if cfg.Token != f.Token {
				t.Errorf("token not read")
			}
		})
	}
}

func TestProxyContractCases(t *testing.T) {
	var f syncFixture
	loadFixture(t, "tool-sync-cases.json", &f)
	for _, tc := range f.ProxyCases {
		t.Run(tc.Layer+"/"+tc.Name, func(t *testing.T) {
			env := tc.Env
			if env == nil {
				env = map[string]string{}
			}
			cfg := Config{EgressProxy: env[EgressProxyEnv], NoProxy: env[NoProxyEnv], Env: env}
			got, ok := cfg.ProxyURL(tc.URL)
			switch {
			case tc.ExpectProxy == nil && ok:
				t.Errorf("proxy %q, want a direct connection", got)
			case tc.ExpectProxy != nil && (!ok || got != *tc.ExpectProxy):
				t.Errorf("proxy %q (ok %v), want %q", got, ok, *tc.ExpectProxy)
			}
		})
	}
}
