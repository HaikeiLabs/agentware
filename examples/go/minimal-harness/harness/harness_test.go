package harness_test

import (
	"context"
	"testing"

	"github.com/haikeilabs/agentware/examples/go/minimal-harness/harness"
	"github.com/haikeilabs/agentware/go/kei/evaluator"
)

type allowClient struct{}

func (allowClient) Authorize(ctx context.Context, req evaluator.AuthorizeRequest) (map[string]any, error) {
	return map[string]any{"decision": "allow"}, nil
}

type denyClient struct{}

func (denyClient) Authorize(ctx context.Context, req evaluator.AuthorizeRequest) (map[string]any, error) {
	return map[string]any{"decision": "deny"}, nil
}

func TestExecute_Allow(t *testing.T) {
	h := harness.New(allowClient{}, "test")
	h.Register("echo", func(ctx context.Context, args map[string]any) (map[string]any, error) {
		return map[string]any{"echo": args["message"]}, nil
	})
	result, err := h.Execute(context.Background(), "user-1", "echo", map[string]any{"message": "hello"})
	if err != nil {
		t.Fatalf("expected allow, got error: %v", err)
	}
	if result["echo"] != "hello" {
		t.Fatalf("expected echo=hello, got %v", result)
	}
}

func TestExecute_Deny(t *testing.T) {
	h := harness.New(denyClient{}, "test")
	h.Register("echo", func(ctx context.Context, args map[string]any) (map[string]any, error) {
		return map[string]any{"echo": args["message"]}, nil
	})
	_, err := h.Execute(context.Background(), "user-1", "echo", map[string]any{"message": "hello"})
	if err == nil {
		t.Fatal("expected deny error, got nil")
	}
}

func TestExecute_DefaultDeny(t *testing.T) {
	h := harness.New(unknownClient{}, "test")
	h.Register("echo", func(ctx context.Context, args map[string]any) (map[string]any, error) {
		return map[string]any{"echo": args["message"]}, nil
	})
	_, err := h.Execute(context.Background(), "user-1", "echo", map[string]any{"message": "hello"})
	if err == nil {
		t.Fatal("expected deny for unknown decision, got nil")
	}
}

func TestExecute_UnknownTool(t *testing.T) {
	h := harness.New(allowClient{}, "test")
	_, err := h.Execute(context.Background(), "user-1", "nonexistent", nil)
	if err == nil {
		t.Fatal("expected error for unknown tool, got nil")
	}
}

type unknownClient struct{}

func (unknownClient) Authorize(ctx context.Context, req evaluator.AuthorizeRequest) (map[string]any, error) {
	return map[string]any{"decision": "unknown"}, nil
}
