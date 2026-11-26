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
	"errors"
	"testing"

	connection "github.com/HaikeiLabs/kei-connector-contracts/gen/go/kei/connection"
	github "github.com/HaikeiLabs/kei-connector-contracts/gen/go/kei/connectors/github/v1"
	"github.com/HaikeiLabs/kei-connector-contracts/gen/go/kei/connectors/github/v1/githubops"
	contracts "github.com/HaikeiLabs/kei-connector-contracts/gen/go/kei/contracts/v1"
	"github.com/haikeilabs/agentware/go/kei/evaluator"
	"github.com/haikeilabs/agentware/go/kei/runtimesocket"
)

const boundTool = "github.get_issue"

type fakeAgentConfig struct {
	cfg *runtimesocket.AgentConfig
	err error
}

func (f fakeAgentConfig) AgentConfig(context.Context) (*runtimesocket.AgentConfig, error) {
	return f.cfg, f.err
}

func agentConfigWith(edit func(*runtimesocket.AgentConnector)) fakeAgentConfig {
	c := runtimesocket.AgentConnector{
		ConnectorID: "c-1", Name: "acme-github", Kind: "native", Connection: "pat",
		BaseURL: "https://ghe.example",
		Tools:   []runtimesocket.AgentTool{{Tool: boundTool, Operations: []string{"get"}, BindingVersion: "v3"}},
	}
	if edit != nil {
		edit(&c)
	}
	return fakeAgentConfig{cfg: &runtimesocket.AgentConfig{
		Schema: runtimesocket.AgentConfigSchema, Connectors: []runtimesocket.AgentConnector{c},
	}}
}

func boundOps() map[string][]connection.Op {
	return map[string][]connection.Op{boundTool: {githubops.OpGetIssue}}
}

func TestConnectorConfigsBuildsTheBoundToolFromThePlatform(t *testing.T) {
	cfgs, err := ConnectorConfigs(context.Background(), agentConfigWith(nil), boundOps())
	if err != nil {
		t.Fatal(err)
	}
	cfg, ok := cfgs[boundTool]
	if !ok || len(cfgs) != 1 {
		t.Fatalf("configs = %#v, want one entry for %s", cfgs, boundTool)
	}
	if !cfg.DeriveConnector || cfg.InstalledConnectorID != "" {
		t.Errorf("the platform resolves the connector: DeriveConnector %v, InstalledConnectorID %q", cfg.DeriveConnector, cfg.InstalledConnectorID)
	}
	if cfg.ConnectorID != "github" || cfg.ConnectionName != "pat" {
		t.Errorf("ConnectorID %q ConnectionName %q", cfg.ConnectorID, cfg.ConnectionName)
	}
	if cfg.Conn.GetName() != "pat" || cfg.Conn.GetKind() != contracts.ConnectionKind_CONNECTION_KIND_HTTP_BEARER {
		t.Errorf("Conn = %v, want the contracts pat connection", cfg.Conn)
	}
	if cfg.BaseURL != "https://ghe.example" || cfg.BindingVersion != "v3" {
		t.Errorf("BaseURL %q BindingVersion %q", cfg.BaseURL, cfg.BindingVersion)
	}
}

func TestConnectorConfigsBaseURLFallsBackToTheContract(t *testing.T) {
	cfgs, err := ConnectorConfigs(context.Background(), agentConfigWith(func(c *runtimesocket.AgentConnector) { c.BaseURL = "" }), boundOps())
	if err != nil {
		t.Fatal(err)
	}
	if got := cfgs[boundTool].BaseURL; got != "https://api.github.com" {
		t.Errorf("BaseURL = %q, want the contract connector's", got)
	}
}

func TestConnectorConfigsLeavesOutToolsThePlatformCannotConfigure(t *testing.T) {
	for name, edit := range map[string]func(*runtimesocket.AgentConnector){
		"no connection yet":       func(c *runtimesocket.AgentConnector) { c.Connection = "" },
		"unknown connection":      func(c *runtimesocket.AgentConnector) { c.Connection = "user_token" },
		"tool not bound":          func(c *runtimesocket.AgentConnector) { c.Tools = []runtimesocket.AgentTool{} },
		"another tool only bound": func(c *runtimesocket.AgentConnector) { c.Tools[0].Tool = "other.tool" },
	} {
		t.Run(name, func(t *testing.T) {
			cfgs, err := ConnectorConfigs(context.Background(), agentConfigWith(edit), boundOps())
			if err != nil {
				t.Fatal(err)
			}
			if len(cfgs) != 0 {
				t.Errorf("configs = %#v, want none", cfgs)
			}
		})
	}
}

func TestConnectorConfigsRejectsToolsSpanningConnectors(t *testing.T) {
	tools := boundOps()
	tools[boundTool] = append(tools[boundTool], connection.Op{ID: "slack.chat.post"})
	if _, err := ConnectorConfigs(context.Background(), agentConfigWith(nil), tools); err == nil || errors.Is(err, ErrConnectorsUnavailable) {
		t.Fatalf("err = %v, want a caller error", err)
	}
}

func TestConnectorConfigsFailsClosedWhenTheAgentConfigIsUnavailable(t *testing.T) {
	for name, src := range map[string]fakeAgentConfig{
		"503":  {err: &runtimesocket.Error{Class: evaluator.ReasonProxyError, Code: "agent_config_unavailable"}},
		"404":  {err: &runtimesocket.Error{Class: evaluator.ReasonProxyError}},
		"down": {err: &runtimesocket.Error{Class: evaluator.ReasonProxyUnavailable}},
		"nil":  {},
	} {
		t.Run(name, func(t *testing.T) {
			cfgs, err := ConnectorConfigs(context.Background(), src, boundOps())
			var unavailable *ConnectorsUnavailableError
			if cfgs != nil || !errors.Is(err, ErrConnectorsUnavailable) || !errors.As(err, &unavailable) {
				t.Fatalf("cfgs %v err %v, want ConnectorsUnavailableError", cfgs, err)
			}
			if unavailable.ReasonCode() != "connectors_unavailable" {
				t.Errorf("ReasonCode = %q", unavailable.ReasonCode())
			}
			var se *runtimesocket.Error
			if errors.As(src.err, &se) && (unavailable.Class != se.Class || unavailable.Code != se.Code) {
				t.Errorf("class %q code %q, want %q %q", unavailable.Class, unavailable.Code, se.Class, se.Code)
			}
		})
	}
}

func TestRunReleasesWithoutAConnectorForAPlatformConfiguredTool(t *testing.T) {
	p := newProviderServer(t)
	cfgs, err := ConnectorConfigs(context.Background(), agentConfigWith(func(c *runtimesocket.AgentConnector) { c.BaseURL = p.URL }), boundOps())
	if err != nil {
		t.Fatal(err)
	}
	fc := &fakeClient{}
	r := NewRunner(fc, "user-1", cfgs[boundTool], WithHTTPClient(p.Client()))
	args := map[string]any{"owner": "octo", "repository": "repo", "issue_number": 42}
	_, err = Run(context.Background(), r, testCaller(), githubops.OpGetIssue,
		func(conn any) (*github.GetIssueResponse, error) {
			status, _, _, err := conn.(connection.HTTP).Do(context.Background(), "GET", "/repos/octo/repo/issues/42", nil, nil)
			if err != nil || status != 200 {
				return nil, errors.New("provider call failed")
			}
			return &github.GetIssueResponse{}, nil
		},
		&github.GetIssueRequest{Owner: "octo", Repository: "repo", IssueNumber: 42},
		WithToolCall(boundTool, args, nil))
	if err != nil {
		t.Fatal(err)
	}
	if len(fc.releaseReqs) != 1 {
		t.Fatalf("release requests = %d", len(fc.releaseReqs))
	}
	rel := fc.releaseReqs[0]
	if rel.ConnectorID != "" || rel.Capability != "" || rel.Tool != boundTool || rel.ToolArgs == nil {
		t.Errorf("release = %+v, want no connector and the bound tool with its args", rel)
	}
	if fc.authorizeReqs[0].BindingVersion != "v3" {
		t.Errorf("authorize binding version = %q", fc.authorizeReqs[0].BindingVersion)
	}
	if p.authHeader() != "Bearer "+syntheticReleasedToken {
		t.Error("the provider did not get the released credential")
	}
}

func TestRunWithoutAConnectorNeedsTheToolArguments(t *testing.T) {
	cfgs, err := ConnectorConfigs(context.Background(), agentConfigWith(nil), boundOps())
	if err != nil {
		t.Fatal(err)
	}
	fc := &fakeClient{}
	r := NewRunner(fc, "user-1", cfgs[boundTool])
	_, err = Run(context.Background(), r, testCaller(), githubops.OpGetIssue,
		func(any) (*github.GetIssueResponse, error) { return &github.GetIssueResponse{}, nil },
		&github.GetIssueRequest{Owner: "octo", Repository: "repo", IssueNumber: 42})
	var denied *OperationDeniedError
	if !errors.As(err, &denied) || denied.ReasonCode != "connector_not_named" {
		t.Fatalf("err = %v, want connector_not_named", err)
	}
	if len(fc.authorizeReqs)+len(fc.releaseReqs) != 0 {
		t.Error("no socket call may be made")
	}
}
