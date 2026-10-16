package memory

// End-to-end tests of the enforced chain: Enable() wiring, allow/deny
// through the middleware, audit records, and cross-user isolation.

import (
	"context"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/haikeilabs/agentware/go/memory/ontology"
	"github.com/haikeilabs/agentware/go/middleware"
)

func enableTestMemory(t *testing.T) *WikiMemory {
	t.Helper()
	w, err := Enable(Config{
		Root:      t.TempDir(),
		TBoxPaths: []string{tboxEducation, tboxTopics},
	})
	if err != nil {
		if strings.Contains(err.Error(), "ontologies") {
			t.Skipf("T-box unavailable (run: git submodule update --init): %v", err)
		}
		t.Fatal(err)
	}
	return w
}

func callerCtx(userID string) context.Context {
	// InvokingSubject, ParentSpan and SessionID are distinct, explicit fields.
	// The middleware records InvokingSubject from caller.InvokingSubject and
	// ParentSpan from caller.ParentSpan; SessionID is not remapped onto the
	// audit record.
	return middleware.WithCallerContext(context.Background(), middleware.CallerContext{
		UserID: userID, SessionID: "sess-" + userID, Trusted: true,
		InvokingSubject: userID, ParentSpan: "span-" + userID,
	})
}

const workerPoolsPage = `---
id: go-worker-pools
type: sw:Skill
labels: ["Worker Pools"]
topics: [twitch:Go]
claims:
  - {id: c1, text: "Bounded pools prevent goroutine leaks", sources: [src-talk]}
---
Pools bound concurrency.
`

func TestChainValidWriteAllowedAndPersisted(t *testing.T) {
	w := enableTestMemory(t)
	res, err := w.Execute(callerCtx("alice"), ToolWritePage, map[string]any{"content": workerPoolsPage})
	if err != nil || !res.Success {
		t.Fatalf("expected successful write, got res=%+v err=%v", res, err)
	}
	path, _ := w.Vault().PagePath("alice", "go-worker-pools")
	if _, err := os.Stat(path); err != nil {
		t.Errorf("page not persisted: %v", err)
	}
	wiki, _ := w.Vault().WikiDir("alice")
	index, err := os.ReadFile(filepath.Join(wiki, "index.md"))
	if err != nil || !strings.Contains(string(index), "go-worker-pools") {
		t.Errorf("index.md not refreshed: %v %q", err, index)
	}
	logData, err := os.ReadFile(filepath.Join(wiki, "log.md"))
	if err != nil || !strings.Contains(string(logData), "wrote page go-worker-pools") {
		t.Errorf("log.md not appended: %v %q", err, logData)
	}
}

func TestChainDenyCarriesDiagnosticsAndIsAudited(t *testing.T) {
	w := enableTestMemory(t)
	bad := "---\nid: bad-page\ntype: sw:Skil\n---\n"
	res, err := w.Execute(callerCtx("alice"), ToolWritePage, map[string]any{"content": bad})
	if err != nil {
		t.Fatal(err)
	}
	if res.Success {
		t.Fatal("expected policy denial")
	}
	vs, ok := ParseDiagnostics(res.Error)
	if !ok || vs[0].Constraint != ontology.ConstraintUnknownClass || len(vs[0].Nearest) == 0 {
		t.Errorf("expected unknown-class diagnostics with nearest terms in %q", res.Error)
	}
	denies := w.Auditor().Query(middleware.AuditFilter{Action: middleware.ActionDeny})
	if len(denies) != 1 {
		t.Fatalf("expected 1 audited deny, got %d", len(denies))
	}
	// The audit record must capture the caller identity (InvokingSubject) and
	// the parent span (ParentSpan) as distinct, explicit fields.
	if denies[0].ToolName != ToolWritePage ||
		denies[0].InvokingSubject != "alice" ||
		denies[0].ParentSpan != "span-alice" {
		t.Errorf("audit record mismatch: %+v", denies[0])
	}
	if _, ok := ParseDiagnostics(denies[0].Error); !ok {
		t.Error("audited deny must carry the diagnostics payload")
	}
}

func TestChainDeniesCycleThroughRealVault(t *testing.T) {
	w := enableTestMemory(t)
	ctx := callerCtx("alice")
	a := "---\nid: a-page\ntype: sw:Skill\nlinks:\n  - {pred: skos:broader, target: b-page}\n---\n"
	if res, err := w.Execute(ctx, ToolWritePage, map[string]any{"content": a}); err != nil || !res.Success {
		t.Fatalf("first write should pass (dangling target allowed): %+v %v", res, err)
	}
	b := "---\nid: b-page\ntype: sw:Skill\nlinks:\n  - {pred: skos:broader, target: a-page}\n---\n"
	res, err := w.Execute(ctx, ToolWritePage, map[string]any{"content": b})
	if err != nil {
		t.Fatal(err)
	}
	if res.Success {
		t.Fatal("cycle-introducing write must be denied")
	}
	vs, ok := ParseDiagnostics(res.Error)
	if !ok {
		t.Fatalf("expected diagnostics, got %q", res.Error)
	}
	found := false
	for _, v := range vs {
		if v.Constraint == ontology.ConstraintSKOSCycle {
			found = true
		}
	}
	if !found {
		t.Errorf("expected skos-cycle violation, got %v", vs)
	}
}

func TestChainUserIsolation(t *testing.T) {
	w := enableTestMemory(t)
	if res, err := w.Execute(callerCtx("alice"), ToolWritePage, map[string]any{"content": workerPoolsPage}); err != nil || !res.Success {
		t.Fatalf("alice write failed: %+v %v", res, err)
	}

	// Policy half: bob cannot redirect scope via args.
	res, err := w.Execute(callerCtx("bob"), ToolQuery, map[string]any{
		"question": "worker pools", "user_id": "alice",
	})
	if err != nil {
		t.Fatal(err)
	}
	if res.Success || !strings.Contains(res.Error, "memory-deny-scope-override") &&
		!strings.Contains(res.Error, "denied by policy") {
		t.Errorf("scope-override must be denied, got %+v", res)
	}

	// Scoping half: bob's own query sees nothing of alice's vault.
	res, err = w.Execute(callerCtx("bob"), ToolQuery, map[string]any{"question": "worker pools"})
	if err != nil || !res.Success {
		t.Fatalf("bob query failed: %+v %v", res, err)
	}
	if strings.Contains(res.Output, "go-worker-pools") {
		t.Errorf("bob must not see alice's pages, got %s", res.Output)
	}

	// Path half: alice's page exists only under alice's vault, and the
	// vault refuses alice's path for bob.
	alicePage, _ := w.Vault().PagePath("alice", "go-worker-pools")
	if w.Vault().Contains("bob", alicePage) {
		t.Error("vault must reject alice's page path for bob")
	}
	// Claims are scoped the same way.
	res, err = w.Execute(callerCtx("bob"), ToolGetClaims, map[string]any{})
	if err != nil || !res.Success {
		t.Fatalf("bob get_claims failed: %+v %v", res, err)
	}
	if strings.Contains(res.Output, "goroutine leaks") {
		t.Errorf("bob must not see alice's claims, got %s", res.Output)
	}
}

func TestChainAnonymousDeniedBeforeExecutor(t *testing.T) {
	w := enableTestMemory(t)
	res, err := w.Execute(context.Background(), ToolQuery, map[string]any{"question": "x"})
	if err != nil {
		t.Fatal(err)
	}
	if res.Success {
		t.Error("anonymous call must be denied by policy")
	}
}

func TestChainIngestAndClaims(t *testing.T) {
	w := enableTestMemory(t)
	ctx := callerCtx("alice")
	res, err := w.Execute(ctx, ToolIngest, map[string]any{"source_id": "src-talk", "text": "raw notes"})
	if err != nil || !res.Success {
		t.Fatalf("ingest failed: %+v %v", res, err)
	}
	raw, _ := w.Vault().RawDir("alice")
	if _, err := os.Stat(filepath.Join(raw, "src-talk.md")); err != nil {
		t.Errorf("raw source not stored: %v", err)
	}
	if res, err := w.Execute(ctx, ToolWritePage, map[string]any{"content": workerPoolsPage}); err != nil || !res.Success {
		t.Fatalf("write failed: %+v %v", res, err)
	}
	res, err = w.Execute(ctx, ToolGetClaims, map[string]any{"page_id": "go-worker-pools"})
	if err != nil || !res.Success {
		t.Fatalf("get_claims failed: %+v %v", res, err)
	}
	if !strings.Contains(res.Output, "goroutine leaks") || !strings.Contains(res.Output, `"confidence": null`) {
		t.Errorf("claims output missing fields: %s", res.Output)
	}
}

func TestChainLintReportsDanglingLink(t *testing.T) {
	w := enableTestMemory(t)
	ctx := callerCtx("alice")
	a := "---\nid: a-page\ntype: sw:Skill\nlinks:\n  - {pred: skos:related, target: no-such-page}\n---\n"
	if res, err := w.Execute(ctx, ToolWritePage, map[string]any{"content": a}); err != nil || !res.Success {
		t.Fatalf("write failed: %+v %v", res, err)
	}
	res, err := w.Execute(ctx, ToolLint, map[string]any{})
	if err != nil || !res.Success {
		t.Fatalf("lint failed: %+v %v", res, err)
	}
	if !strings.Contains(res.Output, "dangling-link") || !strings.Contains(res.Output, "no-such-page") {
		t.Errorf("lint must report the dangling link, got %s", res.Output)
	}
}
