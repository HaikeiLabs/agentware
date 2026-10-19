package memory

import (
	"strings"
	"testing"
	"time"

	"github.com/soypete/ontology-go/types"

	"github.com/haikeilabs/agentware/go/memory/ontology"
	"github.com/haikeilabs/agentware/go/middleware"
)

const (
	tboxEducation = "../../ontologies/education/TBOX_LEARNING_SOFTWARE.ttl"
	tboxTopics    = "../../ontologies/social/twitch_topics.ttl"
)

func testTBox(t *testing.T) *ontology.TBox {
	t.Helper()
	tb, err := ontology.Load(tboxEducation, tboxTopics)
	if err != nil {
		t.Skipf("T-box fixtures unavailable (run: git submodule update --init): %v", err)
	}
	return tb
}

// fakeVault is an in-memory VaultReader: pageID -> (type CURIE, triples).
type fakeVault struct {
	users map[string]map[string]fakePage
}

type fakePage struct {
	typeCURIE string
	triples   []types.Triple
}

func (f *fakeVault) PageType(userID, pageID string) (string, bool) {
	p, ok := f.users[userID][pageID]
	return p.typeCURIE, ok
}

func (f *fakeVault) VaultTriples(userID, excludePageID string) ([]types.Triple, error) {
	var out []types.Triple
	for id, p := range f.users[userID] {
		if id == excludePageID {
			continue
		}
		out = append(out, p.triples...)
	}
	return out, nil
}

func alice() middleware.CallerContext {
	return middleware.CallerContext{UserID: "alice", SessionID: "s1", Trusted: true}
}

func pageArgs(content string) map[string]any {
	return map[string]any{"content": content}
}

const validWrite = `---
id: go-worker-pools
type: sw:Skill
labels: ["Worker Pools"]
topics: [twitch:Go]
---
Uses [[goroutines|pred=sw:requiresPrerequisite]].
`

func TestEvaluatorAllowsValidWrite(t *testing.T) {
	vault := &fakeVault{users: map[string]map[string]fakePage{
		"alice": {"goroutines": {typeCURIE: "sw:Skill"}},
	}}
	e := NewOntologyEvaluator(testTBox(t), WithVaultReader(vault))
	d := e.Evaluate(ToolWritePage, pageArgs(validWrite), alice())
	if d.Action != middleware.ActionAllow {
		t.Fatalf("expected allow, got %s: %s", d.Action, d.Reason)
	}
}

func TestEvaluatorDeniesUnknownClassWithNearestTerms(t *testing.T) {
	e := NewOntologyEvaluator(testTBox(t))
	content := "---\nid: x-page\ntype: sw:Skil\n---\n"
	d := e.Evaluate(ToolWritePage, pageArgs(content), alice())
	if d.Action != middleware.ActionDeny {
		t.Fatalf("expected deny, got %s", d.Action)
	}
	vs, ok := ParseDiagnostics(d.Reason)
	if !ok || len(vs) != 1 {
		t.Fatalf("expected parseable diagnostics, got %q", d.Reason)
	}
	if vs[0].Constraint != ontology.ConstraintUnknownClass {
		t.Errorf("expected unknown-class, got %s", vs[0].Constraint)
	}
	if len(vs[0].Nearest) == 0 || vs[0].Nearest[0] != "http://soypete.tech/l2ws/Skill" {
		t.Errorf("expected sw:Skill as nearest term, got %v", vs[0].Nearest)
	}
}

func TestEvaluatorDeniesCycleIntroducingLink(t *testing.T) {
	// Existing vault: a-page --skos:broader--> b-page. Writing b-page with
	// skos:broader a-page closes a cycle and must be denied.
	base := "http://soypete.tech/memory/"
	vault := &fakeVault{users: map[string]map[string]fakePage{
		"alice": {
			"a-page": {typeCURIE: "sw:Skill", triples: []types.Triple{
				{Subject: base + "a-page", Predicate: "http://www.w3.org/2004/02/skos/core#broader", Object: base + "b-page"},
			}},
			"b-page": {typeCURIE: "sw:Skill"},
		},
	}}
	e := NewOntologyEvaluator(testTBox(t), WithVaultReader(vault))
	content := "---\nid: b-page\ntype: sw:Skill\nlinks:\n  - {pred: skos:broader, target: a-page}\n---\n"
	d := e.Evaluate(ToolWritePage, pageArgs(content), alice())
	if d.Action != middleware.ActionDeny {
		t.Fatalf("expected deny for cycle-introducing link, got %s: %s", d.Action, d.Reason)
	}
	vs, ok := ParseDiagnostics(d.Reason)
	if !ok {
		t.Fatalf("expected diagnostics, got %q", d.Reason)
	}
	found := false
	for _, v := range vs {
		if v.Constraint == ontology.ConstraintSKOSCycle {
			found = true
		}
	}
	if !found {
		t.Errorf("expected %s violation, got %v", ontology.ConstraintSKOSCycle, vs)
	}
}

func TestEvaluatorDeniesMissingContent(t *testing.T) {
	e := NewOntologyEvaluator(testTBox(t))
	d := e.Evaluate(ToolWritePage, map[string]any{}, alice())
	if d.Action != middleware.ActionDeny {
		t.Errorf("missing content must be denied, got %s", d.Action)
	}
}

func TestEvaluatorNonMemoryToolAllowed(t *testing.T) {
	e := NewOntologyEvaluator(testTBox(t))
	if d := e.Evaluate("delete_database", nil, alice()); d.Action != middleware.ActionAllow {
		t.Errorf("non-memory tool should be allowed (policy from catalog), got %s", d.Action)
	}
}

func TestEvaluatorRateLimit(t *testing.T) {
	limits := map[string]toolRateLimit{
		"memory_query": {Count: 3, Window: time.Minute},
	}
	e := NewOntologyEvaluator(testTBox(t), WithToolRateLimits(limits))
	for i := range 3 {
		if d := e.Evaluate(ToolQuery, nil, alice()); d.Action != middleware.ActionAllow {
			t.Fatalf("call %d should be allowed, got %s: %s", i+1, d.Action, d.Reason)
		}
	}
	d := e.Evaluate(ToolQuery, nil, alice())
	if d.Action != middleware.ActionDeny || !strings.Contains(d.Reason, "rate limit") {
		t.Errorf("4th call must be rate-limited, got %s: %s", d.Action, d.Reason)
	}
	// Other users are unaffected (limits are per user+tool).
	bob := middleware.CallerContext{UserID: "bob", SessionID: "s2"}
	if d := e.Evaluate(ToolQuery, nil, bob); d.Action != middleware.ActionAllow {
		t.Errorf("bob must not share alice's rate bucket, got %s", d.Action)
	}
}

func TestEvaluatorPageIDMismatch(t *testing.T) {
	e := NewOntologyEvaluator(testTBox(t))
	args := pageArgs(validWrite)
	args["page_id"] = "other-page"
	d := e.Evaluate(ToolWritePage, args, alice())
	if d.Action != middleware.ActionDeny || !strings.Contains(d.Reason, "does not match") {
		t.Errorf("expected page_id mismatch deny, got %s: %s", d.Action, d.Reason)
	}
}

func TestParseDiagnosticsRoundTrip(t *testing.T) {
	vs := []ontology.Violation{{Constraint: "unknown-class", Term: "sw:Skil", Message: "m", Nearest: []string{"a"}}}
	d := denyDiagnostics("r", "ontology validation failed", vs)
	got, ok := ParseDiagnostics(d.Reason)
	if !ok || len(got) != 1 || got[0].Constraint != "unknown-class" {
		t.Errorf("round trip failed: %v %v", got, ok)
	}
	if _, ok := ParseDiagnostics("no marker here"); ok {
		t.Error("expected no diagnostics")
	}
}
