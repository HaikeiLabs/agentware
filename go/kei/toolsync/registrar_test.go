package toolsync

import (
	"context"
	"encoding/json"
	"errors"
	"reflect"
	"strings"
	"testing"

	"github.com/haikeilabs/agentware/go/toolspec"
)

type captureToolManifest struct{ data []byte }

func (c *captureToolManifest) SendTools(_ context.Context, data []byte) error {
	c.data = append([]byte(nil), data...)
	return nil
}

func TestSocketRegistrarSerializesToolSpecsBeforeSending(t *testing.T) {
	client := &captureToolManifest{}
	spec := toolspec.ToolSpec{
		Name: "github.get_issue", Description: "Fetch an issue from a GitHub repository",
		Parameters: map[string]any{"type": "object", "properties": map[string]any{"issue_number": map[string]any{"type": "integer", "minimum": 1}}, "required": []any{"issue_number"}},
		Service:    "github", Source: "github", OperationClass: "read",
		Route:                &toolspec.Route{ConnectorBinding: &toolspec.ConnectorBinding{AgentID: "11111111-1111-4111-8111-111111111111", ConnectorID: "22222222-2222-4222-8222-222222222222"}},
		RequiredCapabilities: []string{"issue.read"}, ResourceTypes: []toolspec.ResourceType{{Type: "issue", ParentType: "repository"}},
	}
	registrar := SocketRegistrar{Client: client}
	if err := registrar.Send(context.Background(), []toolspec.ToolSpec{spec}); err != nil {
		t.Fatal(err)
	}
	var got, want any
	if err := json.Unmarshal(client.data, &got); err != nil {
		t.Fatal(err)
	}
	if err := json.Unmarshal([]byte(`{"schema":"kei.tool-manifest/v3","tools":[{"name":"github.get_issue","service":"github","source":"github","operation_class":"read","route":{"connector_binding":{"agent_id":"11111111-1111-4111-8111-111111111111","connector_id":"22222222-2222-4222-8222-222222222222"}},"required_capabilities":["issue.read"],"resource_types":[{"type":"issue","parent_type":"repository"}],"description":"Fetch an issue from a GitHub repository","enabled":true}]}`), &want); err != nil {
		t.Fatal(err)
	}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("serialized manifest mismatch\ngot:  %s", client.data)
	}
}

func TestSocketRegistrarRejectsInvalidSpecWithoutSending(t *testing.T) {
	client := &captureToolManifest{}
	err := (SocketRegistrar{Client: client}).Send(context.Background(), []toolspec.ToolSpec{{Name: "incomplete", Description: "Missing route", Service: "s", Source: "s", OperationClass: "read"}})
	if err == nil || !strings.Contains(err.Error(), "route is required") {
		t.Fatalf("got %v", err)
	}
	if len(client.data) != 0 {
		t.Fatal("sent invalid ToolSpec")
	}
}

func TestSocketRegistrarDoesNotSendEmptyManifest(t *testing.T) {
	client := &captureToolManifest{}
	err := (SocketRegistrar{Client: client}).Send(context.Background(), nil)
	if err == nil || !errors.Is(err, ErrEmptyManifest) {
		t.Fatalf("got %v, want ErrEmptyManifest", err)
	}
	if len(client.data) != 0 {
		t.Fatal("sent an empty manifest")
	}
}
