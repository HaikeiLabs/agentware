package prompts

import "github.com/haikeilabs/agentware/go/tools"

// GenerateToolSchemas returns the JSON schema block for all tools in the registry.
func GenerateToolSchemas(registry *tools.ToolRegistry) []map[string]any {
	toolsList := registry.All()
	if len(toolsList) == 0 {
		return nil
	}

	schemas := make([]map[string]any, 0, len(toolsList))
	for _, t := range toolsList {
		schema := map[string]any{
			"type": "function",
			"function": map[string]any{
				"name":        t.Name(),
				"description": t.Description(),
			},
		}
		if et, ok := t.(tools.ExtendedTool); ok {
			if s := et.InputSchema(); s != nil {
				schema["function"].(map[string]any)["parameters"] = s
			}
		}
		schemas = append(schemas, schema)
	}
	return schemas
}
