package memory

import (
	"context"
	"fmt"

	"github.com/haikeilabs/agentware/go/memory/ontology"
	"github.com/haikeilabs/agentware/go/middleware"
	"github.com/haikeilabs/agentware/go/tools"
)

// Config configures the composable wiki-memory component.
//
// # TBoxPaths - Providing Your Own Ontology
//
// The T-box (schema) is fully composable — pass paths to your own TTL files.
// Multiple files are merged. Use absolute paths or paths relative to your
// working directory.
//
// Example TTL file (my-ontology.ttl):
//
//	@prefix ex: <http://example.org/> .
//	@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
//	@prefix owl: <http://www.w3.org/2002/07/owl#> .
//
//	ex:Project a owl:Class ;
//	    rdfs:label "Project"@en ;
//	    rdfs:comment "A software project" .
//
//	ex:Task a owl:Class ;
//	    rdfs:label "Task"@en ;
//	    rdfs:subClassOf ex:Project .
//
//	ex:hasStatus a owl:ObjectProperty ;
//	    rdfs:domain ex:Project ;
//	    rdfs:range ex:Status .
//
//	ex:Status a owl:Class ;
//	    rdfs:label "Status"@en .
//
// Example usage:
//
//	wiki, err := memory.Enable(memory.Config{
//	    Root: "./memory",
//	    TBoxPaths: []string{
//	        "./my-ontology.ttl",           // your custom ontology
//	        "./shared-skills.ttl",         // another TTL file
//	    },
//	})
//
// The repo includes example ontologies in ontologies/:
//   - ontologies/education/TBOX_LEARNING_SOFTWARE.ttl (learning software concepts)
//   - ontologies/social/twitch_topics.ttl (SKOS concepts)
//   - ontologies/thesaurus/*.ttl (SKOS testing)
type Config struct {
	// Root is the memory root directory (one vault per user beneath it).
	Root string
	// TBoxPaths are Turtle files forming the read-only T-box.
	// Pass paths to your own .ttl files — multiple files are merged.
	// Required: no default is provided; the T-box is a parameter.
	TBoxPaths []string
	// Prefixes overrides the CURIE prefix map (SCHEMA.md defaults).
	Prefixes map[string]string
	// Policy overrides the embedded declarative policy.
	Policy *middleware.Policy
	// Auditor records every decision. Defaults to an in-memory auditor.
	Auditor middleware.Auditor
}

// WikiMemory is the enabled component: the five memory tools behind the
// standard middleware chain (declarative policy + ontology evaluator +
// auditor). Enable it and register its tools; every call is enforced and
// audited.
type WikiMemory struct {
	vault   *Vault
	tbox    *ontology.TBox
	exec    *Executor
	auditor middleware.Auditor
	chain   middleware.Middleware
}

// Enable builds the component: vault, T-box, executor, evaluator, and the
// middleware chain wired as NewMiddleware(exec).WithPolicy(...).WithAuditor(...).
func Enable(cfg Config) (*WikiMemory, error) {
	if len(cfg.TBoxPaths) == 0 {
		return nil, fmt.Errorf("memory: Config.TBoxPaths is required (the T-box is a parameter, e.g. the ontologies/ submodule files)")
	}
	vault, err := NewVault(cfg.Root)
	if err != nil {
		return nil, err
	}
	tbox, err := ontology.Load(cfg.TBoxPaths...)
	if err != nil {
		return nil, err
	}
	exec := NewExecutor(vault, tbox, cfg.Prefixes)
	opts := []EvaluatorOption{WithVaultReader(exec)}
	if cfg.Prefixes != nil {
		opts = append(opts, WithPrefixes(cfg.Prefixes))
	}
	if cfg.Policy != nil {
		opts = append(opts, WithDeclarativePolicy(cfg.Policy))
	}
	auditor := cfg.Auditor
	if auditor == nil {
		auditor = middleware.NewInMemoryAuditor()
	}
	chain := middleware.NewMiddleware(exec).
		WithPolicy(NewOntologyEvaluator(tbox, opts...)).
		WithAuditor(auditor)
	return &WikiMemory{vault: vault, tbox: tbox, exec: exec, auditor: auditor, chain: chain}, nil
}

// Execute runs a memory tool through the enforced chain. The caller must be
// attached to ctx via middleware.WithCallerContext.
func (w *WikiMemory) Execute(ctx context.Context, toolName string, args map[string]any) (*tools.Result, error) {
	return w.chain.Execute(ctx, toolName, args)
}

// Auditor returns the auditor recording this component's decisions.
func (w *WikiMemory) Auditor() middleware.Auditor { return w.auditor }

// Vault returns the underlying vault (path resolution only; writes must go
// through Execute).
func (w *WikiMemory) Vault() *Vault { return w.vault }

// TBox returns the loaded terminology.
func (w *WikiMemory) TBox() *ontology.TBox { return w.tbox }

// RegisterTools registers the five memory tools on a tool registry, each
// dispatching through the enforced chain.
func (w *WikiMemory) RegisterTools(registry *tools.ToolRegistry) {
	descriptions := map[string]string{
		ToolIngest:    "Store a raw source document in the caller's memory vault (args: source_id, text).",
		ToolWritePage: "Create or update an ontology-validated wiki page in the caller's vault (args: content, optional page_id).",
		ToolQuery:     "Query the caller's wiki memory (args: question).",
		ToolGetClaims: "List claims tracked in the caller's vault (args: optional page_id).",
		ToolLint:      "Report contract, ontology, and graph issues in the caller's vault.",
	}
	for _, name := range Tools() {
		registry.Register(&memoryTool{name: name, description: descriptions[name], wiki: w})
	}
}

type memoryTool struct {
	name        string
	description string
	wiki        *WikiMemory
}

func (t *memoryTool) Name() string        { return t.name }
func (t *memoryTool) Description() string { return t.description }
func (t *memoryTool) Execute(ctx context.Context, args map[string]any) (*tools.Result, error) {
	return t.wiki.Execute(ctx, t.name, args)
}
