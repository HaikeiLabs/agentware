package prompts

import (
	"fmt"

	"github.com/haikeilabs/agentware/go/tools"
)

func GenerateToolSection(registry *tools.ToolRegistry) string {
	toolsList := registry.All()
	if len(toolsList) == 0 {
		return ""
	}

	result := "## Available Tools\n\n"
	for _, t := range toolsList {
		result += fmt.Sprintf("### %s\n%s\n\n", t.Name(), t.Description())

		if et, ok := t.(tools.ExtendedTool); ok {
			if examples := et.Examples(); len(examples) > 0 {
				result += "Examples:\n"
				for _, ex := range examples {
					result += fmt.Sprintf("- Input: %v → Output: %s\n", ex.Input, ex.Output)
				}
				result += "\n"
			}
		}
	}
	return result
}
