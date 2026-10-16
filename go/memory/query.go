package memory

import (
	"fmt"
	"slices"
	"sort"
	"strings"

	"github.com/haikeilabs/agentware/go/memory/page"
	"github.com/haikeilabs/agentware/go/tools"
)

// Structured query kinds accepted by memory_query (args.kind). Free-text
// args.question remains the keyword fallback. These answer the L2WS
// competency questions from memory content.
const (
	QueryPrerequisites   = "prerequisites"    // args: page_id
	QueryBuildsToward    = "builds_toward"    // args: page_id
	QueryLearningPath    = "learning_path"    // args: role (role page id)
	QueryContestedClaims = "contested_claims" // no args
	QueryLowConfidence   = "low_confidence"   // args: threshold (default 0.6)
)

const (
	predRequiresPrerequisite = "sw:requiresPrerequisite"
	predBuildsToward         = "sw:buildsToward"
	predIsAppropriateFor     = "sw:isAppropriateFor"
)

func (x *Executor) structuredQuery(userID string, kind string, args map[string]any) (*tools.Result, error) {
	pages, err := x.pages(userID)
	if err != nil {
		return nil, err
	}
	byID := make(map[string]*page.Page, len(pages))
	for _, p := range pages {
		byID[p.ID] = p
	}
	switch kind {
	case QueryPrerequisites:
		return x.closureQuery(byID, stringArg(args, "page_id"), predRequiresPrerequisite)
	case QueryBuildsToward:
		return x.closureQuery(byID, stringArg(args, "page_id"), predBuildsToward)
	case QueryLearningPath:
		return x.learningPath(pages, byID, stringArg(args, "role"))
	case QueryContestedClaims:
		return x.claimFilter(pages, func(c page.Claim) bool { return c.Contested })
	case QueryLowConfidence:
		threshold := 0.6
		if t, ok := args["threshold"].(float64); ok {
			threshold = t
		}
		return x.claimFilter(pages, func(c page.Claim) bool {
			return c.Confidence != nil && *c.Confidence < threshold
		})
	default:
		return &tools.Result{Success: false,
			Error: fmt.Sprintf("memory: unknown query kind %q", kind)}, nil
	}
}

// closureQuery walks links with the given predicate transitively from a
// page, returning targets in BFS order (nearest first). Cycle-safe.
func (x *Executor) closureQuery(byID map[string]*page.Page, pageID, pred string) (*tools.Result, error) {
	if _, ok := byID[pageID]; !ok {
		return &tools.Result{Success: false,
			Error: fmt.Sprintf("memory: page %q not found", pageID)}, nil
	}
	type entry struct {
		ID    string `json:"id"`
		Type  string `json:"type,omitempty"`
		Depth int    `json:"depth"`
	}
	out := []entry{}
	seen := map[string]bool{pageID: true}
	frontier := []string{pageID}
	for depth := 1; len(frontier) > 0; depth++ {
		var next []string
		for _, id := range frontier {
			p, ok := byID[id]
			if !ok {
				continue
			}
			for _, link := range p.AllLinks() {
				if link.Pred != pred || seen[link.Target] {
					continue
				}
				seen[link.Target] = true
				pageType := ""
				if tp, ok := byID[link.Target]; ok {
					pageType = tp.Type
				}
				out = append(out, entry{ID: link.Target, Type: pageType, Depth: depth})
				next = append(next, link.Target)
			}
		}
		frontier = next
	}
	return jsonResult(out)
}

// learningPath lists the skills appropriate for a role, ordered so that
// prerequisites come before the skills that require them.
func (x *Executor) learningPath(pages []*page.Page, byID map[string]*page.Page, roleID string) (*tools.Result, error) {
	if roleID == "" {
		return &tools.Result{Success: false, Error: "memory: learning_path requires args.role"}, nil
	}
	relevant := map[string]bool{}
	for _, p := range pages {
		for _, link := range p.AllLinks() {
			if link.Pred == predIsAppropriateFor && link.Target == roleID {
				relevant[p.ID] = true
			}
		}
	}
	// Include transitive prerequisites of every relevant skill.
	queue := make([]string, 0, len(relevant))
	for id := range relevant {
		queue = append(queue, id)
	}
	for len(queue) > 0 {
		id := queue[0]
		queue = queue[1:]
		p, ok := byID[id]
		if !ok {
			continue
		}
		for _, link := range p.AllLinks() {
			if link.Pred == predRequiresPrerequisite && !relevant[link.Target] {
				relevant[link.Target] = true
				queue = append(queue, link.Target)
			}
		}
	}
	ordered := topoByPrerequisite(relevant, byID)
	type step struct {
		ID    string `json:"id"`
		Type  string `json:"type,omitempty"`
		Label string `json:"label,omitempty"`
	}
	out := []step{}
	for _, id := range ordered {
		s := step{ID: id}
		if p, ok := byID[id]; ok {
			s.Type = p.Type
			if len(p.Labels) > 0 {
				s.Label = p.Labels[0]
			}
		}
		out = append(out, s)
	}
	return jsonResult(out)
}

// topoByPrerequisite orders page ids so prerequisites precede dependents;
// ties break alphabetically. Cycles cannot occur (the evaluator rejects
// them at write time), but the sort degrades gracefully if one appears.
func topoByPrerequisite(ids map[string]bool, byID map[string]*page.Page) []string {
	indegree := map[string]int{}
	dependents := map[string][]string{}
	for id := range ids {
		indegree[id] += 0
		p, ok := byID[id]
		if !ok {
			continue
		}
		for _, link := range p.AllLinks() {
			if link.Pred == predRequiresPrerequisite && ids[link.Target] {
				indegree[id]++
				dependents[link.Target] = append(dependents[link.Target], id)
			}
		}
	}
	ready := []string{}
	for id, deg := range indegree {
		if deg == 0 {
			ready = append(ready, id)
		}
	}
	sort.Strings(ready)
	var out []string
	for len(ready) > 0 {
		id := ready[0]
		ready = ready[1:]
		out = append(out, id)
		next := dependents[id]
		sort.Strings(next)
		for _, dep := range next {
			indegree[dep]--
			if indegree[dep] == 0 {
				ready = append(ready, dep)
			}
		}
	}
	if len(out) < len(indegree) { // cycle remnant: append leftovers stably
		var rest []string
		for id := range indegree {
			if !slices.Contains(out, id) {
				rest = append(rest, id)
			}
		}
		sort.Strings(rest)
		out = append(out, rest...)
	}
	return out
}

func (x *Executor) claimFilter(pages []*page.Page, keep func(page.Claim) bool) (*tools.Result, error) {
	type claimOut struct {
		Page       string   `json:"page"`
		ID         string   `json:"id"`
		Text       string   `json:"text"`
		Confidence *float64 `json:"confidence"`
		Contested  bool     `json:"contested,omitempty"`
	}
	out := []claimOut{}
	for _, p := range pages {
		for _, c := range p.Claims {
			if keep(c) {
				out = append(out, claimOut{Page: p.ID, ID: c.ID, Text: c.Text,
					Confidence: c.Confidence, Contested: c.Contested})
			}
		}
	}
	sort.Slice(out, func(i, j int) bool {
		if out[i].Page != out[j].Page {
			return out[i].Page < out[j].Page
		}
		return out[i].ID < out[j].ID
	})
	return jsonResult(out)
}

// keywordQuery is the free-text fallback: match on page id, labels, topics.
func (x *Executor) keywordQuery(userID, question string) (*tools.Result, error) {
	q := strings.ToLower(question)
	pages, err := x.pages(userID)
	if err != nil {
		return nil, err
	}
	type hit struct {
		ID     string   `json:"id"`
		Type   string   `json:"type"`
		Labels []string `json:"labels,omitempty"`
	}
	hits := []hit{}
	for _, p := range pages {
		haystack := strings.ToLower(p.ID + " " + strings.Join(p.Labels, " ") + " " + strings.Join(p.Topics, " "))
		for word := range strings.FieldsSeq(q) {
			if strings.Contains(haystack, word) {
				hits = append(hits, hit{ID: p.ID, Type: p.Type, Labels: p.Labels})
				break
			}
		}
	}
	sort.Slice(hits, func(i, j int) bool { return hits[i].ID < hits[j].ID })
	return jsonResult(hits)
}
