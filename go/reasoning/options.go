package reasoning

import "time"

// Options bound the reasoning adapter's behavior. All limits fail closed: an
// input that exceeds a bound is rejected with an error rather than being
// truncated into a partial tree (summaries are the one bounded field by
// design; they are truncated, never the parse aborted).
type Options struct {
	// MaxReasoningBytes caps the size of the extracted reasoning text.
	// Input whose reasoning exceeds this bound is rejected. Zero means the
	// default (64 KiB).
	MaxReasoningBytes int
	// MaxNodes caps the number of nodes a single tree may contain. Zero
	// means the default (64).
	MaxNodes int
	// MaxDepth caps the depth of the node chain. Zero means the default (32).
	MaxDepth int
	// MaxSummaryChars caps the length of every node summary. Zero means the
	// default (512).
	MaxSummaryChars int
	// Clock supplies timestamps for tree and nodes. Zero uses time.Now.
	Clock func() time.Time
	// ImplicitOpen opts in to close-tag-only reasoning (MiniMax M2, which
	// emits the closing tag without the opening one): when true, everything
	// before a leading think or mm:think close tag counts as reasoning.
	// Only those two closes may open implicitly; every other close tag, and
	// a second stray close, stays unbalanced and fails closed. Zero means
	// false.
	ImplicitOpen bool
}

// DefaultOptions returns the adapter limits used when no Options are given.
func DefaultOptions() Options {
	return Options{
		MaxReasoningBytes: 64 * 1024,
		MaxNodes:          64,
		MaxDepth:          32,
		MaxSummaryChars:   512,
	}
}

func (o Options) normalize() Options {
	d := DefaultOptions()
	if o.MaxReasoningBytes <= 0 {
		o.MaxReasoningBytes = d.MaxReasoningBytes
	}
	if o.MaxNodes <= 0 {
		o.MaxNodes = d.MaxNodes
	}
	if o.MaxDepth <= 0 {
		o.MaxDepth = d.MaxDepth
	}
	if o.MaxSummaryChars <= 0 {
		o.MaxSummaryChars = d.MaxSummaryChars
	}
	if o.Clock == nil {
		o.Clock = time.Now
	}
	return o
}
