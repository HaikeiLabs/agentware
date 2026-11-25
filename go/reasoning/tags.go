package reasoning

// Reasoning-tag constants shared by the reasoning adapter and the response
// validator (middleware/guardrails), so both recognize the same tags.
//
// Every tag is built from parts and is never typed as one literal. The tags
// are a blind spot for reasoning-model workers: a model that emits these tags
// itself cannot reliably read or type them (it writes a different tag, or the
// tag comes out garbled), so a literal tag in source is an edit waiting to be
// corrupted. Keep it that way: never write a reasoning tag literally in source,
// tests or docs; build it from parts or use these constants.

const (
	tagLT    = "<"
	tagGT    = ">"
	tagLB    = "["
	tagRB    = "]"
	tagSlash = "/"

	nameThink    = "think"
	nameThinking = nameThink + "ing"
	nameTHINK    = "THINK"
	nameMMThink  = "mm:" + nameThink
)

const (
	// ThinkOpen opens the plain think block that Qwen3, QwQ, DeepSeek-R1
	// and GLM emit when served without a reasoning parser.
	ThinkOpen = tagLT + nameThink + tagGT
	// ThinkClose closes a ThinkOpen block.
	ThinkClose = tagLT + tagSlash + nameThink + tagGT

	// MMThinkOpen opens the mm:think block that MiniMax M3 emits inline in
	// content.
	MMThinkOpen = tagLT + nameMMThink + tagGT
	// MMThinkClose closes a MMThinkOpen block.
	MMThinkClose = tagLT + tagSlash + nameMMThink + tagGT

	// ThinkingOpen opens the generic thinking block.
	ThinkingOpen = tagLT + nameThinking + tagGT
	// ThinkingClose closes a ThinkingOpen block.
	ThinkingClose = tagLT + tagSlash + nameThinking + tagGT

	// BracketThinkOpen opens the bracketed THINK block (Mistral/Magistral).
	BracketThinkOpen = tagLB + nameTHINK + tagRB
	// BracketThinkClose closes a BracketThinkOpen block.
	BracketThinkClose = tagLB + tagSlash + nameTHINK + tagRB
)

// TagPair is one open/close reasoning-tag pair. Matching is case-insensitive.
type TagPair struct {
	Open  string
	Close string
}

// TagPairs returns every reasoning-tag pair the adapter and the validator
// recognize, in a fixed order: think, mm:think, thinking, bracket THINK. It
// returns a new slice on each call, so callers may modify it freely.
func TagPairs() []TagPair {
	return []TagPair{
		{Open: ThinkOpen, Close: ThinkClose},
		{Open: MMThinkOpen, Close: MMThinkClose},
		{Open: ThinkingOpen, Close: ThinkingClose},
		{Open: BracketThinkOpen, Close: BracketThinkClose},
	}
}
