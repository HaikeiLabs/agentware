package proxy

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/haikeilabs/agentware/go/middleware/guardrails"
)

func TestIntegration_HealthEndpoint(t *testing.T) {
	handler := &Handler{
		config: &ProxyConfig{
			BackendURL: "http://localhost:8080",
			Model:      "test-model",
		},
	}

	server := httptest.NewServer(http.HandlerFunc(handler.handleHealth))
	defer server.Close()

	resp, err := http.Get(server.URL + "/health")
	if err != nil {
		t.Fatalf("request failed: %v", err)
	}
	defer resp.Body.Close() //nolint:errcheck

	if resp.StatusCode != http.StatusOK {
		t.Fatalf("expected status 200, got %d", resp.StatusCode)
	}

	var result map[string]any
	json.NewDecoder(resp.Body).Decode(&result) //nolint:errcheck

	if result["status"] != "ok" {
		t.Errorf("expected status 'ok', got %v", result["status"])
	}
	if result["model"] != "test-model" {
		t.Errorf("expected model 'test-model', got %v", result["model"])
	}
}

func TestIntegration_ModelsEndpoint(t *testing.T) {
	handler := &Handler{
		config: &ProxyConfig{
			Model: "my-model",
		},
	}

	server := httptest.NewServer(http.HandlerFunc(handler.handleModels))
	defer server.Close()

	resp, err := http.Get(server.URL + "/v1/models")
	if err != nil {
		t.Fatalf("request failed: %v", err)
	}
	defer resp.Body.Close() //nolint:errcheck

	if resp.StatusCode != http.StatusOK {
		t.Fatalf("expected status 200, got %d", resp.StatusCode)
	}

	var result map[string]any
	json.NewDecoder(resp.Body).Decode(&result) //nolint:errcheck

	data := result["data"].([]any)
	if len(data) == 0 {
		t.Fatal("no models in response")
	}

	modelID := data[0].(map[string]any)["id"].(string)
	if modelID != "my-model" {
		t.Errorf("expected model 'my-model', got %s", modelID)
	}
}

func TestIntegration_InvalidRequest(t *testing.T) {
	handler := &Handler{
		config: &ProxyConfig{
			Model:         "test-model",
			ContextWindow: 32768,
		},
		validator:    guardrails.NewResponseValidator(nil, true),
		errorTracker: guardrails.NewErrorTracker(),
	}

	server := httptest.NewServer(http.HandlerFunc(handler.handleChatCompletions))
	defer server.Close()

	resp, err := http.Post(server.URL+"/v1/chat/completions", "application/json", nil)
	if err != nil {
		t.Fatalf("request failed: %v", err)
	}
	defer resp.Body.Close() //nolint:errcheck

	if resp.StatusCode != http.StatusBadRequest {
		t.Errorf("expected 400, got %d", resp.StatusCode)
	}
}
