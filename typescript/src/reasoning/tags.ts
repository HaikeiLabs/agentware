// Reasoning-tag constants shared by the reasoning adapter and the response
// validator (middleware/guardrails), so both recognize the same tags.
//
// Every tag is built from parts and is never typed as one literal. The tags
// are a blind spot for reasoning-model workers: a model that emits these tags
// itself cannot reliably read or type them (it writes a different tag, or the
// tag comes out garbled), so a literal tag in source is an edit waiting to be
// corrupted. Keep it that way: never write a reasoning tag literally in
// source, tests or docs; build it from parts or use these constants.
//
// Mirrors go/reasoning/tags.go and python/.../reasoning/tags.py.

const LT = "<";
const GT = ">";
const LB = "[";
const RB = "]";
const SLASH = "/";

const THINK = "think";
const THINKING = THINK + "ing";
const BRACKET_THINK = THINK.toUpperCase();
const MM_THINK = "mm:" + THINK;

/** Opens the plain think block that Qwen3, QwQ, DeepSeek-R1 and GLM emit when served without a reasoning parser. */
export const THINK_OPEN = LT + THINK + GT;
/** Closes a {@link THINK_OPEN} block. */
export const THINK_CLOSE = LT + SLASH + THINK + GT;

/** Opens the mm:think block that MiniMax M3 emits inline in content. */
export const MMTHINK_OPEN = LT + MM_THINK + GT;
/** Closes a {@link MMTHINK_OPEN} block. */
export const MMTHINK_CLOSE = LT + SLASH + MM_THINK + GT;

/** Opens the generic thinking block. */
export const THINKING_OPEN = LT + THINKING + GT;
/** Closes a {@link THINKING_OPEN} block. */
export const THINKING_CLOSE = LT + SLASH + THINKING + GT;

/** Opens the bracketed THINK block (Mistral/Magistral). */
export const BRACKET_THINK_OPEN = LB + BRACKET_THINK + RB;
/** Closes a {@link BRACKET_THINK_OPEN} block. */
export const BRACKET_THINK_CLOSE = LB + SLASH + BRACKET_THINK + RB;

/** One open/close reasoning-tag pair. Matching is case-insensitive. */
export interface ReasoningTagPair {
  readonly open: string;
  readonly close: string;
}

/**
 * Every reasoning-tag pair the adapter and the validator recognize, in a
 * fixed order: think, mm:think, thinking, bracket THINK. Frozen.
 */
export const REASONING_TAG_PAIRS: readonly ReasoningTagPair[] = Object.freeze([
  Object.freeze({ open: THINK_OPEN, close: THINK_CLOSE }),
  Object.freeze({ open: MMTHINK_OPEN, close: MMTHINK_CLOSE }),
  Object.freeze({ open: THINKING_OPEN, close: THINKING_CLOSE }),
  Object.freeze({ open: BRACKET_THINK_OPEN, close: BRACKET_THINK_CLOSE }),
]);

/** Escapes a string for literal use inside a RegExp source. */
export function escapeRegExp(text: string): string {
  return text.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}
