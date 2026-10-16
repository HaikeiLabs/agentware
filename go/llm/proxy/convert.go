package proxy

import (
	"encoding/json"

	"github.com/haikeilabs/agentware/go/llm"
)

type ToolList []map[string]any
type MessageList []map[string]any

func ToInternalMessages(openAIMessages MessageList) []llm.Message {
	messages := make([]llm.Message, 0, len(openAIMessages))

	for _, om := range openAIMessages {
		role, _ := om["role"].(string)
		content, _ := om["content"].(string)
		toolCallID, _ := om["tool_call_id"].(string)

		msg := llm.Message{
			Role:       llm.Role(role),
			Content:    content,
			ToolCallID: toolCallID,
		}

		if toolCallsRaw, ok := om["tool_calls"].([]any); ok {
			toolCalls := make([]llm.ToolCall, 0, len(toolCallsRaw))
			for _, tcRaw := range toolCallsRaw {
				tcMap, ok := tcRaw.(map[string]any)
				if !ok {
					continue
				}

				var tc llm.ToolCall
				if id, ok := tcMap["id"].(string); ok {
					tc.ID = id
				}

				if funcRaw, ok := tcMap["function"].(map[string]any); ok {
					if name, ok := funcRaw["name"].(string); ok {
						tc.Name = name
					}
					if args, ok := funcRaw["arguments"].(string); ok {
						var argsMap map[string]any
						_ = json.Unmarshal([]byte(args), &argsMap)
						tc.Args = argsMap
					}
				}

				toolCalls = append(toolCalls, tc)
			}
			msg.ToolCalls = toolCalls
		}

		messages = append(messages, msg)
	}

	return messages
}

func ToOpenAIMessages(internalMessages []llm.Message) MessageList {
	messages := make([]map[string]any, 0, len(internalMessages))

	for _, im := range internalMessages {
		msg := map[string]any{
			"role":    string(im.Role),
			"content": im.Content,
		}

		if im.ToolCallID != "" {
			msg["tool_call_id"] = im.ToolCallID
		}

		if len(im.ToolCalls) > 0 {
			toolCalls := make([]map[string]any, 0, len(im.ToolCalls))
			for _, tc := range im.ToolCalls {
				tcMap := map[string]any{
					"id":   tc.ID,
					"type": "function",
					"function": map[string]any{
						"name":      tc.Name,
						"arguments": tc.Args,
					},
				}
				toolCalls = append(toolCalls, tcMap)
			}
			msg["tool_calls"] = toolCalls
		}

		messages = append(messages, msg)
	}

	return messages
}

func ToInternalTools(openAITools ToolList) []llm.ToolDefinition {
	if openAITools == nil {
		return nil
	}

	tools := make([]llm.ToolDefinition, 0, len(openAITools))

	for _, ot := range openAITools {
		toolType, _ := ot["type"].(string)
		if toolType != "function" {
			continue
		}

		funcRaw, ok := ot["function"].(map[string]any)
		if !ok {
			continue
		}

		tool := llm.ToolDefinition{
			Name:        "",
			Description: "",
			InputSchema: nil,
		}

		if name, ok := funcRaw["name"].(string); ok {
			tool.Name = name
		}
		if desc, ok := funcRaw["description"].(string); ok {
			tool.Description = desc
		}
		if params, ok := funcRaw["parameters"].(map[string]any); ok {
			tool.InputSchema = params
		}

		tools = append(tools, tool)
	}

	return tools
}

func ToOpenAITools(internalTools []llm.ToolDefinition) ToolList {
	if internalTools == nil {
		return nil
	}

	tools := make([]map[string]any, 0, len(internalTools))

	for _, it := range internalTools {
		tools = append(tools, map[string]any{
			"type": "function",
			"function": map[string]any{
				"name":        it.Name,
				"description": it.Description,
				"parameters":  it.InputSchema,
			},
		})
	}

	return tools
}

func makeRespondTool() map[string]any {
	return map[string]any{
		"type": "function",
		"function": map[string]any{
			"name":        "respond",
			"description": "Use this tool to provide your final response when you have completed the task or need to respond to the user.",
			"parameters": map[string]any{
				"type": "object",
				"properties": map[string]any{
					"message": map[string]any{
						"type":        "string",
						"description": "The message to send to the user",
					},
				},
				"required": []string{"message"},
			},
		},
	}
}

func injectRespondTool(tools ToolList) ToolList {
	respondTool := makeRespondTool()
	if tools == nil {
		return ToolList{respondTool}
	}
	result := make(ToolList, len(tools)+1)
	result[0] = respondTool
	copy(result[1:], tools)
	return result
}

func HasToolCalls(response map[string]any) bool {
	choices, ok := response["choices"].([]any)
	if !ok || len(choices) == 0 {
		return false
	}

	choice, ok := choices[0].(map[string]any)
	if !ok {
		return false
	}

	msg, ok := choice["message"].(map[string]any)
	if !ok {
		return false
	}

	_, hasToolCalls := msg["tool_calls"]
	return hasToolCalls
}

func ExtractToolCall(response map[string]any) (name string, args map[string]any, found bool) {
	choices, ok := response["choices"].([]any)
	if !ok || len(choices) == 0 {
		return
	}

	choice, ok := choices[0].(map[string]any)
	if !ok {
		return
	}

	msg, ok := choice["message"].(map[string]any)
	if !ok {
		return
	}

	toolCallsRaw, ok := msg["tool_calls"].([]any)
	if !ok || len(toolCallsRaw) == 0 {
		return
	}

	tcMap, ok := toolCallsRaw[0].(map[string]any)
	if !ok {
		return
	}

	funcRaw, ok := tcMap["function"].(map[string]any)
	if !ok {
		return
	}

	name, _ = funcRaw["name"].(string)

	argsStr, _ := funcRaw["arguments"].(string)
	if argsStr != "" {
		_ = json.Unmarshal([]byte(argsStr), &args)
	}

	found = true
	return
}

func StripRespondTool(response map[string]any) map[string]any {
	if !HasToolCalls(response) {
		return response
	}

	name, args, found := ExtractToolCall(response)
	if !found || name != "respond" {
		return response
	}

	result := make(map[string]any)
	for k, v := range response {
		result[k] = v
	}

	choices, _ := response["choices"].([]any)
	if len(choices) > 0 {
		choice := make(map[string]any)
		for k, v := range choices[0].(map[string]any) {
			choice[k] = v
		}

		msg := make(map[string]any)
		for k, v := range choice["message"].(map[string]any) {
			msg[k] = v
		}

		message, _ := args["message"].(string)
		msg["content"] = message
		msg["tool_calls"] = []any{}

		choice["message"] = msg
		choice["finish_reason"] = "stop"
		result["choices"] = []any{choice}
	}

	return result
}
