package ontology

import (
	"context"
	"errors"
	"fmt"

	"github.com/soypete/ontology-go/types"
	"github.com/soypete/ontology-go/validate"

	"github.com/haikeilabs/agentware/go/memory/page"
)

// Constraint identifiers carried in Violation.Constraint. Stable strings:
// they are part of the DENY diagnostic contract agents self-correct against.
const (
	ConstraintUnknownPrefix   = "unknown-prefix"
	ConstraintUnknownClass    = "unknown-class"
	ConstraintUnknownProperty = "unknown-property"
	ConstraintUnknownConcept  = "unknown-concept"
	ConstraintDomain          = "domain-mismatch"
	ConstraintRange           = "range-mismatch"
	ConstraintSKOSCycle       = "skos-cycle"
	ConstraintSKOSHierarchy   = "skos-inconsistent-hierarchy"
)

// Violation is a structured diagnostic for one constraint failure. DENY
// decisions carry these so the calling agent can correct the page and retry.
type Violation struct {
	Constraint string   `json:"constraint"`
	Term       string   `json:"term"`
	Message    string   `json:"message"`
	Nearest    []string `json:"nearest,omitempty"`
}

func (v Violation) String() string {
	s := fmt.Sprintf("%s: %s (%s)", v.Constraint, v.Term, v.Message)
	if len(v.Nearest) > 0 {
		s += fmt.Sprintf(" — nearest valid terms: %v", v.Nearest)
	}
	return s
}

// TypeResolver reports the type CURIE of an existing page in the same vault,
// used for link range checks. Returning ok=false means the target page does
// not exist yet; range checks are skipped for it (wikis grow incrementally —
// memory_lint reports dangling links later).
type TypeResolver func(pageID string) (typeCURIE string, ok bool)

// ValidatePage checks a parsed page against the T-box: type class exists,
// topics are known concepts, link predicates exist and respect domain/range.
// A nil resolver skips all range checks.
func (t *TBox) ValidatePage(p *page.Page, resolve TypeResolver, prefixes map[string]string) []Violation {
	if prefixes == nil {
		prefixes = page.DefaultPrefixes()
	}
	var vs []Violation

	expand := func(curie string) (string, bool) {
		iri, err := page.ExpandCURIE(curie, prefixes)
		if err != nil {
			vs = append(vs, Violation{
				Constraint: ConstraintUnknownPrefix,
				Term:       curie,
				Message:    err.Error(),
			})
			return "", false
		}
		return iri, true
	}

	pageType, typeOK := "", false
	if iri, ok := expand(p.Type); ok {
		pageType = iri
		if !t.HasClass(iri) {
			vs = append(vs, Violation{
				Constraint: ConstraintUnknownClass,
				Term:       p.Type,
				Message:    fmt.Sprintf("type %q is not a class in the T-box", iri),
				Nearest:    t.Nearest(iri, KindClass, 3),
			})
		} else {
			typeOK = true
		}
	}

	for _, topic := range p.Topics {
		iri, ok := expand(topic)
		if !ok {
			continue
		}
		if !t.HasConcept(iri) {
			vs = append(vs, Violation{
				Constraint: ConstraintUnknownConcept,
				Term:       topic,
				Message:    fmt.Sprintf("topic %q is not a skos:Concept in the T-box", iri),
				Nearest:    t.Nearest(iri, KindConcept, 3),
			})
		}
	}

	for _, link := range p.AllLinks() {
		predIRI, ok := expand(link.Pred)
		if !ok {
			continue
		}
		prop, known := t.PropertyFor(predIRI)
		if !known {
			vs = append(vs, Violation{
				Constraint: ConstraintUnknownProperty,
				Term:       link.Pred,
				Message:    fmt.Sprintf("predicate %q is not a property in the T-box", predIRI),
				Nearest:    t.Nearest(predIRI, KindProperty, 3),
			})
			continue
		}
		if typeOK && len(prop.Domain) > 0 && !t.subclassOfAny(pageType, prop.Domain) {
			vs = append(vs, Violation{
				Constraint: ConstraintDomain,
				Term:       link.Pred,
				Message: fmt.Sprintf("page type %s is outside the domain of %s (domain: %v)",
					p.Type, link.Pred, prop.Domain),
			})
		}
		if resolve == nil || len(prop.Range) == 0 {
			continue
		}
		targetCURIE, exists := resolve(link.Target)
		if !exists {
			continue
		}
		targetIRI, ok := expand(targetCURIE)
		if !ok {
			continue
		}
		if !t.subclassOfAny(targetIRI, prop.Range) {
			vs = append(vs, Violation{
				Constraint: ConstraintRange,
				Term:       link.Pred,
				Message: fmt.Sprintf("link target %q has type %s, outside the range of %s (range: %v)",
					link.Target, targetCURIE, link.Pred, prop.Range),
			})
		}
	}
	return vs
}

func (t *TBox) subclassOfAny(class string, ancestors []string) bool {
	for _, a := range ancestors {
		if t.IsSubclassOf(class, a) {
			return true
		}
	}
	return false
}

// CheckSKOSGraph runs the SKOS structural checks (broader cycles,
// broader/narrower hierarchy inconsistency) over a triple set — typically a
// vault's merged A-box, optionally combined with T-box concept triples.
func CheckSKOSGraph(ctx context.Context, triples []types.Triple) ([]Violation, error) {
	report, err := validate.NewValidator(triples).Validate(ctx)
	if err != nil {
		return nil, fmt.Errorf("ontology: skos validation: %w", err)
	}
	var vs []Violation
	for _, issue := range report.Issues {
		switch issue.Type {
		case validate.IssueCircularBroader:
			vs = append(vs, Violation{
				Constraint: ConstraintSKOSCycle,
				Term:       issue.Subject,
				Message:    issue.Message,
			})
		case validate.IssueInconsistentHierarchy:
			vs = append(vs, Violation{
				Constraint: ConstraintSKOSHierarchy,
				Term:       issue.Subject,
				Message:    issue.Message,
			})
		}
	}
	return vs, nil
}

// ErrNoTriples reports an empty graph passed to inference helpers.
var ErrNoTriples = errors.New("ontology: no triples")

// TransitiveBroader returns every concept reachable from subject via
// skos:broader (the skos:broaderTransitive closure), in BFS order. Cycles
// are tolerated: each node is visited once.
func TransitiveBroader(triples []types.Triple, subject string) []string {
	broader := make(map[string][]string)
	for _, t := range triples {
		if t.Predicate == skosBroader {
			broader[t.Subject] = append(broader[t.Subject], t.Object)
		}
	}
	seen := map[string]bool{subject: true}
	var out []string
	queue := append([]string{}, broader[subject]...)
	for len(queue) > 0 {
		c := queue[0]
		queue = queue[1:]
		if seen[c] {
			continue
		}
		seen[c] = true
		out = append(out, c)
		queue = append(queue, broader[c]...)
	}
	return out
}

// InferSymmetricRelated returns the skos:related triples implied by symmetry
// (a related b ⇒ b related a) that are not already present.
func InferSymmetricRelated(triples []types.Triple) []types.Triple {
	present := make(map[[2]string]bool)
	for _, t := range triples {
		if t.Predicate == skosRelated {
			present[[2]string{t.Subject, t.Object}] = true
		}
	}
	var inferred []types.Triple
	for pair := range present {
		inverse := [2]string{pair[1], pair[0]}
		if !present[inverse] {
			inferred = append(inferred, types.Triple{
				Subject:   pair[1],
				Predicate: skosRelated,
				Object:    pair[0],
			})
			present[inverse] = true
		}
	}
	return inferred
}
