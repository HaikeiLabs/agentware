package memory

import (
	_ "embed"
	"fmt"
	"time"

	"gopkg.in/yaml.v3"

	"github.com/haikeilabs/agentware/go/middleware"
)

//go:embed policy.yaml
var defaultPolicyYAML []byte

// yamlPolicy mirrors middleware.Policy for YAML loading. The middleware
// types carry no YAML tags, so the mapping lives here; if a YAML loader
// lands in the middleware package this converts to a thin wrapper.
type yamlPolicy struct {
	DefaultDeny bool       `yaml:"default_deny"`
	Rules       []yamlRule `yaml:"rules"`
}

type yamlRule struct {
	Name       string          `yaml:"name"`
	Tools      []string        `yaml:"tools"`
	Action     string          `yaml:"action"`
	Conditions []yamlCondition `yaml:"conditions"`
	MaxRate    *yamlRate       `yaml:"max_rate"`
}

type yamlCondition struct {
	Field    string `yaml:"field"`
	Operator string `yaml:"operator"`
	Value    string `yaml:"value"`
}

type yamlRate struct {
	Count         int `yaml:"count"`
	WindowSeconds int `yaml:"window_seconds"`
}

// LoadPolicyYAML parses a declarative policy document into the stock
// middleware Policy.
func LoadPolicyYAML(data []byte) (*middleware.Policy, error) {
	var yp yamlPolicy
	if err := yaml.Unmarshal(data, &yp); err != nil {
		return nil, fmt.Errorf("memory: parse policy yaml: %w", err)
	}
	p := &middleware.Policy{DefaultDeny: yp.DefaultDeny}
	for _, yr := range yp.Rules {
		action := middleware.Action(yr.Action)
		switch action {
		case middleware.ActionAllow, middleware.ActionDeny, middleware.ActionFilter:
		default:
			return nil, fmt.Errorf("memory: rule %q has unknown action %q", yr.Name, yr.Action)
		}
		rule := middleware.Rule{Name: yr.Name, Tools: yr.Tools, Action: action}
		for _, yc := range yr.Conditions {
			rule.Conditions = append(rule.Conditions, middleware.Condition{
				Field:    yc.Field,
				Operator: middleware.Operator(yc.Operator),
				Value:    yc.Value,
			})
		}
		if yr.MaxRate != nil {
			rule.MaxRate = &middleware.RateLimit{
				Count:  yr.MaxRate.Count,
				Window: time.Duration(yr.MaxRate.WindowSeconds) * time.Second,
			}
		}
		p.Rules = append(p.Rules, rule)
	}
	return p, nil
}

// DefaultPolicy returns the embedded declarative policy for memory tools:
// deny-by-default, scope-override and anonymous-caller denies, and per-tier
// rate limits.
func DefaultPolicy() *middleware.Policy {
	p, err := LoadPolicyYAML(defaultPolicyYAML)
	if err != nil {
		// The embedded document is compiled in; failing to parse it is a
		// build defect, not a runtime condition.
		panic(err)
	}
	return p
}
