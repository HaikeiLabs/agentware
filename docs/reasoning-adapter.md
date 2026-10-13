# Reasoning Adapter — AR-1 / Linear DVL-21

**Status**: Implemented. Mirrored across Go (`go/reasoning`), Python
(`haikeilabs/agentware/reasoning`), and TypeScript (`typescript/src/reasoning`).

A model-agnostic reasoning adapter. It recognizes reasoning wherever the model
hides it, **strips it before ordinary tool-call parsing**, and normalizes the
extracted reasoning into a **versioned context-tree contract**. Raw reasoning
is never exposed in normal replies or default audits, and the adapter **fails
closed** on malformed or unbounded input.

---

## Scope

This is AR-1 only. It does not cover AR-2+ (streaming reasoning, synthesis,
prompted reasoning disclosure), Kei Proxy changes, assistant production
changes, or npm/PyPI release work.

## What the adapter does

1. **Recognizes three reasoning shapes**
   - **Native**: `reasoning_content` on an OpenAI-compatible message
     (DeepSeek-style; also used by Qwen/QwK, Kimi, GLM).
   - **Generic thinking**: `<thinking>…</thinking>` and `[THINK]…[/THINK]`
     blocks embedded in text (llama.cpp/o1-style; the `[THINK]` form matches
     the guardrails response validator's existing `stripThinkTags`).
   - **Model-specific**: any other named field, registered per model via
     `RegisterModelField` / `register_model_field`.
2. **Strips reasoning before tool-call parsing.** The inference loops in all
   three ports run the adapter on the response before the guardrails
   validator (or native tool-call validation) sees the content. Malformed
   reasoning fails the turn closed (retry), never a partial parse.
3. **Normalizes to a versioned context tree.** Reasoning text is split into
   paragraph nodes that form a deterministic chain (each node's parent is its
   predecessor; the first node is the root). Nodes carry only **bounded
   summaries** — raw reasoning never lands on the tree.

## Context-tree contract (version `"1"`)

```jsonc
{
  "version": "1",
  "format": "native | thinking | model_specific | none",
  "model": "deepseek-reasoner",
  "backend": "llm-backend",
  "created_at": "2026-09-10T12:00:00Z",
  "root_id": "<stable-node-id>",
  "nodes": [
    {
      "id": "<sha256(kind|parent_id|summary), 16 hex chars>",
      "parent_id": "",
      "kind": "goal | observation | decision | tool-call | tool-result | conclusion | blocker",
      "status": "active | complete | blocked | superseded",
      "summary": "bounded to max_summary_chars code points",
      "evidence_refs": ["E1"],
      "tool_call_refs": ["refund_tool"],
      "created_at": "2026-09-10T12:00:00Z",
      "updated_at": "2026-09-10T12:00:00Z"
    }
  ]
}
```

- **Stable node IDs** are `sha256(kind + "\x00" + parent_id + "\x00" +
  summary)` truncated to 16 hex chars. Identical reasoning yields identical
  trees across ports and runs.
- **Kinds** (`goal | observation | decision | tool-call | tool-result |
  conclusion | blocker`) are inferred from leading `label:` / `label=`
  prefixes on a paragraph. Default: `observation`.
- **Status** comes from a trailing `[blocked|pending|active|superseded|
  complete]` marker; `blocker` nodes default to `blocked`, everything else to
  `complete`.
- **Evidence refs** are `[E1]`-style brackets, `ref:`/`ref=`, and
  `evidence:`/`evidence=` tokens. **Tool-call refs** are the leading `name:`
  label (or bare identifier) of `tool-call`/`tool-result` nodes.
- **Timestamps** come from the adapter's clock (injectable for tests).

## Bounds (fail closed)

| Bound | Default | On violation |
| --- | --- | --- |
| `max_reasoning_bytes` | 64 KiB | error — reasoning too large |
| `max_nodes` | 64 | error — too many nodes |
| `max_depth` | 32 | error — tree too deep |
| `max_summary_chars` | 512 | **truncated** (the one bounded field by design) |

Malformed input also fails closed: a reasoning field that is not a string, a
`content` field that is neither a string nor null, unbalanced thinking tags,
and JSON-looking-but-malformed input all raise/return errors. Structured
mappings without any recognized key (e.g. a bare `{"tool": …, "args": …}`
tool call) pass through unchanged.

## "Never expose raw reasoning"

- `llm.Response.Reasoning` (Go `Reasoning`, TS `reasoning`, Python
  `reasoning`) carries reasoning on a **separate channel**; it is never merged
  into `Content`.
- `ContextTree` nodes hold bounded summaries only; the raw text is discarded
  after parsing.
- The default audit record (`AuditRecord` / `AuditRecord`) has **no reasoning
  field** and the middleware never sees LLM responses, so reasoning cannot
  reach a default audit.
- On failed turns the echo message uses the **stripped** content, never the
  raw reasoning.

## Integration points

- **Go** — `go/reasoning` (new package). `go/llm/server.go` decodes
  `reasoning_content` into `Response.Reasoning`. `go/middleware/inference`
  strips reasoning before the validator and attaches the tree to
  `InferenceResult.ReasoningTree`.
- **Python** — `haikeilabs/agentware/reasoning` (new package).
  `llm.Response.reasoning` added; `middleware/inference.py` strips before
  validation and attaches `reasoning_tree` to `InferenceResult`.
- **TypeScript** — `typescript/src/reasoning` (new package).
  `llm/response.ts` adds `reasoning`; `llm/openai_backend.ts` captures
  `reasoning_content`; `middleware/inference.ts` strips before validation and
  attaches `reasoningTree`.
- **TypeScript agent loop** — `typescript/src/executor/agent_loop.ts`
  (`AgentLoop`) applies the same contract at the agent-loop surface: it strips
  native/tagged reasoning before validation, fails closed on malformed or
  unbounded reasoning (the turn is retried as invalid, never partially parsed),
  and attaches the last non-empty bounded tree to
  `AgentResult.reasoning_tree` (null when no turn carried reasoning). Go and Python expose the same capability on
  their inference loops (`InferenceResult.ReasoningTree` /
  `InferenceResult.reasoning_tree`); their formatter-based executors do not
  run the guardrails validator and are out of scope for AR-1.

All three inference loops and the TypeScript `AgentLoop` accept an optional
adapter override (`cfg.Reasoning` / `cfg.reasoning`) so callers can tune
limits and register model-specific fields.

The agent-loop surfaces also share a conversation contract: the system prompt
**leads** the conversation, ahead of any caller-supplied history, then the user
message (`[system, ...history, user]` — Go `buildConversation` is the
reference; pinned by tests in all three ports).

## Model-format matrix and blockers

| Format | Support | Status |
| --- | --- | --- |
| `reasoning_content` (DeepSeek/Qwen/Kimi/GLM style) | Native field, captured by the OpenAI-compatible backends and normalized | **Supported** |
| `<thinking>` / `[THINK]` tags | Generic thinking, stripped before tool-call parsing | **Supported** |
| Model-specific fields (`thinking`, `thinking_content`, `reasoning`, …) | Known set recognized; arbitrary names via `RegisterModelField` | **Supported, unverified per model** |

**Blockers — cannot be confirmed from this repository:**

- **Spark (iFlytek)**: there is no Spark backend or fixture in this
  repository, so the exact reasoning field name and wire shape cannot be
  confirmed. The adapter's known-field set includes `thinking` and the
  `RegisterModelField` hook accepts Spark's actual field once verified against
  a real payload. Until then, treat Spark reasoning as **unconfirmed**.
- **DeepSeek**: `reasoning_content` is the de-facto native field and is
  handled, but the repository has no DeepSeek integration, so the exact
  envelope (message shape, streaming behavior, tool-call coexistence) cannot
  be confirmed here. Verify against a real DeepSeek response before relying on
  it in production.
- **Local (llama.cpp/Ollama)**: local tool-call and reasoning conventions
  vary by build; the generic `<thinking>`/`[THINK]` handling and the existing
  toolformat formatters cover the common shapes, but no local model output is
  captured in this repository to confirm behavior. Exact local reasoning
  formats are **unconfirmed**.

These blockers are deliberate: the adapter never guesses an unverified field
name into a hard behavior — unknown shapes pass through unchanged (the
mapping is treated as opaque text), which is the fail-closed default.

## Rollback / disable

- **Disable stripping** — set the inference adapter to a no-op is not
  supported; instead, remove the reasoning block in
  `go/middleware/inference/inference.go`, `python/.../middleware/inference.py`,
  and `typescript/.../middleware/inference.ts` (each turn re-validates
  `resp.Content` directly and drops the `ReasoningTree` field), or revert this
  change.
- **Revert the whole feature** — the change is additive: `reasoning`/`Reasoning`
  fields on `Response`, the `reasoning` packages, and the inference wiring.
  Undo by reverting the commit touching:
  - `go/reasoning/`, `go/llm/response.go`, `go/llm/server.go`,
    `go/middleware/inference/`
  - `python/src/haikeilabs/agentware/reasoning/`, `llm/response.py`,
    `middleware/inference.py`
  - `typescript/src/reasoning/`, `llm/response.ts`, `llm/openai_backend.ts`,
    `middleware/inference.ts`
- **Unbounded input** — raise `max_reasoning_bytes`/`max_nodes`/`max_depth`
  via the adapter options if legitimate reasoning exceeds the defaults; do not
  disable the bounds.
