// Bounds for the reasoning adapter (mirrors go/reasoning/options.go).
//
// All limits fail closed: an input that exceeds a bound raises an error rather
// than being truncated into a partial tree (summaries are the one bounded
// field by design; they are truncated, never the parse aborted).

export interface ReasoningOptions {
  /** Cap on the size of the extracted reasoning text, in bytes. */
  max_reasoning_bytes?: number;
  /** Cap on the number of nodes a single tree may contain. */
  max_nodes?: number;
  /** Cap on the depth of the node chain. */
  max_depth?: number;
  /** Cap on the length of every node summary, in code points. */
  max_summary_chars?: number;
  /** Clock supplying timestamps for tree and nodes. Defaults to Date.now. */
  clock?: () => Date;
  /** Opts in to close-tag-only reasoning (MiniMax M2, which emits the closing tag without the opening one): when true, everything before a leading think or mm:think close tag counts as reasoning. Only those two closes may open implicitly; every other close tag, and a second stray close, stays unbalanced and fails closed. Defaults to false. */
  implicitOpen?: boolean;
}

export const DEFAULT_MAX_REASONING_BYTES = 64 * 1024;
export const DEFAULT_MAX_NODES = 64;
export const DEFAULT_MAX_DEPTH = 32;
export const DEFAULT_MAX_SUMMARY_CHARS = 512;

export function normalizeOptions(opts?: ReasoningOptions): Required<ReasoningOptions> {
  return {
    max_reasoning_bytes: opts?.max_reasoning_bytes ?? DEFAULT_MAX_REASONING_BYTES,
    max_nodes: opts?.max_nodes ?? DEFAULT_MAX_NODES,
    max_depth: opts?.max_depth ?? DEFAULT_MAX_DEPTH,
    max_summary_chars: opts?.max_summary_chars ?? DEFAULT_MAX_SUMMARY_CHARS,
    clock: opts?.clock ?? (() => new Date()),
    implicitOpen: opts?.implicitOpen ?? false,
  };
}
