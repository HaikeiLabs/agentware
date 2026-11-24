// Package reasoning provides a model-agnostic reasoning adapter.
//
// The adapter recognizes three shapes of model output:
//
//   - native reasoning_content fields (DeepSeek/Qwen/Kimi/GLM-style JSON)
//   - generic reasoning tags (every pair in TagPairs: think, thinking and
//     bracket THINK; see tags.go)
//   - model-specific reasoning fields (registered per model name)
//
// Reasoning is stripped from the model output before ordinary tool-call
// parsing, and the extracted reasoning text is normalized into a versioned
// context tree. Raw reasoning is never exposed in normal replies or default
// audits: tree nodes carry only bounded summaries.
//
// The adapter fails closed. Malformed input (a reasoning field that is not a
// string, unbalanced thinking tags, malformed JSON) and unbounded input
// (reasoning larger than the configured byte cap, more nodes or more depth
// than configured) return errors instead of emitting partial reasoning.
package reasoning

import "time"

// ContractVersion is the version of the context-tree contract this package
// emits. Bump it only when the wire shape of ContextTree/Node changes.
const ContractVersion = "1"

// Format identifies the shape of the input that produced a ContextTree.
type Format string

const (
	// FormatNative is an OpenAI-compatible response carrying a native
	// reasoning_content field.
	FormatNative Format = "native"
	// FormatThinking is text with embedded generic thinking tags.
	FormatThinking Format = "thinking"
	// FormatModelSpecific is a response carrying a model-specific reasoning
	// field registered with RegisterModelField.
	FormatModelSpecific Format = "model_specific"
	// FormatNone means no reasoning was detected.
	FormatNone Format = "none"
)

// NodeKind is the closed vocabulary of context-tree node kinds.
type NodeKind string

const (
	KindGoal        NodeKind = "goal"
	KindObservation NodeKind = "observation"
	KindDecision    NodeKind = "decision"
	KindToolCall    NodeKind = "tool-call"
	KindToolResult  NodeKind = "tool-result"
	KindConclusion  NodeKind = "conclusion"
	KindBlocker     NodeKind = "blocker"
)

// NodeStatus is the closed vocabulary of context-tree node statuses.
type NodeStatus string

const (
	StatusActive     NodeStatus = "active"
	StatusComplete   NodeStatus = "complete"
	StatusBlocked    NodeStatus = "blocked"
	StatusSuperseded NodeStatus = "superseded"
)

// Node is a single node in the reasoning context tree. Nodes carry bounded
// summaries only; raw reasoning text never appears on a node.
type Node struct {
	ID           string     `json:"id"`
	ParentID     string     `json:"parent_id"`
	Kind         NodeKind   `json:"kind"`
	Status       NodeStatus `json:"status"`
	Summary      string     `json:"summary"`
	EvidenceRefs []string   `json:"evidence_refs,omitempty"`
	ToolCallRefs []string   `json:"tool_call_refs,omitempty"`
	CreatedAt    time.Time  `json:"created_at"`
	UpdatedAt    time.Time  `json:"updated_at"`
}

// ContextTree is the versioned, normalized representation of a model's
// reasoning. RootID points at the root node (the first node when the parser
// produced the tree); it is empty when no reasoning was detected.
type ContextTree struct {
	Version   string    `json:"version"`
	Format    Format    `json:"format"`
	Model     string    `json:"model"`
	Backend   string    `json:"backend"`
	CreatedAt time.Time `json:"created_at"`
	RootID    string    `json:"root_id"`
	Nodes     []Node    `json:"nodes"`
}

// Empty reports whether the tree holds no reasoning nodes.
func (t *ContextTree) Empty() bool {
	return t == nil || len(t.Nodes) == 0
}
