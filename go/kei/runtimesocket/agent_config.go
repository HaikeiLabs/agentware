package runtimesocket

import (
	"context"
	"encoding/json"
	"net/http"
)

// AgentConfigSchema is the schema of GET /v1/agent-config answers.
const AgentConfigSchema = "kei.agent-config/v1"

// AgentConfig is the platform configuration of the runtime's agent, from
// GET /v1/agent-config (kei-proxy 0.12.0 or later): the assigned model
// profile and the connectors bound to the agent. It is metadata only: it
// never carries a credential or a secret reference, so a harness config can
// name no model and no connector ID.
type AgentConfig struct {
	Schema string `json:"schema"`
	// ModelProfile is nil when the agent has no model profile assignment.
	ModelProfile *AgentModelProfile `json:"model_profile,omitempty"`
	Connectors   []AgentConnector   `json:"connectors"`
}

// AgentModelProfile is the agent's model profile assignment. Model,
// Endpoint and IsDefault are set only while the daemon holds that active
// profile's metadata.
type AgentModelProfile struct {
	ProfileID string `json:"profile_id"`
	Status    string `json:"status"`
	Model     string `json:"model,omitempty"`
	Endpoint  string `json:"endpoint,omitempty"`
	IsDefault *bool  `json:"is_default,omitempty"`
}

// AgentConnector is one connector bound to the agent, with the
// connector_binding tools routed through it.
type AgentConnector struct {
	ConnectorID string `json:"connector_id"`
	Name        string `json:"name,omitempty"`
	// Kind is the descriptor kind (native, http_api, mcp), not the
	// connection kind.
	Kind string `json:"kind,omitempty"`
	// Connection is the contract connection the installation uses (e.g.
	// pat, user_oauth); empty until the platform projects it.
	Connection string      `json:"connection,omitempty"`
	BaseURL    string      `json:"base_url,omitempty"`
	Tools      []AgentTool `json:"tools"`
}

// AgentTool is a registered connector_binding tool. Operations are the
// tool plan's operation ids.
type AgentTool struct {
	Tool             string   `json:"tool"`
	Operations       []string `json:"operations"`
	BindingVersion   string   `json:"binding_version,omitempty"`
	ResourceTemplate string   `json:"resource_template,omitempty"`
}

// AgentConfig reads GET /v1/agent-config. An older daemon answers 404 and
// one without a fresh v2 policy bundle 503 (code agent_config_unavailable);
// both are a proxy_error refusal. Unknown fields, credential fields
// included, are dropped.
func (c *Client) AgentConfig(ctx context.Context) (*AgentConfig, error) {
	resp, err := c.send(ctx, http.MethodGet, "/v1/agent-config", "", nil, c.timeout())
	if err != nil {
		return nil, err
	}
	body, err := resp.object()
	if err != nil {
		return nil, err
	}
	raw, err := json.Marshal(body)
	if err != nil {
		return nil, malformed("the daemon returned an invalid agent config")
	}
	var cfg AgentConfig
	if err := json.Unmarshal(raw, &cfg); err != nil || cfg.Schema != AgentConfigSchema || cfg.Connectors == nil {
		return nil, malformed("the daemon returned an invalid agent config")
	}
	for i := range cfg.Connectors {
		if cfg.Connectors[i].Tools == nil {
			cfg.Connectors[i].Tools = []AgentTool{}
		}
		for j := range cfg.Connectors[i].Tools {
			if cfg.Connectors[i].Tools[j].Operations == nil {
				cfg.Connectors[i].Tools[j].Operations = []string{}
			}
		}
	}
	return &cfg, nil
}
