// Package memory implements wiki memory: an LLM-maintained,
// ontology-constrained markdown wiki scoped per user, with a Markov link
// network for claim confidence.
//
// Memory is a composable agentware capability, not a standalone product.
// Every memory write passes through the same middleware chain as any tool
// call: an OntologyEvaluator implementing middleware.PolicyEvaluator that
// validates page frontmatter and typed links against a read-only T-box
// (github.com/Soypete/ontologies by default, vendored at ontologies/ in
// this repo) and enforces per-tool runtime rate limits.
//
// On-disk layout, one vault per user:
//
//	<memory_root>/<user_id>/wiki/      typed frontmatter pages
//	<memory_root>/<user_id>/wiki/index.md
//	<memory_root>/<user_id>/wiki/log.md
//	<memory_root>/<user_id>/raw/       ingested sources
//
// User scoping is load-bearing: every memory tool call resolves its vault
// from the CallerContext, and cross-user access is rejected both by policy
// rule and by a path-boundary check in the executor (defense in depth).
//
// The page schema (frontmatter contract, typed wikilink syntax, ingest
// workflow) is documented in SCHEMA.md at the repository root.
package memory
