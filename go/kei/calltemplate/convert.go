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

package calltemplate

import (
	"fmt"
	"strings"

	"github.com/haikeilabs/agentware/go/toolspec"
)

// ToToolSpecs converts validated templates to []toolspec.ToolSpec for
// one-shot manifest registration through toolsync.SocketRegistrar.
//
// Each template becomes a connector-bound tool with the given agentID.
// A missing name, permission, or binding.connector_id is a startup error
// — no entries are skipped.
func ToToolSpecs(templates []Template, agentID string) ([]toolspec.ToolSpec, error) {
	if len(templates) == 0 {
		return nil, fmt.Errorf("calltemplate: at least one template is required")
	}

	specs := make([]toolspec.ToolSpec, 0, len(templates))
	var errs []string

	for i, t := range templates {
		label := t.Name
		if label == "" {
			label = fmt.Sprintf("[%d]", i)
		}

		if t.Name == "" {
			errs = append(errs, fmt.Sprintf("%s: tool name is required", label))
			continue
		}
		if t.Permission == "" {
			errs = append(errs, fmt.Sprintf("%s: permission is required", label))
			continue
		}
		if t.Binding == nil || t.Binding.ConnectorID == "" {
			errs = append(errs, fmt.Sprintf("%s: binding.connector_id is required", label))
			continue
		}

		service := t.Service
		if service == "" {
			// Derive from category or permission prefix.
			service = extractService(t)
		}

		params, _ := parseParameters(t.Parameters)
		if params == nil {
			params = map[string]any{}
		}

		oc := permissionOperationClass(t.Permission)

		route := &toolspec.Route{
			ConnectorBinding: &toolspec.ConnectorBinding{
				AgentID:     agentID,
				ConnectorID: t.Binding.ConnectorID,
			},
		}

		specs = append(specs, toolspec.ToolSpec{
			Name:                 t.Name,
			Description:          t.Description,
			Parameters:           params,
			Service:              service,
			Source:               service,
			OperationClass:       oc,
			Route:                route,
			RequiredCapabilities: []string{t.Permission},
		})
	}

	if len(errs) > 0 {
		return nil, fmt.Errorf("calltemplate: %s", strings.Join(errs, "; "))
	}
	return specs, nil
}

// extractService derives a service name from a template. It prefers the
// Service field, then the Category, then the permission prefix (the part
// before the first dot).
func extractService(t Template) string {
	if t.Service != "" {
		return t.Service
	}
	if t.Category != "" {
		return t.Category
	}
	if idx := strings.Index(t.Permission, "."); idx > 0 {
		return t.Permission[:idx]
	}
	return t.Permission
}
