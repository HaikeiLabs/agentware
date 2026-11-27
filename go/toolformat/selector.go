package toolformat

import "strings"

func GetFormatter(modelName string) ToolFormatter {
	lower := strings.ToLower(modelName)
	switch {
	case strings.Contains(lower, "nemotron"):
		return &NemotronFormatter{}
	case strings.Contains(lower, "qwen"), strings.Contains(lower, "qwq"):
		return &QwenFormatter{}
	case strings.Contains(lower, "llama"):
		return &LlamaFormatter{}
	case strings.Contains(lower, "mistral"):
		return &MistralFormatter{}
	case strings.Contains(lower, "anthropic"), strings.Contains(lower, "claude"):
		return &AnthropicFormatter{}
	case strings.Contains(lower, "deepseek"):
		return &DeepSeekFormatter{}
	case strings.Contains(lower, "glm"), strings.Contains(lower, "chatglm"):
		return &GLMFormatter{}
	case strings.Contains(lower, "gpt"), strings.Contains(lower, "o1"), strings.Contains(lower, "o3"):
		return &OpenAIFOrmatter{}
	default:
		return &GenericFormatter{}
	}
}
