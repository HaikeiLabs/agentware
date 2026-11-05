// Command first-agent asks a model to summarize today's top Hacker News
// stories, then shows a local rule denying hn_post_comment. The logic lives
// in the agent package; this file only wires it up.
package main

import (
	"context"
	"fmt"
	"log"
	"os"
	"os/signal"

	"github.com/haikeilabs/agentware/examples/go/first-agent/agent"
	"github.com/haikeilabs/agentware/go/llm"
)

func main() {
	settings, err := agent.SettingsFromEnv(os.Getenv)
	if err != nil {
		log.Fatal(err)
	}
	backend, err := llm.NewBackend(llm.Config{
		BaseURL: settings.BaseURL,
		APIKey:  settings.APIKey,
		Model:   settings.Model,
		Timeout: agent.ModelTimeout,
	})
	if err != nil {
		log.Fatal(err)
	}

	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt)
	defer stop()

	a := agent.New(agent.Config{Backend: backend, Model: settings.Model})

	report, err := a.Run(ctx, agent.Prompt)
	if err != nil {
		log.Fatal(err)
	}
	fmt.Printf("> %s\n\n%s\n\n", agent.Prompt, report.Answer)
	for _, r := range report.Audit {
		fmt.Println(agent.FormatAudit(r))
	}

	// The local rule in agent/policy.go denies hn_post_comment.
	// The tool never runs, and the denial is audited like any other call.
	result, err := a.Call(ctx, agent.ToolPostComment, map[string]any{"story_id": 1, "text": "Nice post"})
	if err != nil {
		log.Fatal(err)
	}
	fmt.Printf("\n%s: %s\n", agent.ToolPostComment, result.Error)
	audit := a.Audit()
	fmt.Println(agent.FormatAudit(audit[len(audit)-1]))
}
