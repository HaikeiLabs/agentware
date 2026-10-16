package memory

import (
	"context"
	"encoding/json"
	"fmt"
	"strings"
	"time"

	"github.com/soypete/ontology-go/types"

	"github.com/haikeilabs/agentware/go/memory/ontology"
	"github.com/haikeilabs/agentware/go/memory/page"
	"github.com/haikeilabs/agentware/go/middleware"
)

const (
	skosConceptIRI = "http://www.w3.org/2004/02/skos/core#Concept"
	rdfTypeIRI     = "http://www.w3.org/1999/02/22-rdf-syntax-ns#type"
)

// VaultReader gives the evaluator read access to a user's existing pages so
// it can range-check links and detect graph-level violations a new write
// would introduce. The memory executor implements it; tests may fake it.
type VaultReader interface {
	// PageType returns the type CURIE of an existing page, if the page exists.
	PageType(userID, pageID string) (string, bool)
	// VaultTriples returns the merged A-box of the user's vault, excluding
	// excludePageID (the page being rewritten).
	VaultTriples(userID, excludePageID string) ([]types.Triple, error)
}

// OntologyEvaluator implements middleware.PolicyEvaluator for memory tools.
// Evaluation is two-tier:
//
//  1. Declarative: the stock middleware Policy (deny-by-default, scope
//     rules, per-rule rate limits).
//  2. Semantic: for write tools, the page content in args["content"] is
//     parsed and validated against the T-box, and the write is checked for
//     SKOS violations it would introduce into the vault graph (cycles,
//     broader/narrower inconsistency).
//
// DENY decisions embed structured diagnostics: Reason ends with a JSON
// array of ontology.Violation after the "diagnostics:" marker, so calling
// agents can self-correct and retry.
type OntologyEvaluator struct {
	tbox     *ontology.TBox
	policy   *middleware.Policy
	limiter  *middleware.RateLimiter
	prefixes map[string]string
	vault    VaultReader
}

// EvaluatorOption configures an OntologyEvaluator.
type EvaluatorOption func(*OntologyEvaluator)

// WithDeclarativePolicy replaces the embedded default policy.
func WithDeclarativePolicy(p *middleware.Policy) EvaluatorOption {
	return func(e *OntologyEvaluator) { e.policy = p }
}

// WithPrefixes replaces the default CURIE prefix map (SCHEMA.md).
func WithPrefixes(prefixes map[string]string) EvaluatorOption {
	return func(e *OntologyEvaluator) { e.prefixes = prefixes }
}

// WithVaultReader wires vault read access for range and graph checks.
// Without it, those checks are skipped (existence/domain checks still run).
func WithVaultReader(v VaultReader) EvaluatorOption {
	return func(e *OntologyEvaluator) { e.vault = v }
}

// NewOntologyEvaluator builds the evaluator for a read-only T-box.
func NewOntologyEvaluator(tbox *ontology.TBox, opts ...EvaluatorOption) *OntologyEvaluator {
	e := &OntologyEvaluator{
		tbox:     tbox,
		policy:   DefaultPolicy(),
		limiter:  middleware.NewRateLimiter(),
		prefixes: page.DefaultPrefixes(),
	}
	for _, opt := range opts {
		opt(e)
	}
	return e
}

// Evaluate implements middleware.PolicyEvaluator.
func (e *OntologyEvaluator) Evaluate(toolName string, args map[string]any, caller middleware.CallerContext) middleware.Decision {
	decision := e.policy.Evaluate(toolName, args, caller)
	if decision.Action == middleware.ActionDeny {
		return decision
	}
	if denied, d := e.rateLimited(decision, toolName, caller); denied {
		return d
	}
	if !isSemanticWrite(toolName) {
		return decision
	}
	return e.evaluateWrite(decision, args, caller)
}

func (e *OntologyEvaluator) rateLimited(d middleware.Decision, toolName string, caller middleware.CallerContext) (bool, middleware.Decision) {
	// The stock policy engine matches rules but does not enforce MaxRate;
	// apply the matched rule's limit here, keyed per user and tool.
	for i := range e.policy.Rules {
		rule := &e.policy.Rules[i]
		if rule.Name != d.Rule {
			continue
		}
		if rule.MaxRate == nil {
			return false, d
		}
		key := caller.UserID + "|" + toolName
		e.limiter.SetWindow(key, rule.MaxRate.Window)
		if e.limiter.Allow(key, rule.MaxRate.Count) {
			return false, d
		}
		return true, middleware.Decision{
			Action: middleware.ActionDeny,
			Rule:   rule.Name,
			Reason: fmt.Sprintf("rate limit exceeded: %d calls per %s for %s",
				rule.MaxRate.Count, rule.MaxRate.Window, toolName),
			Timestamp: time.Now(),
		}
	}
	return false, d
}

func (e *OntologyEvaluator) evaluateWrite(d middleware.Decision, args map[string]any, caller middleware.CallerContext) middleware.Decision {
	content, ok := args["content"].(string)
	if !ok || content == "" {
		return denyDiagnostics(d.Rule, "write requires string args.content with page markdown", nil)
	}
	p, err := page.Parse([]byte(content))
	if err != nil {
		return denyDiagnostics(d.Rule, fmt.Sprintf("page contract violation: %v", err), nil)
	}
	if pageID, ok := args["page_id"].(string); ok && pageID != p.ID {
		return denyDiagnostics(d.Rule,
			fmt.Sprintf("args.page_id %q does not match frontmatter id %q", pageID, p.ID), nil)
	}

	var resolve ontology.TypeResolver
	if e.vault != nil {
		resolve = func(pageID string) (string, bool) {
			return e.vault.PageType(caller.UserID, pageID)
		}
	}
	violations := e.tbox.ValidatePage(p, resolve, e.prefixes)

	if len(violations) == 0 && e.vault != nil {
		graphViolations, err := e.graphCheck(p, caller.UserID)
		if err != nil {
			return denyDiagnostics(d.Rule, fmt.Sprintf("vault graph check failed: %v", err), nil)
		}
		violations = graphViolations
	}
	if len(violations) > 0 {
		return denyDiagnostics(d.Rule,
			fmt.Sprintf("ontology validation failed with %d violation(s)", len(violations)),
			violations)
	}
	return d
}

// graphCheck validates the vault graph as it would exist after the write.
// Page subjects are additionally typed skos:Concept in the candidate graph:
// vault pages are the concepts of the user's scheme, and the upstream SKOS
// checks seed their traversals from skos:Concept subjects.
func (e *OntologyEvaluator) graphCheck(p *page.Page, userID string) ([]ontology.Violation, error) {
	existing, err := e.vault.VaultTriples(userID, p.ID)
	if err != nil {
		return nil, err
	}
	newTriples, err := p.Triples("", e.prefixes)
	if err != nil {
		return nil, err
	}
	candidate := append(append([]types.Triple{}, existing...), newTriples...)
	subjects := make(map[string]bool)
	for _, t := range candidate {
		subjects[t.Subject] = true
	}
	for subj := range subjects {
		candidate = append(candidate, types.Triple{
			Subject: subj, Predicate: rdfTypeIRI, Object: skosConceptIRI,
		})
	}
	return ontology.CheckSKOSGraph(context.Background(), candidate)
}

// DiagnosticsMarker separates the human-readable reason from the JSON
// diagnostics payload in DENY reasons.
const DiagnosticsMarker = "diagnostics: "

func denyDiagnostics(rule, reason string, violations []ontology.Violation) middleware.Decision {
	if len(violations) > 0 {
		if payload, err := json.Marshal(violations); err == nil {
			reason += "; " + DiagnosticsMarker + string(payload)
		}
	}
	return middleware.Decision{
		Action:    middleware.ActionDeny,
		Rule:      rule,
		Reason:    reason,
		Timestamp: time.Now(),
	}
}

// ParseDiagnostics extracts the structured violations from a DENY reason
// produced by this evaluator. ok is false if the reason carries none.
func ParseDiagnostics(reason string) ([]ontology.Violation, bool) {
	i := strings.Index(reason, DiagnosticsMarker)
	if i < 0 {
		return nil, false
	}
	var vs []ontology.Violation
	if err := json.Unmarshal([]byte(reason[i+len(DiagnosticsMarker):]), &vs); err != nil {
		return nil, false
	}
	return vs, true
}
