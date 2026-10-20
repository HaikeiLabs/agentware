package main

import (
	"context"
	"flag"
	"fmt"
	"log"

	"github.com/haikeilabs/agentware/examples/go/minimal-harness/harness"
	"github.com/haikeilabs/agentware/go/kei/runtimesocket"
)

func main() {
	socketPath := flag.String("socket", "", "path to the kei-proxy Unix socket (overrides KEI_RUNTIME_SOCKET_PATH)")
	flag.Parse()

	source := "minimal-harness"
	if args := flag.Args(); len(args) > 0 {
		source = args[0]
	}

	opts := runtimesocket.SelectOptions{}
	if *socketPath != "" {
		opts.SocketPath = *socketPath
	}

	client, err := runtimesocket.SelectAuthorizationClient(source, opts)
	if err != nil {
		log.Fatalf("selecting auth client: %v", err)
	}

	h := harness.New(client, source)
	h.Register("echo", func(ctx context.Context, args map[string]any) (map[string]any, error) {
		msg, _ := args["message"].(string)
		return map[string]any{"echo": msg}, nil
	})
	deleteHandlerRan := false
	h.Register("delete_everything", func(ctx context.Context, args map[string]any) (map[string]any, error) {
		deleteHandlerRan = true
		return map[string]any{"deleted": true}, nil
	})

	result, allowDecision, err := h.ExecuteWithDecision(context.Background(), "user-123", "echo", map[string]any{"message": "hello harness"})
	if err != nil {
		log.Fatalf("execute: %v", err)
	}
	fmt.Printf("ALLOW tool=echo policy_id=%s result=%v\n", allowDecision.Rule, result)
	_, denyDecision, err := h.ExecuteWithDecision(context.Background(), "user-123", "delete_everything", nil)
	if err == nil {
		log.Fatal("expected delete_everything to be denied")
	}
	if deleteHandlerRan {
		log.Fatal("denied delete_everything handler ran")
	}
	fmt.Printf("DENY tool=delete_everything reason_code=%s hint=%s handler=not_run\n", denyDecision.ReasonCode, denyDecision.Hint)
}
