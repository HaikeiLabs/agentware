package main

import (
	"context"
	"flag"
	"fmt"
	"log"
	"os"

	"github.com/haikeilabs/agentware/go/llm/proxy"
)

func main() {
	config := proxy.DefaultConfig()

	flag.StringVar(&config.BackendURL, "backend-url", config.BackendURL, "LLM backend URL (llama-server, Ollama, etc)")
	flag.StringVar(&config.ListenAddr, "port", config.ListenAddr, "Proxy listen address")
	flag.StringVar(&config.Model, "model", config.Model, "Model name")
	flag.IntVar(&config.MaxRetries, "max-retries", config.MaxRetries, "Maximum retry attempts")
	flag.IntVar(&config.KeepRecent, "keep-recent", config.KeepRecent, "Recent messages to keep during compaction")
	flag.IntVar(&config.ContextWindow, "context-window", config.ContextWindow, "Context window size in tokens")
	flag.StringVar(&config.APIKey, "api-key", config.APIKey, "API key for backend")
	flag.Parse()

	if len(flag.Args()) > 0 && flag.Args()[0] == "version" {
		fmt.Println("pedro-proxy v0.1.0")
		os.Exit(0)
	}

	log.Printf("Starting pedro-proxy")
	log.Printf("  Backend: %s", config.BackendURL)
	log.Printf("  Listen:  %s", config.ListenAddr)
	log.Printf("  Model:   %s", config.Model)

	handler, err := proxy.NewHandler(config)
	if err != nil {
		log.Fatalf("Failed to create handler: %v", err)
	}

	server := proxy.NewServer(config, handler)

	log.Printf("Server ready. Press Ctrl+C to stop.")

	proxy.WaitForSignal()
	log.Println("Shutting down...")

	if err := server.Shutdown(context.Background()); err != nil {
		log.Printf("Shutdown error: %v", err)
	}
}
