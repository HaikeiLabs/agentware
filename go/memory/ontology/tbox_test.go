package ontology

import (
	"path/filepath"
	"testing"

	"github.com/haikeilabs/agentware/go/memory/page"
)

const (
	tboxEducation = "../../../ontologies/education/TBOX_LEARNING_SOFTWARE.ttl"
	tboxTopics    = "../../../ontologies/social/twitch_topics.ttl"
	l2ws          = "http://soypete.tech/l2ws/"
	twitch        = "http://soypete.tech/twitch/topics/"
)

func loadTBox(t *testing.T) *TBox {
	t.Helper()
	paths := []string{tboxEducation, tboxTopics}
	for _, p := range paths {
		if _, err := filepath.Abs(p); err != nil {
			t.Fatal(err)
		}
	}
	tb, err := Load(paths...)
	if err != nil {
		t.Skipf("T-box fixtures unavailable (run: git submodule update --init): %v", err)
	}
	return tb
}

func TestLoadIndexesRealTBox(t *testing.T) {
	tb := loadTBox(t)
	for _, class := range []string{l2ws + "Skill", l2ws + "Beginner", l2ws + "Course"} {
		if !tb.HasClass(class) {
			t.Errorf("expected class %s", class)
		}
	}
	if tb.HasClass(l2ws + "NotAClass") {
		t.Error("NotAClass should not be a class")
	}
	prop, ok := tb.PropertyFor(l2ws + "requiresPrerequisite")
	if !ok {
		t.Fatal("expected property requiresPrerequisite")
	}
	if len(prop.Domain) == 0 || prop.Domain[0] != l2ws+"Skill" {
		t.Errorf("requiresPrerequisite domain: %v", prop.Domain)
	}
	if !tb.HasConcept(twitch + "Go") {
		t.Error("expected twitch:Go concept")
	}
	// skos core properties are built in without T-box declarations.
	if _, ok := tb.PropertyFor("http://www.w3.org/2004/02/skos/core#related"); !ok {
		t.Error("expected built-in skos:related")
	}
}

func TestMatchConceptLabel(t *testing.T) {
	tb := loadTBox(t)
	iri, ok := tb.MatchConceptLabel("Golang")
	if !ok || iri != twitch+"Go" {
		t.Errorf("MatchConceptLabel(Golang) = %q, %v; want twitch:Go", iri, ok)
	}
	if _, ok := tb.MatchConceptLabel("definitely-not-a-topic"); ok {
		t.Error("unexpected match for unknown label")
	}
}

func TestNearestSuggestsSimilarTerms(t *testing.T) {
	tb := loadTBox(t)
	nearest := tb.Nearest(l2ws+"Skil", KindClass, 3)
	if len(nearest) == 0 || nearest[0] != l2ws+"Skill" {
		t.Errorf("Nearest(Skil) = %v; want sw:Skill first", nearest)
	}
}

func TestIsSubclassOfTransitive(t *testing.T) {
	tb := loadTBox(t)
	// sw:Syntax rdfs:subClassOf sw:Fundamentals in the T-box.
	if !tb.IsSubclassOf(l2ws+"Syntax", l2ws+"Fundamentals") {
		t.Error("Syntax should be a subclass of Fundamentals")
	}
	if !tb.IsSubclassOf(l2ws+"Skill", l2ws+"Skill") {
		t.Error("IsSubclassOf should be reflexive")
	}
	if tb.IsSubclassOf(l2ws+"Skill", l2ws+"Course") {
		t.Error("Skill is not a subclass of Course")
	}
}

func mustParse(t *testing.T, src string) *page.Page {
	t.Helper()
	p, err := page.Parse([]byte(src))
	if err != nil {
		t.Fatal(err)
	}
	return p
}

const validPage = `---
id: go-worker-pools
type: sw:Skill
labels: ["Worker Pools"]
topics: [twitch:Go]
links:
  - {pred: sw:requiresPrerequisite, target: goroutines}
---
Body with [[channels]].
`

func TestValidatePageAccepts(t *testing.T) {
	tb := loadTBox(t)
	p := mustParse(t, validPage)
	resolve := func(id string) (string, bool) {
		if id == "goroutines" || id == "channels" {
			return "sw:Skill", true
		}
		return "", false
	}
	if vs := tb.ValidatePage(p, resolve, nil); len(vs) != 0 {
		t.Errorf("expected clean page, got %v", vs)
	}
}

func TestValidatePageUnknownClassGetsNearest(t *testing.T) {
	tb := loadTBox(t)
	p := mustParse(t, "---\nid: x-page\ntype: sw:Skil\n---\n")
	vs := tb.ValidatePage(p, nil, nil)
	if len(vs) != 1 || vs[0].Constraint != ConstraintUnknownClass {
		t.Fatalf("expected one unknown-class violation, got %v", vs)
	}
	if len(vs[0].Nearest) == 0 || vs[0].Nearest[0] != l2ws+"Skill" {
		t.Errorf("expected sw:Skill as nearest term, got %v", vs[0].Nearest)
	}
}

func TestValidatePageUnknownTopicAndProperty(t *testing.T) {
	tb := loadTBox(t)
	src := `---
id: x-page
type: sw:Skill
topics: [twitch:Golangg]
links:
  - {pred: sw:requiresPrereq, target: other-page}
---
`
	vs := tb.ValidatePage(mustParse(t, src), nil, nil)
	got := map[string]bool{}
	for _, v := range vs {
		got[v.Constraint] = true
		if len(v.Nearest) == 0 {
			t.Errorf("%s: expected nearest-term suggestions", v.Constraint)
		}
	}
	if !got[ConstraintUnknownConcept] || !got[ConstraintUnknownProperty] {
		t.Errorf("expected unknown-concept and unknown-property, got %v", vs)
	}
}

func TestValidatePageDomainAndRange(t *testing.T) {
	tb := loadTBox(t)
	// requiresPrerequisite has domain sw:Skill; a Course page using it
	// violates the domain. Its range is sw:Skill; a Role target violates it.
	src := `---
id: x-page
type: sw:Course
links:
  - {pred: sw:requiresPrerequisite, target: some-role}
---
`
	resolve := func(id string) (string, bool) { return "sw:Role", true }
	vs := tb.ValidatePage(mustParse(t, src), resolve, nil)
	got := map[string]bool{}
	for _, v := range vs {
		got[v.Constraint] = true
	}
	if !got[ConstraintDomain] || !got[ConstraintRange] {
		t.Errorf("expected domain and range violations, got %v", vs)
	}
	// Unresolvable target: range check is skipped, domain still fails.
	vs = tb.ValidatePage(mustParse(t, src), func(string) (string, bool) { return "", false }, nil)
	for _, v := range vs {
		if v.Constraint == ConstraintRange {
			t.Errorf("range must be skipped for dangling targets, got %v", vs)
		}
	}
}

func TestValidatePageUnknownPrefix(t *testing.T) {
	tb := loadTBox(t)
	p := mustParse(t, "---\nid: x-page\ntype: bogus:Thing\n---\n")
	vs := tb.ValidatePage(p, nil, nil)
	if len(vs) != 1 || vs[0].Constraint != ConstraintUnknownPrefix {
		t.Errorf("expected unknown-prefix violation, got %v", vs)
	}
}
