// Package agent is a first Agentware agent: a model on any OpenAI-compatible
// endpoint, one Hacker News tool, a local rule that denies a second tool, and
// an audit record for every tool call. It needs no Kei account.
package agent

import (
	"context"
	"fmt"
	"net/http"
	"strings"
	"time"

	"github.com/haikeilabs/agentware/go/executor"
	"github.com/haikeilabs/agentware/go/llm"
	"github.com/haikeilabs/agentware/go/middleware"
	"github.com/haikeilabs/agentware/go/toolformat"
	"github.com/haikeilabs/agentware/go/tools"
)

// Prompt is the question the example asks.
const Prompt = "Summarize today's top five Hacker News stories."

// Source names this agent in the audit trail.
const Source = "first-agent"

const instructions = "You answer questions about Hacker News. " +
	"Use the tools to read live data, and cite each story with its URL."

// Config wires the agent. Backend and Model are required.
type Config struct {
	// Backend is the model endpoint, usually llm.NewBackend.
	Backend llm.Backend
	// Model picks the tool-call format for the model family.
	Model string
	// HNBaseURL defaults to the public Hacker News API.
	HNBaseURL string
	// HTTPClient calls the Hacker News API. Defaults to a 10s timeout.
	HTTPClient *http.Client
	// MaxIterations caps tool-call rounds per run. Defaults to 5.
	MaxIterations int

	// postComment lets tests observe whether hn_post_comment ever ran.
	postComment func(context.Context, map[string]any)
}

// Agent runs the loop and records every tool call.
type Agent struct {
	registry *tools.ToolRegistry
	client   *middleware.AuditedToolClient
	exec     executor.Executor
	caller   middleware.CallerContext
}

// Report is the result of one run.
type Report struct {
	// Answer is the model's final reply.
	Answer string
	// Audit holds the audit records written during this run.
	Audit []middleware.AuditRecord
}

// New builds the agent: the tools, the local policy, the audited tool client
// and the inference loop.
func New(cfg Config) *Agent {
	if cfg.HNBaseURL == "" {
		cfg.HNBaseURL = HNBaseURL
	}
	if cfg.HTTPClient == nil {
		cfg.HTTPClient = &http.Client{Timeout: 10 * time.Second}
	}
	if cfg.MaxIterations == 0 {
		cfg.MaxIterations = 5
	}

	registry := tools.NewToolRegistry()
	registry.Register(&topStoriesTool{baseURL: strings.TrimSuffix(cfg.HNBaseURL, "/"), client: cfg.HTTPClient})
	registry.Register(&postCommentTool{onExecute: cfg.postComment})

	// Every tool call goes through the audited client: the policy decides
	// first, the tool runs only when allowed, and the call is recorded either way.
	dispatch := tools.NewRegistryExecutor(registry)
	client := middleware.NewAuditedToolClient(Source, dispatch.Execute).WithPolicy(localPolicy())

	return &Agent{
		registry: registry,
		client:   client,
		exec: executor.NewInferenceExecutor(executor.InferenceExecutorConfig{
			Backend:       cfg.Backend,
			Registry:      registry,
			ToolExec:      client,
			Formatter:     toolformat.GetFormatter(cfg.Model),
			MaxIterations: cfg.MaxIterations,
		}),
		caller: middleware.CallerContext{UserID: "local-user", InvokingSubject: "local-user", Source: Source},
	}
}

// Run asks the model prompt and lets it call tools until it answers.
func (a *Agent) Run(ctx context.Context, prompt string) (*Report, error) {
	before := len(a.Audit())
	result, err := a.exec.Execute(ctx, executor.ExecuteRequest{
		SystemPrompt: executor.BuildSystemPrompt(a.registry, instructions),
		UserMessage:  prompt,
		CallerCtx:    a.caller,
	})
	if err != nil {
		return nil, fmt.Errorf("agent run: %w", err)
	}
	if result.TerminationReason != executor.TerminationComplete {
		return nil, fmt.Errorf("agent run stopped early: %s", result.TerminationReason)
	}
	return &Report{
		Answer: strings.TrimSpace(strings.ReplaceAll(result.FinalResponse, "TASK_COMPLETE", "")),
		Audit:  a.Audit()[before:],
	}, nil
}

// Call runs one tool through the same policy and audit path the model uses.
func (a *Agent) Call(ctx context.Context, tool string, args map[string]any) (*tools.Result, error) {
	return a.client.Execute(middleware.WithCallerContext(ctx, a.caller), tool, args)
}

// Audit returns every audit record written so far.
func (a *Agent) Audit() []middleware.AuditRecord {
	return a.client.Records(middleware.AuditFilter{})
}

// FormatAudit renders one audit record as a single line. The record holds a
// SHA-256 digest of the arguments, never the arguments themselves.
func FormatAudit(r middleware.AuditRecord) string {
	digest := r.ToolArgsDigest
	if len(digest) > 12 {
		digest = digest[:12]
	}
	return fmt.Sprintf("audit tool=%s decision=%s rule=%s success=%t args_sha256=%s latency_ms=%d",
		r.ToolName, r.Decision, r.PolicyID, r.Success, digest, r.LatencyMs)
}
