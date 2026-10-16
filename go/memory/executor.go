package memory

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"time"

	"github.com/soypete/ontology-go/types"

	"github.com/haikeilabs/agentware/go/memory/ontology"
	"github.com/haikeilabs/agentware/go/memory/page"
	"github.com/haikeilabs/agentware/go/middleware"
	"github.com/haikeilabs/agentware/go/tools"
)

// ErrNoCaller reports a memory tool call with no caller in the context.
// The declarative policy denies these before execution; the executor
// re-checks as defense in depth.
var ErrNoCaller = errors.New("memory: no caller context")

// Executor executes the memory tools against a per-user vault. It
// implements middleware.ToolExecutor and VaultReader. It performs no policy
// evaluation itself, but every filesystem path is re-checked against the
// caller's vault boundary (defense in depth alongside the policy tier).
type Executor struct {
	vault    *Vault
	tbox     *ontology.TBox
	prefixes map[string]string
}

// NewExecutor builds an Executor over vault, validating reads against tbox.
func NewExecutor(vault *Vault, tbox *ontology.TBox, prefixes map[string]string) *Executor {
	if prefixes == nil {
		prefixes = page.DefaultPrefixes()
	}
	return &Executor{vault: vault, tbox: tbox, prefixes: prefixes}
}

// Execute implements middleware.ToolExecutor.
func (x *Executor) Execute(ctx context.Context, toolName string, args map[string]any) (*tools.Result, error) {
	caller, ok := middleware.CallerFromContext(ctx)
	if !ok || caller.UserID == "" {
		return nil, ErrNoCaller
	}
	userID := caller.UserID
	if err := x.vault.EnsureUser(userID); err != nil {
		return nil, err
	}
	switch toolName {
	case ToolWritePage:
		return x.writePage(userID, args)
	case ToolIngest:
		return x.ingest(userID, args)
	case ToolQuery:
		return x.query(userID, args)
	case ToolGetClaims:
		return x.getClaims(userID, args)
	case ToolLint:
		return x.lint(userID, args)
	default:
		return &tools.Result{Success: false, Error: "unknown memory tool: " + toolName}, nil
	}
}

func stringArg(args map[string]any, key string) string {
	s, _ := args[key].(string)
	return s
}

func (x *Executor) writePage(userID string, args map[string]any) (*tools.Result, error) {
	content := stringArg(args, "content")
	p, err := page.Parse([]byte(content))
	if err != nil {
		return &tools.Result{Success: false, Error: err.Error()}, nil
	}
	path, err := x.vault.PagePath(userID, p.ID)
	if err != nil {
		return &tools.Result{Success: false, Error: err.Error()}, nil
	}
	if !x.vault.Contains(userID, path) {
		return nil, fmt.Errorf("%w: %s", ErrOutsideVault, path)
	}
	if err := os.WriteFile(path, []byte(content), 0o644); err != nil {
		return nil, fmt.Errorf("memory: write page: %w", err)
	}
	if err := x.refreshIndex(userID); err != nil {
		return nil, err
	}
	if err := x.appendLog(userID, "wrote page "+p.ID); err != nil {
		return nil, err
	}
	return &tools.Result{
		Success:       true,
		Output:        "wrote " + p.ID,
		ModifiedFiles: []string{path},
	}, nil
}

func (x *Executor) ingest(userID string, args map[string]any) (*tools.Result, error) {
	sourceID := stringArg(args, "source_id")
	text := stringArg(args, "text")
	if !pageIDPattern.MatchString(sourceID) {
		return &tools.Result{Success: false,
			Error: fmt.Sprintf("memory: source_id %q is not kebab-case", sourceID)}, nil
	}
	if strings.TrimSpace(text) == "" {
		return &tools.Result{Success: false, Error: "memory: ingest requires args.text"}, nil
	}
	raw, err := x.vault.RawDir(userID)
	if err != nil {
		return nil, err
	}
	path := filepath.Join(raw, sourceID+".md")
	if !x.vault.Contains(userID, path) {
		return nil, fmt.Errorf("%w: %s", ErrOutsideVault, path)
	}
	if err := os.WriteFile(path, []byte(text), 0o644); err != nil {
		return nil, fmt.Errorf("memory: write source: %w", err)
	}
	if err := x.appendLog(userID, "ingested source "+sourceID); err != nil {
		return nil, err
	}
	return &tools.Result{Success: true, Output: "ingested " + sourceID,
		ModifiedFiles: []string{path}}, nil
}

// query answers competency questions via args.kind (see query.go) or falls
// back to keyword matching over args.question.
func (x *Executor) query(userID string, args map[string]any) (*tools.Result, error) {
	if kind := stringArg(args, "kind"); kind != "" {
		return x.structuredQuery(userID, kind, args)
	}
	return x.keywordQuery(userID, stringArg(args, "question"))
}

func (x *Executor) getClaims(userID string, args map[string]any) (*tools.Result, error) {
	wantPage := stringArg(args, "page_id")
	pages, err := x.pages(userID)
	if err != nil {
		return nil, err
	}
	type claimOut struct {
		Page        string   `json:"page"`
		ID          string   `json:"id"`
		Text        string   `json:"text"`
		Sources     []string `json:"sources,omitempty"`
		Supports    []string `json:"supports,omitempty"`
		Contradicts []string `json:"contradicts,omitempty"`
		Confidence  *float64 `json:"confidence"`
		Contested   bool     `json:"contested,omitempty"`
	}
	out := []claimOut{}
	for _, p := range pages {
		if wantPage != "" && p.ID != wantPage {
			continue
		}
		for _, c := range p.Claims {
			out = append(out, claimOut{Page: p.ID, ID: c.ID, Text: c.Text,
				Sources: c.Sources, Supports: c.Supports, Contradicts: c.Contradicts,
				Confidence: c.Confidence, Contested: c.Contested})
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

// Lint-only constraint identifiers (write-time enforcement never blocks on
// these; they are hygiene findings for the maintaining agent).
const (
	LintDanglingLink     = "dangling-link"
	LintOrphanPage       = "orphan-page"
	LintStalePage        = "stale-page"
	LintMissingTypedLink = "missing-typed-links"
)

// defaultStaleDays is the lint threshold for the `updated` frontmatter date.
const defaultStaleDays = 90

func (x *Executor) lint(userID string, args map[string]any) (*tools.Result, error) {
	pages, err := x.pages(userID)
	if err != nil {
		return nil, err
	}
	resolve := func(id string) (string, bool) {
		for _, p := range pages {
			if p.ID == id {
				return p.Type, true
			}
		}
		return "", false
	}
	staleDays := defaultStaleDays
	if d, ok := args["stale_days"].(float64); ok && d > 0 {
		staleDays = int(d)
	}
	staleBefore := time.Now().AddDate(0, 0, -staleDays)

	type lintIssue struct {
		Page      string `json:"page,omitempty"`
		Violation ontology.Violation
	}
	issues := []lintIssue{}
	var abox []types.Triple
	incoming := map[string]bool{}
	for _, p := range pages {
		for _, link := range p.AllLinks() {
			incoming[link.Target] = true
		}
	}
	for _, p := range pages {
		for _, v := range x.tbox.ValidatePage(p, resolve, x.prefixes) {
			issues = append(issues, lintIssue{Page: p.ID, Violation: v})
		}
		if triples, err := p.Triples("", x.prefixes); err == nil {
			abox = append(abox, triples...)
		}
		typedLinks := 0
		for _, link := range p.AllLinks() {
			if _, ok := resolve(link.Target); !ok {
				issues = append(issues, lintIssue{Page: p.ID, Violation: ontology.Violation{
					Constraint: LintDanglingLink,
					Term:       link.Target,
					Message:    fmt.Sprintf("link target %q is mentioned but has no page", link.Target),
				}})
			}
			if link.Pred != page.DefaultLinkPred {
				typedLinks++
			}
		}
		if len(pages) > 1 && !incoming[p.ID] && len(p.AllLinks()) == 0 {
			issues = append(issues, lintIssue{Page: p.ID, Violation: ontology.Violation{
				Constraint: LintOrphanPage,
				Term:       p.ID,
				Message:    "page has no links in or out",
			}})
		}
		if typedLinks == 0 && len(p.AllLinks()) > 0 {
			issues = append(issues, lintIssue{Page: p.ID, Violation: ontology.Violation{
				Constraint: LintMissingTypedLink,
				Term:       p.ID,
				Message:    "page links only via bare skos:related; add typed predicates",
			}})
		}
		if p.Updated != "" {
			if updated, err := time.Parse("2006-01-02", p.Updated); err == nil && updated.Before(staleBefore) {
				issues = append(issues, lintIssue{Page: p.ID, Violation: ontology.Violation{
					Constraint: LintStalePage,
					Term:       p.ID,
					Message:    fmt.Sprintf("last updated %s (over %d days ago)", p.Updated, staleDays),
				}})
			}
		}
	}
	graphViolations, err := ontology.CheckSKOSGraph(context.Background(), abox)
	if err != nil {
		return nil, err
	}
	for _, v := range graphViolations {
		issues = append(issues, lintIssue{Violation: v})
	}
	return jsonResult(issues)
}

// PageType implements VaultReader.
func (x *Executor) PageType(userID, pageID string) (string, bool) {
	p, err := x.readPage(userID, pageID)
	if err != nil {
		return "", false
	}
	return p.Type, true
}

// VaultTriples implements VaultReader.
func (x *Executor) VaultTriples(userID, excludePageID string) ([]types.Triple, error) {
	pages, err := x.pages(userID)
	if err != nil {
		return nil, err
	}
	var all []types.Triple
	for _, p := range pages {
		if p.ID == excludePageID {
			continue
		}
		triples, err := p.Triples("", x.prefixes)
		if err != nil {
			continue
		}
		all = append(all, triples...)
	}
	return all, nil
}

func (x *Executor) readPage(userID, pageID string) (*page.Page, error) {
	path, err := x.vault.PagePath(userID, pageID)
	if err != nil {
		return nil, err
	}
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	return page.Parse(data)
}

// pages parses every page in the user's wiki, skipping index.md, log.md,
// and unparseable files (memory_lint reports those separately).
func (x *Executor) pages(userID string) ([]*page.Page, error) {
	wiki, err := x.vault.WikiDir(userID)
	if err != nil {
		return nil, err
	}
	entries, err := os.ReadDir(wiki)
	if err != nil {
		if errors.Is(err, os.ErrNotExist) {
			return nil, nil
		}
		return nil, err
	}
	var pages []*page.Page
	for _, e := range entries {
		name := e.Name()
		if e.IsDir() || !strings.HasSuffix(name, ".md") || name == "index.md" || name == "log.md" {
			continue
		}
		data, err := os.ReadFile(filepath.Join(wiki, name))
		if err != nil {
			continue
		}
		p, err := page.Parse(data)
		if err != nil {
			continue
		}
		pages = append(pages, p)
	}
	sort.Slice(pages, func(i, j int) bool { return pages[i].ID < pages[j].ID })
	return pages, nil
}

func (x *Executor) refreshIndex(userID string) error {
	pages, err := x.pages(userID)
	if err != nil {
		return err
	}
	var sb strings.Builder
	sb.WriteString("# Wiki Index\n\n")
	if len(pages) == 0 {
		sb.WriteString("No pages yet.\n")
	}
	for _, p := range pages {
		label := p.ID
		if len(p.Labels) > 0 {
			label = p.Labels[0]
		}
		fmt.Fprintf(&sb, "- [%s](%s.md) — %s\n", label, p.ID, p.Type)
	}
	wiki, err := x.vault.WikiDir(userID)
	if err != nil {
		return err
	}
	return os.WriteFile(filepath.Join(wiki, "index.md"), []byte(sb.String()), 0o644)
}

func (x *Executor) appendLog(userID, entry string) error {
	wiki, err := x.vault.WikiDir(userID)
	if err != nil {
		return err
	}
	f, err := os.OpenFile(filepath.Join(wiki, "log.md"), os.O_APPEND|os.O_CREATE|os.O_WRONLY, 0o644)
	if err != nil {
		return err
	}
	_, err = fmt.Fprintf(f, "- [%s] %s\n", time.Now().UTC().Format("2006-01-02 15:04:05"), entry)
	return errors.Join(err, f.Close())
}

func jsonResult(v any) (*tools.Result, error) {
	data, err := json.MarshalIndent(v, "", "  ")
	if err != nil {
		return nil, err
	}
	return &tools.Result{Success: true, Output: string(data)}, nil
}
