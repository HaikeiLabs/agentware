// Copyright 2026 Haikei Labs
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.

package operations

// The Runner end to end against a real `kei-proxy serve`, bootstrapped from
// fixtures/kei/fake-control-plane.py serving its operation-plan bundle: a
// catalog-shaped kei.policy-bundle/v2 with one connector_binding tool
// (github.list_pulls) whose plan derives a github.pulls.list operation from
// the tool arguments under an args_schema, bound to a shared GitHub connector.
//
// Skipped unless KEI_PROXY_TEST_BINARY names a kei-proxy binary (proxy-v0.11.0
// or later) and python3 is on PATH:
//
//	go -C <kei-connector-runtime> build -o /tmp/kei-proxy ./cmd/kei-proxy
//	KEI_PROXY_TEST_BINARY=/tmp/kei-proxy go test ./kei/operations -run RealProxy

import (
	"bufio"
	"context"
	"errors"
	"net/http"
	"net/http/httptest"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"
	"sync"
	"testing"
	"time"

	connection "github.com/HaikeiLabs/kei-connector-contracts/gen/go/kei/connection"
	github "github.com/HaikeiLabs/kei-connector-contracts/gen/go/kei/connectors/github/v1"
	"github.com/HaikeiLabs/kei-connector-contracts/gen/go/kei/connectors/github/v1/githubops"
	contracts "github.com/HaikeiLabs/kei-connector-contracts/gen/go/kei/contracts/v1"
	"github.com/haikeilabs/agentware/go/kei/runtimesocket"
)

const (
	realProxyRuntimeToken   = "kh_live_ci_dummy_token_0123456789abcdef0123456789abcdef0123456789abcdef"
	realProxyConnectorToken = "fake-connector-token-e2e"
	realProxyConnectorID    = "conn-github-ci"
	realProxyTool           = "github.list_pulls"
)

type realProxy struct {
	socketPath string
	env        map[string]string
}

func startRealProxy(t *testing.T) *realProxy {
	t.Helper()
	binary := os.Getenv("KEI_PROXY_TEST_BINARY")
	if binary == "" {
		t.Skip("KEI_PROXY_TEST_BINARY is not set to a kei-proxy binary")
	}
	python, err := exec.LookPath("python3")
	if err != nil {
		t.Skip("python3 is required for the fake control plane")
	}
	_, file, _, _ := runtime.Caller(0)
	fakeControlPlane := filepath.Join(filepath.Dir(file), "../../../fixtures/kei/fake-control-plane.py")

	// Short path: macOS limits a Unix socket path to 104 bytes.
	root, err := os.MkdirTemp("/tmp", "kp-op-")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = os.RemoveAll(root) })

	cp := exec.Command(python, fakeControlPlane)
	cp.Env = append(os.Environ(), "FAKE_CONTROL_PLANE_BUNDLE=operation-plan",
		"FAKE_CONTROL_PLANE_CONNECTOR_TOKEN="+realProxyConnectorToken)
	stdout, err := cp.StdoutPipe()
	if err != nil {
		t.Fatal(err)
	}
	if err := cp.Start(); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = cp.Process.Kill(); _ = cp.Wait() })
	port, err := bufio.NewReader(stdout).ReadString('\n')
	if err != nil {
		t.Fatalf("fake control plane port: %v", err)
	}
	url := "http://127.0.0.1:" + strings.TrimSpace(port)

	p := &realProxy{socketPath: filepath.Join(root, "rt.sock")}
	p.env = map[string]string{
		"PATH":                          os.Getenv("PATH"),
		"HOME":                          root,
		"KEI_RUNTIME_TOKEN":             realProxyRuntimeToken,
		"KEI_RUNTIME_CONTROL_PLANE_URL": url,
		"KEI_RUNTIME_VERSION":           "e2e",
		"KEI_RUNTIME_STATE_DIR":         filepath.Join(root, "state"),
		"KEI_RUNTIME_SOCKET_PATH":       p.socketPath,
		"KEI_PROXY_LISTEN_ADDR":         "127.0.0.1:0",
		"KEI_PROXY_AUDIT":               filepath.Join(root, "audit.jsonl"),
		"KEI_CREDENTIAL_RELEASE":        "identity",
		"KEI_CREDENTIAL_RELEASE_URL":    url,
		"ENV":                           "development",
	}
	proxy := exec.Command(binary, "serve")
	for k, v := range p.env {
		proxy.Env = append(proxy.Env, k+"="+v)
	}
	if err := proxy.Start(); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = proxy.Process.Kill(); _ = proxy.Wait() })

	client := &runtimesocket.Client{SocketPath: p.socketPath, Env: p.env}
	deadline := time.Now().Add(30 * time.Second)
	for time.Now().Before(deadline) {
		if st, err := client.PolicyBundleStatus(context.Background()); err == nil && st["state"] == "active" {
			return p
		}
		time.Sleep(100 * time.Millisecond)
	}
	t.Fatal("kei-proxy serve did not activate the operation-plan bundle")
	return nil
}

type pullsProvider struct {
	*httptest.Server
	mu    sync.Mutex
	paths []string
	auth  string
}

func newPullsProvider(t *testing.T) *pullsProvider {
	t.Helper()
	p := &pullsProvider{}
	p.Server = httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		p.mu.Lock()
		p.paths = append(p.paths, r.URL.Path)
		p.auth = r.Header.Get("Authorization")
		p.mu.Unlock()
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`[{"number": 7, "title": "e2e"}]`))
	}))
	t.Cleanup(p.Close)
	return p
}

func runListPulls(t *testing.T, p *realProxy, provider *pullsProvider, args map[string]any, audit *[]AuditRecord) error {
	t.Helper()
	client := &runtimesocket.AuthorizationClient{Source: "ci", Socket: &runtimesocket.Client{SocketPath: p.socketPath, Env: p.env}}
	r := NewRunner(client, "ci-user", ConnectorConfig{
		ConnectorID:          "github",
		InstalledConnectorID: realProxyConnectorID,
		ConnectionName:       "pat",
		Conn:                 &contracts.Connection{Kind: contracts.ConnectionKind_CONNECTION_KIND_HTTP_BEARER},
		BaseURL:              provider.URL,
	}, WithAuditSink(func(a AuditRecord) { *audit = append(*audit, a) }))
	_, err := Run(context.Background(), r, testCaller(), githubops.OpListPulls,
		func(conn any) (*github.ListPullsResponse, error) {
			status, _, _, err := conn.(connection.HTTP).Do(context.Background(), "GET", "/repos/acme/widgets/pulls", nil, nil)
			if err != nil || status != http.StatusOK {
				return nil, errors.New("provider call failed")
			}
			return &github.ListPullsResponse{}, nil
		},
		&github.ListPullsRequest{Owner: "acme", Repository: "widgets"},
		WithToolCall(realProxyTool, args, map[string]any{}))
	return err
}

func TestRealProxyPermitAndCredentialReleaseForAPlannedTool(t *testing.T) {
	p := startRealProxy(t)
	provider := newPullsProvider(t)
	var audit []AuditRecord

	if err := runListPulls(t, p, provider, map[string]any{"owner": "acme", "repository": "widgets"}, &audit); err != nil {
		t.Fatalf("Run: %v", err)
	}
	provider.mu.Lock()
	defer provider.mu.Unlock()
	if len(provider.paths) != 1 || provider.auth != "Bearer "+realProxyConnectorToken {
		t.Errorf("provider paths = %v, auth set = %t; want one call with the released token", provider.paths, provider.auth != "")
	}
	if len(audit) != 1 || audit[0] != (AuditRecord{OperationID: "github.pulls.list", Decision: "allow", Outcome: "success"}) {
		t.Errorf("audit = %+v", audit)
	}
}

func TestRealProxyToolArgsViolatingArgsSchemaAreDenied(t *testing.T) {
	p := startRealProxy(t)
	provider := newPullsProvider(t)
	var audit []AuditRecord

	err := runListPulls(t, p, provider, map[string]any{"owner": "acme", "repository": "widgets", "extra": "x"}, &audit)
	deniedWithCode(t, err, "operation_plan_invalid")
	if strings.Contains(err.Error(), realProxyConnectorToken) {
		t.Error("the denied error carries the connector credential")
	}
	provider.mu.Lock()
	defer provider.mu.Unlock()
	if len(provider.paths) != 0 {
		t.Errorf("provider calls = %v, want none", provider.paths)
	}
	if len(audit) != 1 || audit[0] != (AuditRecord{OperationID: "github.pulls.list", Decision: "deny", Outcome: "denied"}) {
		t.Errorf("audit = %+v", audit)
	}
}
