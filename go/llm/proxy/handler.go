package proxy

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"log"
	"net/http"
	"time"

	"github.com/haikeilabs/agentware/go/llm"
	"github.com/haikeilabs/agentware/go/middleware/guardrails"
	"github.com/haikeilabs/agentware/go/middleware/inference"
)

type Handler struct {
	config         *ProxyConfig
	backend        llm.Backend
	contextManager *llm.ContextWindowManager
	validator      *guardrails.ResponseValidator
	errorTracker   *guardrails.ErrorTracker
	stepEnforcer   *guardrails.StepEnforcer
}

func NewHandler(cfg *ProxyConfig) (*Handler, error) {
	backendConfig := llm.ServerConfig{
		BaseURL:       cfg.BackendURL,
		APIKey:        cfg.APIKey,
		Model:         cfg.Model,
		ContextWindow: cfg.ContextWindow,
		Timeout:       cfg.Timeout,
	}
	backend := llm.NewServerBackend(backendConfig)

	counter := llm.DefaultCounter
	contextManager := llm.NewContextWindowManager(cfg.ContextWindow, counter)

	validator := guardrails.NewResponseValidator(nil, true)

	errorTracker := guardrails.NewErrorTracker()

	stepEnforcer := guardrails.NewStepEnforcer()

	return &Handler{
		config:         cfg,
		backend:        backend,
		contextManager: contextManager,
		validator:      validator,
		errorTracker:   errorTracker,
		stepEnforcer:   stepEnforcer,
	}, nil
}

func (h *Handler) handleChatCompletions(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
		return
	}

	body, err := io.ReadAll(r.Body)
	if err != nil {
		http.Error(w, "failed to read request body", http.StatusBadRequest)
		return
	}
	// #nosec G104
	defer func() { _ = r.Body.Close() }()

	var req map[string]any
	if err := json.Unmarshal(body, &req); err != nil {
		http.Error(w, "invalid JSON", http.StatusBadRequest)
		return
	}

	stream, _ := req["stream"].(bool)

	var messagesRaw MessageList
	var toolsRaw ToolList
	if msgs, ok := req["messages"].([]any); ok {
		messagesRaw = make(MessageList, len(msgs))
		for i, m := range msgs {
			if mm, ok := m.(map[string]any); ok {
				messagesRaw[i] = mm
			}
		}
	}
	if tls, ok := req["tools"].([]any); ok {
		toolsRaw = make(ToolList, len(tls))
		for i, t := range tls {
			if tt, ok := t.(map[string]any); ok {
				toolsRaw[i] = tt
			}
		}
	}

	messages := ToInternalMessages(messagesRaw)
	tools := ToInternalTools(toolsRaw)

	if len(tools) > 0 {
		req["tools"] = injectRespondTool(toolsRaw)
	}

	if stream {
		h.handleStreaming(w, req, messages, tools)
	} else {
		h.handleNonStreaming(w, req, messages, tools)
	}
}

func (h *Handler) handleNonStreaming(w http.ResponseWriter, req map[string]any, messages []llm.Message, tools []llm.ToolDefinition) {
	cfg := inference.InferenceConfig{
		Client:         h.backend,
		ContextManager: h.contextManager,
		Validator:      h.validator,
		ErrorTracker:   h.errorTracker,
		StepEnforcer:   h.stepEnforcer,
		ToolSpecs:      tools,
		MaxAttempts:    h.config.MaxRetries,
	}

	result, err := inference.RunInference(context.Background(), messages, cfg)
	if err != nil {
		log.Printf("Inference failed: %v", err)
		http.Error(w, fmt.Sprintf("inference failed: %v", err), http.StatusInternalServerError)
		return
	}

	openAIResp := h.buildResponse(result.Response)

	if len(result.Response.ToolCalls) > 0 {
		hasRespondTool := false
		for _, tc := range result.Response.ToolCalls {
			if tc.Name == "respond" {
				hasRespondTool = true
				break
			}
		}
		if hasRespondTool {
			openAIResp = StripRespondTool(openAIResp)
		}
	}

	w.Header().Set("Content-Type", "application/json")
	// #nosec G104
	_ = json.NewEncoder(w).Encode(openAIResp)
}

func (h *Handler) handleStreaming(w http.ResponseWriter, req map[string]any, messages []llm.Message, tools []llm.ToolDefinition) {
	w.Header().Set("Content-Type", "text/event-stream")
	w.Header().Set("Cache-Control", "no-cache")
	w.Header().Set("Connection", "keep-alive")
	w.Header().Set("X-Accel-Buffering", "no")

	flusher, ok := w.(http.Flusher)
	if !ok {
		http.Error(w, "streaming not supported", http.StatusInternalServerError)
		return
	}

	maxTokens, _ := req["max_tokens"].(float64)
	temperature, _ := req["temperature"].(float64)

	llmReq := &llm.Request{
		Messages:    messages,
		Tools:       tools,
		Temperature: temperature,
		MaxTokens:   int(maxTokens),
		Stop:        []string{"[DONE]"},
	}

	result, err := h.backend.Complete(context.Background(), llmReq)
	if err != nil {
		log.Printf("Inference failed: %v", err)
		http.Error(w, fmt.Sprintf("inference failed: %v", err), http.StatusInternalServerError)
		return
	}

	openAIChunk := h.buildStreamingChunk(llm.Response{
		Content:      result.Content,
		ToolCalls:    result.ToolCalls,
		FinishReason: result.FinishReason,
	})
	data, _ := json.Marshal(openAIChunk)
	// #nosec G104
	_, _ = fmt.Fprintf(w, "data: %s\n\n", string(data))
	flusher.Flush()

	if len(result.ToolCalls) > 0 {
		hasRespondTool := false
		for _, tc := range result.ToolCalls {
			if tc.Name == "respond" {
				hasRespondTool = true
				break
			}
		}
		if hasRespondTool {
			// #nosec G104
			_, _ = fmt.Fprintf(w, "data: [DONE]\n\n")
			flusher.Flush()
			return
		}
	}

	// #nosec G104
	_, _ = fmt.Fprintf(w, "data: [DONE]\n\n")
	flusher.Flush()
}

func (h *Handler) buildResponse(resp llm.Response) map[string]any {
	choices := []map[string]any{
		{
			"message": map[string]any{
				"role":       "assistant",
				"content":    resp.Content,
				"tool_calls": ToOpenAIToolsFromToolCalls(resp.ToolCalls),
			},
			"finish_reason": h.finishReason(resp),
		},
	}

	return map[string]any{
		"id":      fmt.Sprintf("chatcmpl-%d", time.Now().Unix()),
		"object":  "chat.completion",
		"created": time.Now().Unix(),
		"model":   h.config.Model,
		"choices": choices,
		"usage": map[string]any{
			"prompt_tokens":     resp.UsageTokens.PromptTokens,
			"completion_tokens": resp.UsageTokens.CompletionTokens,
			"total_tokens":      resp.UsageTokens.TotalTokens,
			"prompt_tokens_details": map[string]any{
				"cached_tokens": resp.UsageTokens.CachedTokens,
			},
		},
	}
}

func (h *Handler) buildStreamingChunk(resp llm.Response) map[string]any {
	return map[string]any{
		"id":      fmt.Sprintf("chatcmpl-%d", time.Now().Unix()),
		"object":  "chat.completion.chunk",
		"created": time.Now().Unix(),
		"model":   h.config.Model,
		"choices": []map[string]any{
			{
				"index": 0,
				"delta": map[string]any{
					"content":    resp.Content,
					"tool_calls": ToOpenAIToolsFromToolCalls(resp.ToolCalls),
				},
				"finish_reason": h.finishReason(resp),
			},
		},
	}
}

func (h *Handler) finishReason(resp llm.Response) string {
	if len(resp.ToolCalls) > 0 {
		return "tool_calls"
	}
	return resp.FinishReason
}

func ToOpenAIToolsFromToolCalls(toolCalls []llm.ToolCall) []map[string]any {
	if len(toolCalls) == 0 {
		return nil
	}

	result := make([]map[string]any, 0, len(toolCalls))
	for _, tc := range toolCalls {
		result = append(result, map[string]any{
			"id":   tc.ID,
			"type": "function",
			"function": map[string]any{
				"name":      tc.Name,
				"arguments": tc.Args,
			},
		})
	}
	return result
}

func (h *Handler) handleModels(w http.ResponseWriter, r *http.Request) {
	resp := map[string]any{
		"object": "list",
		"data": []map[string]any{
			{
				"id":       h.config.Model,
				"object":   "model",
				"created":  time.Now().Unix(),
				"owned_by": "local",
			},
		},
	}

	w.Header().Set("Content-Type", "application/json")
	// #nosec G104
	_ = json.NewEncoder(w).Encode(resp)
}

func (h *Handler) handleHealth(w http.ResponseWriter, r *http.Request) {
	w.Header().Set("Content-Type", "application/json")
	// #nosec G104
	_ = json.NewEncoder(w).Encode(map[string]any{
		"status":    "ok",
		"backend":   h.config.BackendURL,
		"model":     h.config.Model,
		"timestamp": time.Now().Unix(),
	})
}

type StreamingFlusher interface {
	Flush()
}
