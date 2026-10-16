package hermes

import (
	"context"
	"errors"
	"testing"
	"time"

	"github.com/haikeilabs/agentware/go/middleware"
)

type mockAgentHandle struct {
	execID    string
	execution *Execution
	err       error
	runCalled bool
}

func (m *mockAgentHandle) Run(ctx context.Context, inputs map[string]any) (string, error) {
	m.runCalled = true
	if m.err != nil {
		return "", m.err
	}
	if m.execID == "" {
		m.execID = "exec-123"
	}
	return m.execID, nil
}

func (m *mockAgentHandle) RunWithWait(ctx context.Context, inputs map[string]any, pollInterval time.Duration) (*Execution, error) {
	select {
	case <-ctx.Done():
		return nil, ctx.Err()
	default:
	}
	if m.err != nil {
		return nil, m.err
	}
	if m.execution != nil {
		return m.execution, nil
	}
	if m.execID == "" {
		m.execID = "exec-123"
	}
	return &Execution{ID: m.execID, Status: "completed", Output: map[string]any{"result": "success"}}, nil
}

func (m *mockAgentHandle) GetExecution() (*Execution, error) {
	if m.err != nil {
		return nil, m.err
	}
	if m.execution != nil {
		return m.execution, nil
	}
	if m.execID == "" {
		m.execID = "exec-123"
	}
	return &Execution{ID: m.execID, Status: "completed"}, nil
}

func (m *mockAgentHandle) Checkpoint(name string, data map[string]any) error {
	return m.err
}

func (m *mockAgentHandle) RestoreCheckpoint(name string) (map[string]any, error) {
	return nil, m.err
}

func (m *mockAgentHandle) SaveArtifact(key string, data interface{}, artifactType string) error {
	return m.err
}

func (m *mockAgentHandle) LoadArtifact(key string, dest interface{}) error {
	return m.err
}

func (m *mockAgentHandle) Log(level LogLevel, message string, metadata map[string]any) error {
	return m.err
}

func (m *mockAgentHandle) Replay(fromCheckpoint string, inputs map[string]any) (string, error) {
	return "", m.err
}

func (m *mockAgentHandle) GetExecID() string {
	return m.execID
}

type mockClient struct {
	agentHandle *mockAgentHandle
	agentName   string
}

func (m *mockClient) Agent(name string) AgentHandle {
	m.agentName = name
	if m.agentHandle == nil {
		m.agentHandle = &mockAgentHandle{
			execID:    "exec-123",
			execution: &Execution{ID: "exec-123", Status: "completed", Output: map[string]any{"result": "success"}},
		}
	}
	return m.agentHandle
}

type mockPolicy struct {
	decision middleware.Decision
}

func (m *mockPolicy) Evaluate(toolName string, args map[string]any, caller middleware.CallerContext) middleware.Decision {
	return m.decision
}

func TestNewAgentToolExecutor(t *testing.T) {
	client := &mockClient{}
	mapping := map[string]string{"tool1": "agent1"}

	executor := NewAgentToolExecutor(client, mapping)

	if executor.client != client {
		t.Error("expected client to be set")
	}
	if executor.defaultConfig == nil {
		t.Error("expected default config to be set")
	}
	if executor.defaultConfig.WaitInterval != 2*time.Second {
		t.Errorf("expected WaitInterval 2s, got %v", executor.defaultConfig.WaitInterval)
	}
	if executor.defaultConfig.RequestTimeout != 60*time.Second {
		t.Errorf("expected RequestTimeout 60s, got %v", executor.defaultConfig.RequestTimeout)
	}
	if executor.defaultConfig.RetryCount != 3 {
		t.Errorf("expected RetryCount 3, got %v", executor.defaultConfig.RetryCount)
	}
}

func TestNewAgentToolExecutorWithConfig(t *testing.T) {
	client := &mockClient{}
	mapping := map[string]string{"tool1": "agent1"}
	config := &Config{
		WaitInterval:   5 * time.Second,
		RequestTimeout: 30 * time.Second,
		RetryCount:     5,
	}

	executor := NewAgentToolExecutorWithConfig(client, mapping, config)

	if executor.defaultConfig.WaitInterval != 5*time.Second {
		t.Errorf("expected WaitInterval 5s, got %v", executor.defaultConfig.WaitInterval)
	}
	if executor.defaultConfig.RequestTimeout != 30*time.Second {
		t.Errorf("expected RequestTimeout 30s, got %v", executor.defaultConfig.RequestTimeout)
	}
	if executor.defaultConfig.RetryCount != 5 {
		t.Errorf("expected RetryCount 5, got %v", executor.defaultConfig.RetryCount)
	}
}

func TestNewAgentToolExecutorWithNilConfig(t *testing.T) {
	client := &mockClient{}
	mapping := map[string]string{"tool1": "agent1"}

	executor := NewAgentToolExecutorWithConfig(client, mapping, nil)

	if executor.defaultConfig.WaitInterval != 2*time.Second {
		t.Errorf("expected default WaitInterval, got %v", executor.defaultConfig.WaitInterval)
	}
}

func TestAgentToolExecutor_Execute_Success(t *testing.T) {
	client := &mockClient{}
	mapping := map[string]string{"my_tool": "my_agent"}
	executor := NewAgentToolExecutor(client, mapping)

	result, err := executor.Execute(context.Background(), "my_tool", map[string]any{"key": "value"})

	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if !result.Success {
		t.Error("expected success")
	}
	if result.Output != "map[result:success]" {
		t.Errorf("expected output 'map[result:success]', got '%s'", result.Output)
	}
	if result.Metadata["tool_name"] != "my_tool" {
		t.Errorf("expected tool_name 'my_tool', got '%v'", result.Metadata["tool_name"])
	}
	if result.Metadata["agent_name"] != "my_agent" {
		t.Errorf("expected agent_name 'my_agent', got '%v'", result.Metadata["agent_name"])
	}
}

func TestAgentToolExecutor_Execute_UnknownTool(t *testing.T) {
	client := &mockClient{}
	mapping := map[string]string{"my_tool": "my_agent"}
	executor := NewAgentToolExecutor(client, mapping)

	result, err := executor.Execute(context.Background(), "unknown_tool", nil)

	if err != nil {
		t.Fatal("expected no error for unknown tool")
	}
	if result.Success {
		t.Error("expected failure for unknown tool")
	}
	if result.Error != "unknown tool: unknown_tool" {
		t.Errorf("expected error 'unknown tool: unknown_tool', got '%s'", result.Error)
	}
}

func TestAgentToolExecutor_Execute_FailedStatus(t *testing.T) {
	client := &mockClient{}
	mapping := map[string]string{"my_tool": "my_agent"}
	executor := NewAgentToolExecutor(client, mapping)

	client.Agent("my_agent")
	client.agentHandle.execution = &Execution{ID: "exec-123", Status: "failed", Output: map[string]any{"result": "fail"}}

	result, err := executor.Execute(context.Background(), "my_tool", nil)

	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if result.Success {
		t.Error("expected failure for failed status")
	}
}

func TestAgentToolExecutor_Execute_RunError(t *testing.T) {
	client := &mockClient{}
	mapping := map[string]string{"my_tool": "my_agent"}
	executor := NewAgentToolExecutor(client, mapping)

	client.Agent("my_agent")
	client.agentHandle.err = errors.New("run failed")

	result, err := executor.Execute(context.Background(), "my_tool", nil)

	if err == nil {
		t.Fatal("expected error")
	}
	if result.Success {
		t.Error("expected failure")
	}
}

func TestAgentToolExecutor_ListTools(t *testing.T) {
	client := &mockClient{}
	mapping := map[string]string{
		"tool1": "agent1",
		"tool2": "agent2",
	}
	executor := NewAgentToolExecutor(client, mapping)

	toolsList := executor.ListTools()

	if len(toolsList) != 2 {
		t.Errorf("expected 2 tools, got %d", len(toolsList))
	}

	toolNames := make(map[string]bool)
	for _, tool := range toolsList {
		toolNames[tool.Name()] = true
		if tool.Description() == "" {
			t.Error("expected non-empty description")
		}
	}

	if !toolNames["tool1"] {
		t.Error("expected tool1")
	}
	if !toolNames["tool2"] {
		t.Error("expected tool2")
	}
}

func TestAgentToolExecutor_GetAgentMapping(t *testing.T) {
	client := &mockClient{}
	mapping := map[string]string{"tool1": "agent1", "tool2": "agent2"}
	executor := NewAgentToolExecutor(client, mapping)

	result := executor.GetAgentMapping()

	if result["tool1"] != "agent1" {
		t.Errorf("expected agent1, got %s", result["tool1"])
	}
	if result["tool2"] != "agent2" {
		t.Errorf("expected agent2, got %s", result["tool2"])
	}

	result["tool1"] = "modified"
	if executor.GetAgentMapping()["tool1"] == "modified" {
		t.Error("expected copy, not modification of original")
	}
}

func TestAgentToolExecutor_UpdateAgentMapping(t *testing.T) {
	client := &mockClient{}
	executor := NewAgentToolExecutor(client, map[string]string{"tool1": "agent1"})

	executor.UpdateAgentMapping(map[string]string{"new_tool": "new_agent"})

	if executor.GetAgentMapping()["new_tool"] != "new_agent" {
		t.Error("expected new_tool mapping")
	}
	if executor.GetAgentMapping()["tool1"] != "" {
		t.Error("expected old tool to be removed")
	}
}

func TestAgentToolExecutor_AddAgentMapping(t *testing.T) {
	client := &mockClient{}
	executor := NewAgentToolExecutor(client, map[string]string{"tool1": "agent1"})

	executor.AddAgentMapping("tool2", "agent2")

	if executor.GetAgentMapping()["tool2"] != "agent2" {
		t.Error("expected tool2 mapping")
	}
	if executor.GetAgentMapping()["tool1"] != "agent1" {
		t.Error("expected tool1 to still exist")
	}
}

func TestAgentToolExecutor_SetConfig(t *testing.T) {
	client := &mockClient{}
	executor := NewAgentToolExecutor(client, nil)

	newConfig := &Config{WaitInterval: 10 * time.Second}
	executor.SetConfig(newConfig)

	result := executor.getConfig()
	if result.WaitInterval != 10*time.Second {
		t.Errorf("expected 10s, got %v", result.WaitInterval)
	}
}

func TestAgentTool_Name(t *testing.T) {
	client := &mockClient{}
	executor := NewAgentToolExecutor(client, map[string]string{"my_tool": "my_agent"})
	toolsList := executor.ListTools()

	if toolsList[0].Name() != "my_tool" {
		t.Errorf("expected 'my_tool', got '%s'", toolsList[0].Name())
	}
}

func TestAgentTool_Description(t *testing.T) {
	client := &mockClient{}
	executor := NewAgentToolExecutor(client, map[string]string{"my_tool": "my_agent"})
	toolsList := executor.ListTools()

	desc := toolsList[0].Description()
	if desc != "Execute Hermes agent: my_agent" {
		t.Errorf("expected 'Execute Hermes agent: my_agent', got '%s'", desc)
	}
}

func TestAgentTool_Execute(t *testing.T) {
	client := &mockClient{}
	executor := NewAgentToolExecutor(client, map[string]string{"my_tool": "my_agent"})
	toolsList := executor.ListTools()

	result, err := toolsList[0].Execute(context.Background(), map[string]any{"key": "value"})

	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if !result.Success {
		t.Error("expected success")
	}
}

func TestAgentTool_InputSchema(t *testing.T) {
	client := &mockClient{}
	executor := NewAgentToolExecutor(client, map[string]string{"my_tool": "my_agent"})
	toolsList := executor.ListTools()

	at, ok := toolsList[0].(*agentTool)
	if !ok {
		t.Fatal("expected agentTool type")
	}

	schema := at.InputSchema()

	if schema["type"] != "object" {
		t.Errorf("expected type 'object', got '%v'", schema["type"])
	}
}

func TestAgentTool_Examples(t *testing.T) {
	client := &mockClient{}
	executor := NewAgentToolExecutor(client, map[string]string{"my_tool": "my_agent"})
	toolsList := executor.ListTools()

	at, ok := toolsList[0].(*agentTool)
	if !ok {
		t.Fatal("expected agentTool type")
	}

	examples := at.Examples()

	if examples != nil {
		t.Error("expected nil examples")
	}
}

func TestNewHermesMiddlewareAdapter(t *testing.T) {
	client := &mockClient{}
	mapping := map[string]string{"tool1": "agent1"}
	policy := &mockPolicy{}
	auditor := middleware.NewInMemoryAuditor()

	adapter := NewHermesMiddlewareAdapter(client, mapping, policy, auditor)

	if adapter.executor == nil {
		t.Error("expected executor to be set")
	}
	if adapter.policy != policy {
		t.Error("expected policy to be set")
	}
	if adapter.auditor != auditor {
		t.Error("expected auditor to be set")
	}
}

func TestHermesMiddlewareAdapter_Execute_WithAllowPolicy(t *testing.T) {
	client := &mockClient{}
	mapping := map[string]string{"my_tool": "my_agent"}
	policy := &mockPolicy{decision: middleware.Decision{Action: middleware.ActionAllow}}
	auditor := middleware.NewInMemoryAuditor()

	adapter := NewHermesMiddlewareAdapter(client, mapping, policy, auditor)

	result, err := adapter.Execute(context.Background(), "my_tool", map[string]any{"key": "value"})

	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if !result.Success {
		t.Error("expected success")
	}
	records := auditor.Query(middleware.AuditFilter{ToolName: "my_tool"})
	if len(records) != 1 {
		t.Errorf("expected 1 audit record, got %d", len(records))
	}
}

func TestHermesMiddlewareAdapter_Execute_WithDenyPolicy(t *testing.T) {
	client := &mockClient{}
	mapping := map[string]string{"my_tool": "my_agent"}
	policy := &mockPolicy{decision: middleware.Decision{
		Action: middleware.ActionDeny,
		Rule:   "deny-rule",
		Reason: "denied by policy",
	}}
	auditor := middleware.NewInMemoryAuditor()

	adapter := NewHermesMiddlewareAdapter(client, mapping, policy, auditor)

	result, err := adapter.Execute(context.Background(), "my_tool", map[string]any{"key": "value"})

	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if result.Success {
		t.Error("expected failure due to policy deny")
	}
	if result.Error != "denied by policy: denied by policy" {
		t.Errorf("expected deny error, got '%s'", result.Error)
	}
	if result.Metadata["rule"] != "deny-rule" {
		t.Errorf("expected rule 'deny-rule', got '%v'", result.Metadata["rule"])
	}
}

func TestHermesMiddlewareAdapter_Execute_WithNoPolicy(t *testing.T) {
	client := &mockClient{}
	mapping := map[string]string{"my_tool": "my_agent"}
	auditor := middleware.NewInMemoryAuditor()

	adapter := NewHermesMiddlewareAdapter(client, mapping, nil, auditor)

	result, err := adapter.Execute(context.Background(), "my_tool", nil)

	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if !result.Success {
		t.Error("expected success when no policy")
	}
}

func TestHermesMiddlewareAdapter_Execute_WithNoAuditor(t *testing.T) {
	client := &mockClient{}
	mapping := map[string]string{"my_tool": "my_agent"}
	policy := &mockPolicy{decision: middleware.Decision{Action: middleware.ActionAllow}}

	adapter := NewHermesMiddlewareAdapter(client, mapping, policy, nil)

	result, err := adapter.Execute(context.Background(), "my_tool", nil)

	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if !result.Success {
		t.Error("expected success")
	}
}

func TestHermesMiddlewareAdapter_ListTools(t *testing.T) {
	client := &mockClient{}
	mapping := map[string]string{"tool1": "agent1", "tool2": "agent2"}
	adapter := NewHermesMiddlewareAdapter(client, mapping, nil, nil)

	toolsList := adapter.ListTools()

	if len(toolsList) != 2 {
		t.Errorf("expected 2 tools, got %d", len(toolsList))
	}
}

func TestHermesMiddlewareAdapter_WithPolicy(t *testing.T) {
	client := &mockClient{}
	adapter := NewHermesMiddlewareAdapter(client, nil, nil, nil)

	newPolicy := &mockPolicy{}
	resultAdapter := adapter.WithPolicy(newPolicy)

	if resultAdapter.policy != newPolicy {
		t.Error("expected policy to be set")
	}
	if resultAdapter != adapter {
		t.Error("expected same adapter returned for chaining")
	}
}

func TestHermesMiddlewareAdapter_WithAuditor(t *testing.T) {
	client := &mockClient{}
	adapter := NewHermesMiddlewareAdapter(client, nil, nil, nil)

	newAuditor := middleware.NewInMemoryAuditor()
	resultAdapter := adapter.WithAuditor(newAuditor)

	if resultAdapter.auditor != newAuditor {
		t.Error("expected auditor to be set")
	}
	if resultAdapter != adapter {
		t.Error("expected same adapter returned for chaining")
	}
}

func TestHermesMiddlewareAdapter_GetAuditor(t *testing.T) {
	client := &mockClient{}
	auditor := middleware.NewInMemoryAuditor()
	adapter := NewHermesMiddlewareAdapter(client, nil, nil, auditor)

	result := adapter.GetAuditor()

	if result != auditor {
		t.Error("expected same auditor")
	}
}

func TestGetCallerContext_WithContext(t *testing.T) {
	ctx := context.Background()
	callerCtx := middleware.CallerContext{
		Trusted: false,
		Role:    "user",
		UserID:  "user-123",
	}
	ctx = WithCallerContext(ctx, callerCtx)

	result := getCallerContext(ctx)

	if result.Trusted {
		t.Error("expected trusted to be false")
	}
	if result.Role != "user" {
		t.Errorf("expected role 'user', got '%s'", result.Role)
	}
	if result.UserID != "user-123" {
		t.Errorf("expected user_id 'user-123', got '%s'", result.UserID)
	}
}

func TestGetCallerContext_WithoutContext(t *testing.T) {
	ctx := context.Background()

	result := getCallerContext(ctx)

	if result.Trusted {
		t.Error("expected default trusted to be false (fail-closed)")
	}
}

func TestWithCallerContext(t *testing.T) {
	ctx := context.Background()
	callerCtx := middleware.CallerContext{
		Trusted:   true,
		SessionID: "session-123",
	}

	resultCtx := WithCallerContext(ctx, callerCtx)
	result, ok := middleware.CallerFromContext(resultCtx)
	if !ok {
		t.Fatal("expected caller context to be present")
	}

	if result.Trusted != true {
		t.Error("expected trusted to be true")
	}
	if result.SessionID != "session-123" {
		t.Error("expected session-123")
	}
}

func TestClientAgentNameTracking(t *testing.T) {
	client := &mockClient{}
	executor := NewAgentToolExecutor(client, map[string]string{"my_tool": "my_agent"})

	_, _ = executor.Execute(context.Background(), "my_tool", nil)

	if client.agentName != "my_agent" {
		t.Errorf("expected agentName 'my_agent', got '%s'", client.agentName)
	}
}
