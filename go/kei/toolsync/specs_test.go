package toolsync

import (
	"context"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"sync"
	"testing"

	"github.com/haikeilabs/agentware/go/toolspec"
)

var specsFixture = []toolspec.ToolSpec{
	{
		Name: "local.echo", Description: "Echo text back", Service: "codex", Source: "codex",
		OperationClass: "write",
		Route:          &toolspec.Route{HarnessExecutor: &toolspec.HarnessExecutor{Executor: "codex", Registration: "local.echo"}},
	},
	{
		Name: "github.get_issue", Description: "Fetch an issue", Service: "github", Source: "github",
		OperationClass:       "read",
		Route:                &toolspec.Route{ConnectorBinding: &toolspec.ConnectorBinding{AgentID: "a", ConnectorID: "c"}},
		RequiredCapabilities: []string{"issue.read"},
	},
}

// TestSpecEntryPointsSendWhatSyncSends proves the typed-specs and manifest-path
// entry points send exactly the bytes Sync sends for the same manifest.
func TestSpecEntryPointsSendWhatSyncSends(t *testing.T) {
	var mu sync.Mutex
	bodies := make([][]byte, 0, 3)
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		body, _ := io.ReadAll(r.Body)
		mu.Lock()
		bodies = append(bodies, body)
		mu.Unlock()
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"tools":2}`))
	}))
	defer srv.Close()
	cfg := Config{ControlPlaneURL: srv.URL, Token: testToken, Attempts: 1, Env: map[string]string{}, Sleep: noSleep, Logger: slog.New(slog.NewTextHandler(io.Discard, nil))}
	ctx := context.Background()

	marshalled, err := toolspec.MarshalManifestV3(specsFixture)
	if err != nil {
		t.Fatal(err)
	}

	baseline, err := Sync(ctx, marshalled, cfg, true)
	if err != nil {
		t.Fatalf("Sync: %v", err)
	}
	bySpecs, err := SyncSpecs(ctx, specsFixture, cfg, true)
	if err != nil {
		t.Fatalf("SyncSpecs: %v", err)
	}
	path := filepath.Join(t.TempDir(), "manifest.json")
	if err := os.WriteFile(path, marshalled, 0o600); err != nil {
		t.Fatal(err)
	}
	byFile, err := SyncManifestFile(ctx, path, cfg, true)
	if err != nil {
		t.Fatalf("SyncManifestFile: %v", err)
	}

	if len(bodies) != 3 {
		t.Fatalf("got %d PUTs, want 3", len(bodies))
	}
	for i, body := range bodies {
		if string(body) != string(marshalled) {
			t.Errorf("PUT %d body differs from Sync's body for the same manifest\ngot:  %s\nwant: %s", i, body, marshalled)
		}
	}
	for name, res := range map[string]Result{"SyncSpecs": bySpecs, "SyncManifestFile": byFile} {
		if res.Sent != baseline.Sent || res.ToolCount != baseline.ToolCount {
			t.Errorf("%s result = %+v, want Sync's %+v", name, res, baseline)
		}
	}
}

func TestSyncManifestFileReadErrorIsPlainIO(t *testing.T) {
	cfg := Config{ControlPlaneURL: "https://kei.example.test", Token: testToken, Env: map[string]string{}}
	_, err := SyncManifestFile(context.Background(), filepath.Join(t.TempDir(), "absent.json"), cfg, true)
	if err == nil {
		t.Fatal("expected an error for a missing file")
	}
	if err.Error() == "" {
		t.Error("empty error message")
	}
}
