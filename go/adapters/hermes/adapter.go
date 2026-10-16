package hermes

import (
	"context"
	"fmt"
	"sync"
	"time"

	"github.com/haikeilabs/agentware/go/middleware"
	"github.com/haikeilabs/agentware/go/tools"
)

type Client interface {
	Agent(name string) AgentHandle
}

type AgentHandle interface {
	Run(ctx context.Context, inputs map[string]any) (string, error)
	RunWithWait(ctx context.Context, inputs map[string]any, pollInterval time.Duration) (*Execution, error)
	GetExecution() (*Execution, error)
	Checkpoint(name string, data map[string]any) error
	RestoreCheckpoint(name string) (map[string]any, error)
	SaveArtifact(key string, data interface{}, artifactType string) error
	LoadArtifact(key string, dest interface{}) error
	Log(level LogLevel, message string, metadata map[string]any) error
	Replay(fromCheckpoint string, inputs map[string]any) (string, error)
	GetExecID() string
}

type Execution struct {
	ID        string
	Status    string
	StartedAt time.Time
	UpdatedAt time.Time
	Output    map[string]any
}

type LogLevel int

const (
	LogLevelDebug LogLevel = iota
	LogLevelInfo
	LogLevelWarning
	LogLevelError
)

type AgentToolExecutor struct {
	client        Client
	agentMapping  map[string]string
	defaultConfig *Config
	mu            sync.RWMutex
}

type Config struct {
	WaitInterval   time.Duration
	RequestTimeout time.Duration
	RetryCount     int
}

type agentTool struct {
	name        string
	description string
	executor    *AgentToolExecutor
}

func (t *agentTool) Name() string        { return t.name }
func (t *agentTool) Description() string { return t.description }
func (t *agentTool) Execute(ctx context.Context, args map[string]any) (*tools.Result, error) {
	return t.executor.Execute(ctx, t.name, args)
}
func (t *agentTool) InputSchema() map[string]any {
	return map[string]any{
		"type": "object",
		"properties": map[string]any{
			"inputs": map[string]any{
				"type":        "object",
				"description": "Agent input parameters",
			},
		},
	}
}
func (t *agentTool) Examples() []tools.ToolExample { return nil }

func NewAgentToolExecutor(client Client, agentMapping map[string]string) *AgentToolExecutor {
	return &AgentToolExecutor{
		client:       client,
		agentMapping: agentMapping,
		defaultConfig: &Config{
			WaitInterval:   2 * time.Second,
			RequestTimeout: 60 * time.Second,
			RetryCount:     3,
		},
	}
}

func NewAgentToolExecutorWithConfig(client Client, agentMapping map[string]string, config *Config) *AgentToolExecutor {
	if config == nil {
		config = &Config{
			WaitInterval:   2 * time.Second,
			RequestTimeout: 60 * time.Second,
			RetryCount:     3,
		}
	}
	return &AgentToolExecutor{
		client:        client,
		agentMapping:  agentMapping,
		defaultConfig: config,
	}
}

func (e *AgentToolExecutor) Execute(ctx context.Context, toolName string, args map[string]any) (*tools.Result, error) {
	agentName, ok := e.agentMapping[toolName]
	if !ok {
		return &tools.Result{
			Success: false,
			Error:   fmt.Sprintf("unknown tool: %s", toolName),
		}, nil
	}

	agent := e.client.Agent(agentName)
	config := e.getConfig()

	ctx, cancel := context.WithTimeout(ctx, config.RequestTimeout)
	defer cancel()

	exec, err := agent.RunWithWait(ctx, args, config.WaitInterval)
	if err != nil {
		return &tools.Result{
			Success: false,
			Error:   fmt.Sprintf("agent execution failed: %v", err),
			Metadata: map[string]any{
				"tool_name":  toolName,
				"agent_name": agentName,
			},
		}, err
	}

	return &tools.Result{
		Success: exec.Status == "completed",
		Output:  fmt.Sprintf("%v", exec.Output),
		Metadata: map[string]any{
			"execution_id": exec.ID,
			"status":       exec.Status,
			"tool_name":    toolName,
			"agent_name":   agentName,
		},
	}, nil
}

func (e *AgentToolExecutor) ListTools() []tools.Tool {
	toolsList := make([]tools.Tool, 0, len(e.agentMapping))
	for toolName, agentName := range e.agentMapping {
		toolsList = append(toolsList, &agentTool{
			name:        toolName,
			description: fmt.Sprintf("Execute Hermes agent: %s", agentName),
			executor:    e,
		})
	}
	return toolsList
}

func (e *AgentToolExecutor) GetAgentMapping() map[string]string {
	e.mu.RLock()
	defer e.mu.RUnlock()
	result := make(map[string]string, len(e.agentMapping))
	for k, v := range e.agentMapping {
		result[k] = v
	}
	return result
}

func (e *AgentToolExecutor) UpdateAgentMapping(mapping map[string]string) {
	e.mu.Lock()
	defer e.mu.Unlock()
	e.agentMapping = mapping
}

func (e *AgentToolExecutor) AddAgentMapping(toolName, agentName string) {
	e.mu.Lock()
	defer e.mu.Unlock()
	e.agentMapping[toolName] = agentName
}

func (e *AgentToolExecutor) getConfig() *Config {
	e.mu.RLock()
	defer e.mu.RUnlock()
	return e.defaultConfig
}

func (e *AgentToolExecutor) SetConfig(config *Config) {
	e.mu.Lock()
	defer e.mu.Unlock()
	e.defaultConfig = config
}

type HermesMiddlewareAdapter struct {
	executor *AgentToolExecutor
	policy   middleware.PolicyEvaluator
	auditor  middleware.Auditor
}

func NewHermesMiddlewareAdapter(
	client Client,
	agentMapping map[string]string,
	policy middleware.PolicyEvaluator,
	auditor middleware.Auditor,
) *HermesMiddlewareAdapter {
	executor := NewAgentToolExecutor(client, agentMapping)
	return &HermesMiddlewareAdapter{
		executor: executor,
		policy:   policy,
		auditor:  auditor,
	}
}

func (a *HermesMiddlewareAdapter) Execute(ctx context.Context, toolName string, args map[string]any) (*tools.Result, error) {
	caller := getCallerContext(ctx)

	var decision middleware.Decision
	if a.policy != nil {
		decision = a.policy.Evaluate(toolName, args, caller)
	} else {
		decision = middleware.Decision{Action: middleware.ActionAllow, Reason: "no policy configured"}
	}

	if a.auditor != nil {
		a.auditor.Record(middleware.AuditRecord{
			InvokedAt:       time.Now(),
			InvokingSubject: caller.InvokingSubject,
			ParentSpan:      caller.ParentSpan,
			DelegationDepth: caller.DelegationDepth,
			Framework:       "hermes",
			ToolName:        toolName,
			Decision:        string(decision.Action),
			PolicyID:        decision.Rule,
		})
	}

	if decision.Action == middleware.ActionDeny {
		return &tools.Result{
			Success: false,
			Error:   fmt.Sprintf("denied by policy: %s", decision.Reason),
			Metadata: map[string]any{
				"rule": decision.Rule,
			},
		}, nil
	}

	return a.executor.Execute(ctx, toolName, args)
}

func (a *HermesMiddlewareAdapter) ListTools() []tools.Tool {
	return a.executor.ListTools()
}

func (a *HermesMiddlewareAdapter) WithPolicy(policy middleware.PolicyEvaluator) *HermesMiddlewareAdapter {
	a.policy = policy
	return a
}

func (a *HermesMiddlewareAdapter) WithAuditor(auditor middleware.Auditor) *HermesMiddlewareAdapter {
	a.auditor = auditor
	return a
}

func (a *HermesMiddlewareAdapter) GetAuditor() middleware.Auditor {
	return a.auditor
}

func getCallerContext(ctx context.Context) middleware.CallerContext {
	if c, ok := middleware.CallerFromContext(ctx); ok {
		return c
	}
	// Fail-closed: a missing caller context is untrusted, never trusted.
	return middleware.CallerContext{
		Trusted: false,
	}
}

func WithCallerContext(ctx context.Context, caller middleware.CallerContext) context.Context {
	return middleware.WithCallerContext(ctx, caller)
}
