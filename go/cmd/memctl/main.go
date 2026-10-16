// memctl is a development/CI tool for wiki memory. It is not part of the
// SDK surface (Go module API, MCP tools, Python/TS SDKs) — it exists so CI
// and humans can run the same validation rules the middleware enforces.
//
// Usage:
//
//	memctl lint -tbox <file.ttl> [-tbox ...] <wiki-dir>
//
// lint exits 1 if any page violates the frontmatter contract or the T-box,
// or if the vault's merged A-box fails the SKOS structural checks.
package main

import (
	"context"
	"flag"
	"fmt"
	"os"
	"path/filepath"
	"strings"

	"github.com/soypete/ontology-go/types"

	"github.com/haikeilabs/agentware/go/mcp"
	"github.com/haikeilabs/agentware/go/memory"
	"github.com/haikeilabs/agentware/go/memory/ontology"
	"github.com/haikeilabs/agentware/go/memory/page"
	"github.com/haikeilabs/agentware/go/middleware"
	"github.com/haikeilabs/agentware/go/tools"
)

func main() {
	if len(os.Args) < 2 {
		usage()
	}
	switch os.Args[1] {
	case "lint":
		os.Exit(lint(os.Args[2:]))
	case "serve":
		os.Exit(serve(os.Args[2:]))
	default:
		usage()
	}
}

func usage() {
	fmt.Fprintln(os.Stderr, `usage:
  memctl lint  -tbox <file.ttl> [-tbox ...] <wiki-dir>
  memctl serve -tbox <file.ttl> [-tbox ...] -root <memory-root> -user <user-id> [-session <id>]`)
	os.Exit(2)
}

// serve exposes the memory tools as an MCP stdio server scoped to one user.
// SDK clients spawn one process per principal; the user can never be
// overridden in-band (the policy denies args.user_id).
func serve(args []string) int {
	fs := flag.NewFlagSet("serve", flag.ExitOnError)
	var paths tboxPaths
	fs.Var(&paths, "tbox", "T-box Turtle file (repeatable)")
	root := fs.String("root", "", "memory root directory")
	user := fs.String("user", "", "vault owner (required)")
	session := fs.String("session", "mcp", "session id for audit records")
	_ = fs.Parse(args)
	if *root == "" || *user == "" || len(paths) == 0 {
		usage()
	}
	wiki, err := memory.Enable(memory.Config{Root: *root, TBoxPaths: paths})
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		return 2
	}
	registry := tools.NewToolRegistry()
	wiki.RegisterTools(registry)
	server := mcp.NewServer(registry, middleware.CallerContext{
		UserID:    *user,
		SessionID: *session,
		Source:    "mcp",
		Trusted:   true,
	})
	if err := server.Serve(context.Background(), os.Stdin, os.Stdout); err != nil {
		fmt.Fprintln(os.Stderr, err)
		return 2
	}
	return 0
}

type tboxPaths []string

func (t *tboxPaths) String() string     { return strings.Join(*t, ",") }
func (t *tboxPaths) Set(v string) error { *t = append(*t, v); return nil }

func lint(args []string) int {
	fs := flag.NewFlagSet("lint", flag.ExitOnError)
	var paths tboxPaths
	fs.Var(&paths, "tbox", "T-box Turtle file (repeatable)")
	_ = fs.Parse(args)
	if fs.NArg() != 1 || len(paths) == 0 {
		usage()
	}
	wikiDir := fs.Arg(0)

	tbox, err := ontology.Load(paths...)
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		return 2
	}

	pages, failures := loadPages(wikiDir)
	pageTypes := make(map[string]string, len(pages))
	for id, p := range pages {
		pageTypes[id] = p.Type
	}
	resolve := func(id string) (string, bool) {
		t, ok := pageTypes[id]
		return t, ok
	}

	for id, p := range pages {
		for _, v := range tbox.ValidatePage(p, resolve, nil) {
			failures++
			fmt.Printf("%s.md: %s\n", id, v)
		}
	}

	violations, err := ontology.CheckSKOSGraph(context.Background(), mergedABox(pages))
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		return 2
	}
	for _, v := range violations {
		failures++
		fmt.Printf("vault: %s\n", v)
	}

	if failures > 0 {
		fmt.Printf("memctl lint: %d violation(s)\n", failures)
		return 1
	}
	fmt.Printf("memctl lint: %d page(s) clean\n", len(pages))
	return 0
}

func loadPages(wikiDir string) (map[string]*page.Page, int) {
	pages := make(map[string]*page.Page)
	failures := 0
	entries, err := os.ReadDir(wikiDir)
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(2)
	}
	for _, e := range entries {
		name := e.Name()
		if e.IsDir() || !strings.HasSuffix(name, ".md") ||
			name == "index.md" || name == "log.md" {
			continue
		}
		data, err := os.ReadFile(filepath.Join(wikiDir, name))
		if err != nil {
			fmt.Printf("%s: %v\n", name, err)
			failures++
			continue
		}
		p, err := page.Parse(data)
		if err != nil {
			fmt.Printf("%s: %v\n", name, err)
			failures++
			continue
		}
		id := strings.TrimSuffix(name, ".md")
		if p.ID != id {
			fmt.Printf("%s: frontmatter id %q does not match filename\n", name, p.ID)
			failures++
			continue
		}
		pages[id] = p
	}
	return pages, failures
}

func mergedABox(pages map[string]*page.Page) []types.Triple {
	var all []types.Triple
	for _, p := range pages {
		triples, err := p.Triples("", nil)
		if err != nil {
			// Unknown-prefix errors are already reported by ValidatePage.
			continue
		}
		all = append(all, triples...)
	}
	return all
}
