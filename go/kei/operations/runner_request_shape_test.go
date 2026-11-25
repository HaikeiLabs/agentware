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

import (
	"context"
	"encoding/json"
	"net"
	"net/http"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"sync"
	"testing"

	github "github.com/HaikeiLabs/kei-connector-contracts/gen/go/kei/connectors/github/v1"
	"github.com/HaikeiLabs/kei-connector-contracts/gen/go/kei/connectors/github/v1/githubops"
	"github.com/haikeilabs/agentware/go/kei/runtimesocket"
)

// The request shape kei-connector-runtime proxy-v0.11.0 decides a
// connector_binding tool with: authorize carries the tool and its arguments
// (decideV2ConnectorPlan runs only when tool_args is set; without them a v2
// bundle denies preflight_required), and the credential release carries the
// bare operation id as its capability (the policy-set capability pattern
// ^[a-z0-9_]+(\.[a-z0-9_]+)*$ rejects "connector:") plus the same arguments,
// from which the runtime re-derives the plan before releasing.

var (
	shapeToolArgs    = map[string]any{"owner": "octo", "repository": "repo", "issue_number": float64(42)}
	shapeToolContext = map[string]any{"channel": "c1"}
)

func getIssue(ctx context.Context, r *Runner, opts ...RunOption) error {
	_, err := Run(ctx, r, testCaller(), githubops.OpGetIssue,
		func(conn any) (*github.GetIssueResponse, error) { return &github.GetIssueResponse{}, nil },
		&github.GetIssueRequest{Owner: "octo", Repository: "repo", IssueNumber: 42}, opts...)
	return err
}

func TestRunSendsTheToolCallAndBareOperationID(t *testing.T) {
	fc, _, cfg := allowFlow(t)
	cfg.InstalledConnectorID = "conn-github-0001"
	r := NewRunner(fc, "user-1", cfg)

	if err := getIssue(context.Background(), r, WithToolCall("github.get_issue", shapeToolArgs, shapeToolContext)); err != nil {
		t.Fatalf("Run: %v", err)
	}
	ar, rr := fc.authorizeReqs[0], fc.releaseReqs[0]
	if ar.Tool != "github.get_issue" || ar.Action != "" {
		t.Errorf("authorize tool/action = %q/%q, want github.get_issue and no action", ar.Tool, ar.Action)
	}
	if !reflect.DeepEqual(ar.ToolArgs, shapeToolArgs) || !reflect.DeepEqual(ar.ToolContext, shapeToolContext) {
		t.Errorf("authorize tool args/context = %v/%v", ar.ToolArgs, ar.ToolContext)
	}
	if rr.Capability != "github.issues.get" || rr.ConnectorID != "conn-github-0001" || rr.Tool != "github.get_issue" {
		t.Errorf("release capability/connector/tool = %q/%q/%q", rr.Capability, rr.ConnectorID, rr.Tool)
	}
	if !reflect.DeepEqual(rr.ToolArgs, shapeToolArgs) || !reflect.DeepEqual(rr.ToolContext, shapeToolContext) {
		t.Errorf("release tool args/context = %v/%v", rr.ToolArgs, rr.ToolContext)
	}
}

func TestRunWithoutToolCallSendsNoToolArguments(t *testing.T) {
	// Additive: callers that pass no RunOption keep the contract prefix as
	// the connector id and send no tool arguments.
	fc, _, cfg := allowFlow(t)
	if err := getIssue(context.Background(), NewRunner(fc, "user-1", cfg)); err != nil {
		t.Fatalf("Run: %v", err)
	}
	ar, rr := fc.authorizeReqs[0], fc.releaseReqs[0]
	if ar.Action != "" || ar.ToolArgs != nil || ar.ToolContext != nil {
		t.Errorf("authorize = %+v, want no action and no tool arguments", ar)
	}
	if rr.Capability != "github.issues.get" || rr.ConnectorID != "github" || rr.ToolArgs != nil {
		t.Errorf("release = %+v", rr)
	}
}

// wireDaemon is a Unix-socket daemon behind the real runtimesocket.Client: it
// records the JSON body of every authorize and release.
type wireDaemon struct {
	mu     sync.Mutex
	bodies map[string]map[string]any
}

func startWireDaemon(t *testing.T) (*wireDaemon, string) {
	t.Helper()
	dir, err := os.MkdirTemp("/tmp", "aw-op-")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = os.RemoveAll(dir) })
	path := filepath.Join(dir, "rt.sock")
	ln, err := net.Listen("unix", path)
	if err != nil {
		t.Fatal(err)
	}
	d := &wireDaemon{bodies: map[string]map[string]any{}}
	srv := &http.Server{Handler: http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		var body map[string]any
		_ = json.NewDecoder(r.Body).Decode(&body)
		d.mu.Lock()
		d.bodies[r.URL.Path] = body
		d.mu.Unlock()
		w.Header().Set("Content-Type", "application/json")
		switch r.URL.Path {
		case "/v1/session":
			_, _ = w.Write([]byte(`{"schema":"kei.session/v2","session_id":"s-1","status":"linked"}`))
		case "/v1/authorize":
			_, _ = w.Write([]byte(`{"schema":"kei.authorize-response/v1","decision":"permit"}`))
		case "/v1/credentials:release":
			_, _ = w.Write([]byte(`{"schema":"kei.credential-release-response/v1","decision":"permit","credential":{"connector_id":"conn-github-0001","access_token":"` + syntheticReleasedToken + `"}}`))
		default:
			w.WriteHeader(http.StatusNotFound)
		}
	})}
	go func() { _ = srv.Serve(ln) }()
	t.Cleanup(func() { _ = srv.Close() })
	return d, path
}

func TestWireBodiesCarryBareOperationIDAndToolArguments(t *testing.T) {
	d, path := startWireDaemon(t)
	client := &runtimesocket.AuthorizationClient{Source: "test", Socket: &runtimesocket.Client{
		SocketPath: path, Env: map[string]string{"KEI_RUNTIME_TOKEN": "kh_test"},
	}}
	cfg := testConfig("http://github.test")
	cfg.InstalledConnectorID = "conn-github-0001"

	if err := getIssue(context.Background(), NewRunner(client, "user-1", cfg),
		WithToolCall("github.get_issue", shapeToolArgs, shapeToolContext)); err != nil {
		t.Fatalf("Run: %v", err)
	}
	d.mu.Lock()
	defer d.mu.Unlock()
	authorize, release := d.bodies["/v1/authorize"], d.bodies["/v1/credentials:release"]
	if authorize["tool"] != "github.get_issue" {
		t.Errorf("authorize tool = %v", authorize["tool"])
	}
	for _, key := range []string{"action", "connector"} {
		if _, ok := authorize[key]; ok {
			t.Errorf("authorize body carries %q: %v", key, authorize)
		}
	}
	if !reflect.DeepEqual(authorize["tool_args"], shapeToolArgs) || !reflect.DeepEqual(authorize["tool_context"], shapeToolContext) {
		t.Errorf("authorize tool_args/tool_context = %v/%v", authorize["tool_args"], authorize["tool_context"])
	}
	wantConnector := map[string]any{"connector_id": "conn-github-0001", "capability": "github.issues.get"}
	if !reflect.DeepEqual(release["connector"], wantConnector) || release["tool"] != "github.get_issue" || release["resource"] != "repos/octo/repo" {
		t.Errorf("release body = %v", release)
	}
	if !reflect.DeepEqual(release["tool_args"], shapeToolArgs) || !reflect.DeepEqual(release["tool_context"], shapeToolContext) {
		t.Errorf("release tool_args/tool_context = %v/%v", release["tool_args"], release["tool_context"])
	}
	raw, _ := json.Marshal(d.bodies)
	if strings.Contains(string(raw), "connector:") {
		t.Errorf("a request body carries a connector: prefix: %s", raw)
	}
}
