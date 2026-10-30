// Model-agnostic reasoning adapter (mirrors go/reasoning/adapter.go).
//
// The adapter recognizes three shapes of model output:
//   - native reasoning_content fields (DeepSeek/Qwen/Kimi/GLM-style JSON)
//   - generic thinking tags (<thinking>...</thinking>, [THINK]...[/THINK])
//   - model-specific reasoning fields (registered per model name)
//
// Reasoning is stripped before ordinary tool-call parsing, and the extracted
// reasoning text is normalized into a versioned context tree. Raw reasoning is
// never exposed in normal replies or default audits: tree nodes carry only
// bounded summaries. The adapter fails closed on malformed or unbounded input.

import { createHash } from "node:crypto";

import {
  MalformedContentError,
  MalformedInputError,
  MalformedJSONError,
  MalformedReasoningFieldError,
  ReasoningError,
  ReasoningTooLargeError,
  TooDeepError,
  TooManyNodesError,
  UnbalancedThinkingError,
} from "./errors.js";
import { normalizeOptions, ReasoningOptions } from "./options.js";
import { CONTRACT_VERSION, ContextTree, Format, Node, NodeKind, NodeStatus } from "./types.js";

export { ReasoningError };

// Known reasoning field names on a structured response, in priority order.
// reasoning_content is the de-facto native field (DeepSeek, Qwen/QwQ, Kimi,
// GLM); the others cover common model-specific names.
const KNOWN_REASONING_FIELDS = ["reasoning_content", "thinking", "thinking_content", "reasoning"];

// Keys that make a structured response worth entering the structured path. A
// mapping without any of these is treated as opaque text and returned
// unchanged.
const RECOGNIZED_KEYS = new Set([
  "message",
  "choices",
  "content",
  "reasoning_content",
  "thinking",
  "thinking_content",
  "reasoning",
]);

const THINKING_OPEN = /<thinking>|\[THINK\]/i;
const THINKING_CLOSE = /<\/thinking>|\[\/THINK\]/i;

const PARAGRAPH_SEP = /\n[ \t]*\n/;
const LIST_ORNAMENT = /^\s*(?:[-*+•]|\d+[.)])\s+/;
const KIND_PREFIX =
  /^\s*(goal|observation|decision|tool-call|tool_call|tool-result|tool_result|tool|conclusion|blocker)\s*[:=]\s*/i;
const STATUS_MARKER = /\s*\[(blocked|pending|active|superseded|complete)\]\s*$/i;
const EVIDENCE_PATTERNS = [
  /\[E\d+\]/g,
  /\bref\s*[=:]\s*(\S+)/g,
  /\bevidence\s*[=:]\s*(\S+)/g,
];
const TOOL_REF = /\b(?:tool|call)\s*[=:]\s*([a-zA-Z_][a-zA-Z0-9_.-]*)/;
const LEADING_IDENTIFIER = /^([a-zA-Z_][a-zA-Z0-9_.-]*)\s*[:=]/;
const BARE_IDENTIFIER = /^[a-zA-Z_][a-zA-Z0-9_.-]*$/;

const KIND_BY_LABEL: Record<string, NodeKind> = {
  goal: NodeKind.GOAL,
  observation: NodeKind.OBSERVATION,
  decision: NodeKind.DECISION,
  "tool-call": NodeKind.TOOL_CALL,
  tool_call: NodeKind.TOOL_CALL,
  tool: NodeKind.TOOL_CALL,
  "tool-result": NodeKind.TOOL_RESULT,
  tool_result: NodeKind.TOOL_RESULT,
  conclusion: NodeKind.CONCLUSION,
  blocker: NodeKind.BLOCKER,
};

const STATUS_BY_LABEL: Record<string, NodeStatus> = {
  blocked: NodeStatus.BLOCKED,
  pending: NodeStatus.ACTIVE,
  active: NodeStatus.ACTIVE,
  superseded: NodeStatus.SUPERSEDED,
  complete: NodeStatus.COMPLETE,
};

interface Analysis {
  reasoning: string;
  clean: string;
  format: Format;
}

function isRecord(v: unknown): v is Record<string, unknown> {
  return typeof v === "object" && v !== null && !Array.isArray(v);
}

export class ReasoningAdapter {
  private readonly opts: Required<ReasoningOptions>;
  private readonly modelFields: Map<string, string>;

  constructor(options?: ReasoningOptions) {
    this.opts = normalizeOptions(options);
    this.modelFields = new Map();
  }

  get options(): Required<ReasoningOptions> {
    return this.opts;
  }

  /** Map a model name to a model-specific reasoning field name. Fields
   * registered here are consulted before the known field set. */
  registerModelField(model: string, fieldName: string): void {
    if (model && fieldName) {
      this.modelFields.set(model.toLowerCase(), fieldName);
    }
  }

  /** Remove reasoning from raw and return the clean content that ordinary
   * tool-call parsers should see. Fails closed. */
  strip(raw: unknown): string {
    return this.analyze(raw, "").clean;
  }

  /** Normalize reasoning found in raw into a versioned context tree. Raw
   * reasoning is never stored on the tree: nodes carry bounded summaries. */
  extract(raw: unknown, model = "", backend = ""): ContextTree {
    const analysis = this.analyze(raw, model);
    const now = this.opts.clock();

    const tree: ContextTree = {
      version: CONTRACT_VERSION,
      format: analysis.format,
      model,
      backend,
      created_at: now,
      root_id: "",
      nodes: [],
    };

    if (analysis.reasoning.trim() === "") {
      return tree;
    }

    const nodes = this.parseNodes(analysis.reasoning, now);
    tree.nodes = nodes;
    if (nodes.length > 0) {
      tree.root_id = nodes[0].id;
    }
    return tree;
  }

  /** Strip and extract in one call: return the clean content and tree. */
  parse(raw: unknown, model = "", backend = ""): { clean: string; tree: ContextTree } {
    const analysis = this.analyze(raw, model);
    return { clean: analysis.clean, tree: this.extract(raw, model, backend) };
  }

  private analyze(raw: unknown, model: string): Analysis {
    if (typeof raw === "string") {
      return this.analyzeString(raw, model);
    }
    if (raw instanceof Uint8Array) {
      return this.analyzeBytes(raw, model);
    }
    if (isRecord(raw)) {
      return this.analyzeStructured(raw, model);
    }
    throw new MalformedInputError(`unsupported raw type ${typeof raw}`);
  }

  private analyzeString(raw: string, model: string): Analysis {
    const trimmed = raw.trim();
    if (trimmed.startsWith("{")) {
      let parsed: unknown;
      try {
        parsed = JSON.parse(trimmed);
      } catch (e) {
        throw new MalformedJSONError(e instanceof Error ? e.message : String(e));
      }
      if (isRecord(parsed) && this.hasRecognizedKey(parsed)) {
        return this.analyzeStructured(parsed, model);
      }
    }
    return this.analyzeText(raw);
  }

  private analyzeBytes(raw: Uint8Array, model: string): Analysis {
    const trimmed = raw.length > 0 ? new TextDecoder().decode(raw).trim() : "";
    if (trimmed.startsWith("{") || trimmed.startsWith("[")) {
      let parsed: unknown;
      try {
        parsed = JSON.parse(trimmed);
      } catch (e) {
        throw new MalformedJSONError(e instanceof Error ? e.message : String(e));
      }
      if (isRecord(parsed) && this.hasRecognizedKey(parsed)) {
        return this.analyzeStructured(parsed, model);
      }
    }
    return this.analyzeText(trimmed);
  }

  private analyzeStructured(m: Record<string, unknown>, model: string): Analysis {
    const message = m["message"];
    if (isRecord(message)) {
      return this.analyzeStructured(message, model);
    }

    const choices = m["choices"];
    if (Array.isArray(choices) && choices.length > 0 && isRecord(choices[0])) {
      const choiceMessage = choices[0]["message"];
      if (isRecord(choiceMessage)) return this.analyzeStructured(choiceMessage, model);
    }

    let content = "";
    let partReasoning = "";
    if ("content" in m) {
      const value = m["content"];
      if (value === null || value === undefined) {
        content = "";
      } else if (typeof value === "string") {
        content = value;
      } else if (Array.isArray(value)) {
        const textParts: string[] = [];
        const thinkingParts: string[] = [];
        for (const part of value) {
          if (!isRecord(part)) throw new MalformedContentError("content part is not an object");
          if (part["type"] === "text") {
            if (typeof part["text"] !== "string") {
              throw new MalformedContentError("text part has no string text");
            }
            textParts.push(part["text"]);
          } else if (part["type"] === "thinking") {
            if (typeof part["thinking"] !== "string") {
              throw new MalformedReasoningFieldError("thinking part has no string thinking");
            }
            thinkingParts.push(part["thinking"]);
          }
        }
        content = textParts.join("\n");
        partReasoning = thinkingParts.join("\n");
      } else {
        throw new MalformedContentError(`content field is ${typeof value}, not a string`);
      }
    }

    let [reasoning, format] = this.extractReasoningField(m, model);
    if (reasoning === "" && partReasoning !== "") {
      reasoning = partReasoning;
      format = Format.NATIVE;
    }

    if (reasoning !== "") {
      return { reasoning, clean: splitThinkingTags(content).clean, format };
    }

    const result = splitThinkingTags(content);
    if (result.reasoning !== "") {
      return { reasoning: result.reasoning, clean: result.clean, format: Format.THINKING };
    }
    return { reasoning: "", clean: content, format: Format.NONE };
  }

  private extractReasoningField(
    m: Record<string, unknown>,
    model: string
  ): [string, Format] {
    if (model) {
      const fieldName = this.modelFields.get(model.toLowerCase());
      if (fieldName) {
        const value = m[fieldName];
        if (value !== null && value !== undefined) {
          if (typeof value !== "string") {
            throw new MalformedReasoningFieldError(
              `field "${fieldName}" is ${typeof value}, not a string`
            );
          }
          if (value !== "") {
            return [value, Format.MODEL_SPECIFIC];
          }
        }
      }
    }
    for (const fieldName of KNOWN_REASONING_FIELDS) {
      const value = m[fieldName];
      if (value === null || value === undefined) {
        continue;
      }
      if (typeof value !== "string") {
        throw new MalformedReasoningFieldError(
          `field "${fieldName}" is ${typeof value}, not a string`
        );
      }
      if (value !== "") {
        return [value, Format.NATIVE];
      }
    }
    return ["", Format.NONE];
  }

  private analyzeText(raw: string): Analysis {
    const result = splitThinkingTags(raw);
    if (result.reasoning !== "") {
      return { reasoning: result.reasoning, clean: result.clean, format: Format.THINKING };
    }
    return { reasoning: "", clean: raw, format: Format.NONE };
  }

  private hasRecognizedKey(m: Record<string, unknown>): boolean {
    return Object.keys(m).some((k) => RECOGNIZED_KEYS.has(k));
  }

  private parseNodes(reasoning: string, now: Date): Node[] {
    if (Buffer.byteLength(reasoning, "utf8") > this.opts.max_reasoning_bytes) {
      throw new ReasoningTooLargeError();
    }

    const paragraphs = splitParagraphs(reasoning);
    if (paragraphs.length > this.opts.max_nodes) {
      throw new TooManyNodesError();
    }

    const nodes: Node[] = [];
    let parentId = "";
    let depth = 0;

    for (const para of paragraphs) {
      depth += 1;
      if (depth > this.opts.max_depth) {
        throw new TooDeepError();
      }

      let kind = NodeKind.OBSERVATION;
      let summary = para.replace(LIST_ORNAMENT, "");
      const kindMatch = KIND_PREFIX.exec(summary);
      if (kindMatch) {
        kind = KIND_BY_LABEL[kindMatch[1].toLowerCase()] ?? NodeKind.OBSERVATION;
        summary = summary.slice(kindMatch[0].length).trim();
      }

      let status = NodeStatus.COMPLETE;
      const statusMatch = STATUS_MARKER.exec(summary);
      if (statusMatch) {
        status = STATUS_BY_LABEL[statusMatch[1].toLowerCase()] ?? NodeStatus.COMPLETE;
        summary = summary.slice(0, statusMatch.index).trim();
      } else if (kind === NodeKind.BLOCKER) {
        status = NodeStatus.BLOCKED;
      }

      summary = truncateCodePoints(summary, this.opts.max_summary_chars);
      if (summary === "") {
        depth -= 1;
        continue;
      }

      const id = nodeId(kind, parentId, summary);
      nodes.push({
        id,
        parent_id: parentId,
        kind,
        status,
        summary,
        evidence_refs: extractEvidence(summary),
        tool_call_refs: extractToolRefs(kind, summary),
        created_at: now,
        updated_at: now,
      });
      parentId = id;
    }
    return nodes;
  }
}

function splitThinkingTags(text: string): { clean: string; reasoning: string } {
  if (!THINKING_OPEN.test(text) && !THINKING_CLOSE.test(text)) {
    return { clean: text, reasoning: "" };
  }

  const parts: string[] = [];
  const reasons: string[] = [];
  let rest = text;
  for (;;) {
    const open = THINKING_OPEN.exec(rest);
    if (open === null) {
      if (THINKING_CLOSE.test(rest)) {
        throw new UnbalancedThinkingError();
      }
      parts.push(rest);
      break;
    }
    parts.push(rest.slice(0, open.index));
    const inner = rest.slice(open.index + open[0].length);
    const close = THINKING_CLOSE.exec(inner);
    if (close === null) {
      throw new UnbalancedThinkingError();
    }
    reasons.push(inner.slice(0, close.index));
    rest = inner.slice(close.index + close[0].length);
  }
  return { clean: parts.join(""), reasoning: reasons.join("\n").trim() };
}

function splitParagraphs(text: string): string[] {
  return text
    .split(PARAGRAPH_SEP)
    .map((p) => p.trim())
    .filter((p) => p !== "");
}

function extractEvidence(summary: string): string[] {
  const out: string[] = [];
  const seen = new Set<string>();
  for (const pattern of EVIDENCE_PATTERNS) {
    pattern.lastIndex = 0;
    let match: RegExpExecArray | null;
    while ((match = pattern.exec(summary)) !== null) {
      const ref = (match[1] ?? match[0]).replace(/^\[|\]$/g, "");
      if (ref !== "" && !seen.has(ref)) {
        seen.add(ref);
        out.push(ref);
      }
    }
  }
  return out;
}

function extractToolRefs(kind: NodeKind, summary: string): string[] {
  if (kind !== NodeKind.TOOL_CALL && kind !== NodeKind.TOOL_RESULT) {
    return [];
  }
  const leading = LEADING_IDENTIFIER.exec(summary);
  if (leading) {
    return [leading[1]];
  }
  const ref = TOOL_REF.exec(summary);
  if (ref) {
    return [ref[1]];
  }
  if (BARE_IDENTIFIER.test(summary)) {
    return [summary];
  }
  return [];
}

function truncateCodePoints(s: string, n: number): string {
  if (n <= 0) {
    return "";
  }
  const points = Array.from(s);
  if (points.length <= n) {
    return s;
  }
  return points.slice(0, n).join("");
}

function nodeId(kind: NodeKind, parentId: string, summary: string): string {
  return createHash("sha256")
    .update(`${kind}\0${parentId}\0${summary}`)
    .digest("hex")
    .slice(0, 16);
}
