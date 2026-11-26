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
	"fmt"
	"strings"

	connection "github.com/HaikeiLabs/kei-connector-contracts/gen/go/kei/connection"
	contracts "github.com/HaikeiLabs/kei-connector-contracts/gen/go/kei/contracts/v1"
	"github.com/haikeilabs/agentware/go/kei/evaluator"
	"github.com/haikeilabs/agentware/go/kei/runtimesocket"
)

// AgentConfigReader reads the agent's platform configuration.
// *runtimesocket.Client satisfies it.
type AgentConfigReader interface {
	AgentConfig(ctx context.Context) (*runtimesocket.AgentConfig, error)
}

// ErrConnectorsUnavailable is the sentinel a ConnectorsUnavailableError
// unwraps to.
var ErrConnectorsUnavailable = errors.New("connectors unavailable")

// ConnectorsUnavailableError means kei-proxy could not say which connectors
// the agent has: a daemon older than 0.12.0 (404), one without a fresh v2
// policy bundle (503 agent_config_unavailable), an unreachable daemon or a
// malformed answer. Callers fail closed. Class and Code are the socket
// refusal's, when there was one.
type ConnectorsUnavailableError struct {
	Class evaluator.ReasonClass
	Code  string
}

func (e *ConnectorsUnavailableError) Error() string {
	if e.Class == "" {
		return "connectors unavailable"
	}
	if e.Code == "" {
		return fmt.Sprintf("connectors unavailable (%s)", e.Class)
	}
	return fmt.Sprintf("connectors unavailable (%s: %s)", e.Class, e.Code)
}

// ReasonCode is the typed reason: connectors_unavailable.
func (e *ConnectorsUnavailableError) ReasonCode() string { return "connectors_unavailable" }

func (e *ConnectorsUnavailableError) Unwrap() error { return ErrConnectorsUnavailable }

// ConnectorConfigs builds a ConnectorConfig per registered tool from the
// platform, so the harness names no connector ID. tools maps each
// connector_binding tool the harness serves to the generated operations it
// runs (githubops.OpGetIssue, ...).
//
// For each tool it reads, from GET /v1/agent-config (kei-proxy 0.12.0 or
// later), the bound connector's connection name and base URL and the tool's
// binding version; the connection's kind and header come from the contracts
// Connection of that name on the contract connector the operations belong
// to (its generated package must be linked in). The config has
// DeriveConnector set: the runner releases without a connector and
// kei-proxy derives it from the tool's binding.
//
// A tool is left out (Run then has no config for it) when the platform does
// not bind it to the agent, the installation does not name its connection
// yet, or the connection is not the contract connector's. It returns a
// *ConnectorsUnavailableError when the agent config cannot be read, and an
// error when one tool's operations span more than one contract connector.
func ConnectorConfigs(ctx context.Context, src AgentConfigReader, tools map[string][]connection.Op) (map[string]ConnectorConfig, error) {
	cfg, err := src.AgentConfig(ctx)
	if err != nil {
		unavailable := &ConnectorsUnavailableError{}
		var se *runtimesocket.Error
		if errors.As(err, &se) {
			unavailable.Class, unavailable.Code = se.Class, se.Code
		}
		return nil, unavailable
	}
	if cfg == nil || cfg.Connectors == nil {
		return nil, &ConnectorsUnavailableError{Class: evaluator.ReasonMalformedResponse}
	}

	type boundTool struct {
		connector runtimesocket.AgentConnector
		tool      runtimesocket.AgentTool
	}
	bound := map[string]boundTool{}
	for _, c := range cfg.Connectors {
		for _, t := range c.Tools {
			bound[t.Tool] = boundTool{connector: c, tool: t}
		}
	}

	out := map[string]ConnectorConfig{}
	for name, ops := range tools {
		prefix, err := contractPrefix(name, ops)
		if err != nil {
			return nil, err
		}
		b, ok := bound[name]
		if !ok || prefix == "" || b.connector.Connection == "" {
			continue
		}
		contract, ok := contracts.LookupConnector(prefix)
		if !ok {
			continue
		}
		var spec *contracts.Connection
		for _, c := range contract.GetConnections() {
			if c.GetName() == b.connector.Connection {
				spec = c
				break
			}
		}
		if spec == nil {
			continue
		}
		baseURL := b.connector.BaseURL
		if baseURL == "" {
			baseURL = spec.GetBaseUrl()
		}
		if baseURL == "" {
			baseURL = contract.GetBaseUrl()
		}
		out[name] = ConnectorConfig{
			ConnectorID:     prefix,
			ConnectionName:  spec.GetName(),
			Conn:            spec,
			BaseURL:         baseURL,
			BindingVersion:  b.tool.BindingVersion,
			DeriveConnector: true,
		}
	}
	return out, nil
}

// contractPrefix is the one contract connector (github) a tool's operation
// ids share, or "" for no operations.
func contractPrefix(tool string, ops []connection.Op) (string, error) {
	prefix := ""
	for _, op := range ops {
		p, _, _ := strings.Cut(op.ID, ".")
		if prefix != "" && p != prefix {
			return "", fmt.Errorf("tool %q binds operations of more than one contract connector", tool)
		}
		prefix = p
	}
	return prefix, nil
}
