package main

import (
	"context"
	"fmt"
	"log"

	"github.com/haikeilabs/agentware/go/executor"
	"github.com/haikeilabs/agentware/go/llm"
	"github.com/haikeilabs/agentware/go/middleware"
	"github.com/haikeilabs/agentware/go/tools"
)

// ExampleTool is a simple tool that echoes its input.
type ExampleTool struct{}

func (t *ExampleTool) Name() string        { return "echo" }
func (t *ExampleTool) Description() string { return "Echoes the input text back" }
func (t *ExampleTool) Execute(ctx context.Context, args map[string]any) (*tools.Result, error) {
	message, _ := args["message"].(string)
	return &tools.Result{
		Success: true,
		Output:  "Echo: " + message,
	}, nil
}

func main() {
	// 1. Create a tool registry and register tools
	registry := tools.NewToolRegistry()
	registry.Register(&ExampleTool{})

	// 2. Create an LLM backend (using OpenAI-compatible API)
	backend, err := llm.NewBackend(llm.Config{
		BaseURL:       "http://localhost:11434/v1",
		APIKey:        "not-needed",
		Model:         "llama3",
		ContextWindow: 8192,
	})
	if err != nil {
		log.Fatalf("Failed to create backend: %v", err)
	}

	// 3. Create a policy evaluator
	policy := &middleware.Policy{
		Rules: []middleware.Rule{
			{
				Name:   "allow-echo",
				Tools:  []string{"echo"},
				Action: middleware.ActionAllow,
			},
		},
		DefaultDeny: true,
	}

	// 4. Create an auditor
	auditor := middleware.NewInMemoryAuditor()

	// 5. Build the executor with all components
	exec := executor.NewDispatchExecutor(
		backend,
		registry,
		policy,
		auditor,
		"llama3",
	)

	// 6. Create a request
	req := executor.BuildRequest(
		registry,
		"Hello, please use the echo tool to say 'Hello World'",
		middleware.CallerContext{
			UserID:    "user-123",
			SessionID: "session-456",
			Trusted:   true,
		},
		"job-001",
		"You are a helpful assistant.",
	)

	// 7. Execute
	result, err := exec.Execute(context.Background(), req)
	if err != nil {
		log.Fatalf("Execution failed: %v", err)
	}

	fmt.Printf("Final Response: %s\n", result.FinalResponse)
	fmt.Printf("Iterations: %d\n", result.Iterations)
	fmt.Printf("Tool Calls Made: %d\n", result.ToolCallsMade)
	fmt.Printf("Termination: %s\n", result.TerminationReason)

	// 8. Check audit log
	records := auditor.Query(middleware.AuditFilter{})
	fmt.Printf("Audit records: %d\n", len(records))
	for _, r := range records {
		fmt.Printf("  - %s: %s -> %s\n", r.ToolName, r.Decision.Action, r.Decision.Rule)
	}
}
