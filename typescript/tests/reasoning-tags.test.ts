// Shared reasoning-tag constants (src/reasoning/tags.ts).
//
// The expected tag names are spelled without their delimiters on purpose:
// the tags themselves are never typed literally (see src/reasoning/tags.ts).
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import {
  BRACKET_THINK_CLOSE,
  BRACKET_THINK_OPEN,
  MMTHINK_CLOSE,
  MMTHINK_OPEN,
  REASONING_TAG_PAIRS,
  ReasoningAdapter,
  THINK_CLOSE,
  THINK_OPEN,
  THINKING_CLOSE,
  THINKING_OPEN,
} from "../src/reasoning/index.js";
import { ResponseValidator } from "../src/middleware/guardrails/response_validator.js";

describe("reasoning tag constants", () => {
  it("covers think, mm:think, thinking and bracket THINK", () => {
    const want = [
      ["<", "think", ">"],
      ["<", "mm:think", ">"],
      ["<", "thinking", ">"],
      ["[", "THINK", "]"],
    ];
    expect(REASONING_TAG_PAIRS).toHaveLength(4);
    REASONING_TAG_PAIRS.forEach((pair, i) => {
      const [lead, name, trail] = want[i];
      expect(pair.open).toBe(lead + name + trail);
      expect(pair.close).toBe(lead + "/" + name + trail);
    });
  });

  it("names every pair with a constant", () => {
    expect(REASONING_TAG_PAIRS).toEqual([
      { open: THINK_OPEN, close: THINK_CLOSE },
      { open: MMTHINK_OPEN, close: MMTHINK_CLOSE },
      { open: THINKING_OPEN, close: THINKING_CLOSE },
      { open: BRACKET_THINK_OPEN, close: BRACKET_THINK_CLOSE },
    ]);
  });

  it("is frozen", () => {
    expect(Object.isFrozen(REASONING_TAG_PAIRS)).toBe(true);
  });

  for (const pair of REASONING_TAG_PAIRS) {
    for (const upper of [false, true]) {
      it(`adapter strips ${pair.open}${upper ? " (upper case)" : ""}`, () => {
        const open = upper ? pair.open.toUpperCase() : pair.open;
        const close = upper ? pair.close.toUpperCase() : pair.close;
        expect(new ReasoningAdapter().strip(`${open}\nplan the call\n${close}answer`)).toBe("answer");
      });
    }
  }

  it("adapter fails closed on an unclosed think tag", () => {
    expect(() => new ReasoningAdapter().strip(THINK_OPEN + "never closed")).toThrow();
  });

  it("adapter fails closed on a stray mm:think close tag by default", () => {
    // MiniMax M2 emits the close tag without the open tag; without the
    // opt-in the adapter stays fail-closed.
    expect(() => new ReasoningAdapter().strip("reasoning" + MMTHINK_CLOSE + "answer")).toThrow();
  });

  it("implicit_open treats a leading think close tag as reasoning", () => {
    expect(new ReasoningAdapter({ implicitOpen: true }).strip("reasoning" + THINK_CLOSE + "answer")).toBe("answer");
  });

  it("implicit_open counts the mm:think close tag", () => {
    const adapter = new ReasoningAdapter({ implicitOpen: true });
    const { clean, tree } = adapter.parse("reasoning" + MMTHINK_CLOSE + "answer");
    expect(clean).toBe("answer");
    expect(tree.nodes.map((n) => n.summary)).toEqual(["reasoning"]);
  });

  it("implicit_open still fails closed on other close tags", () => {
    // Close-only mode counts only the plain think and mm:think closes.
    expect(() => new ReasoningAdapter({ implicitOpen: true }).strip("reasoning" + THINKING_CLOSE + "answer")).toThrow();
  });
});

const fixture = JSON.parse(
  readFileSync(resolve(process.cwd(), "../fixtures/reasoning/validator-rescue.v1.json"), "utf8"),
) as {
  tool_names: string[];
  cases: { name: string; response: string; expected_tool_calls: { tool: string; args: Record<string, unknown> }[] }[];
};

describe("response validator shared reasoning fixture", () => {
  test.each(fixture.cases.map((c) => [c.name, c] as const))("%s", (_name, c) => {
    const rv = new ResponseValidator(fixture.tool_names, true);
    const result = rv.validateTextResponse(c.response);
    if (c.expected_tool_calls.length === 0) {
      expect(result.needsRetry).toBe(true);
      expect(result.toolCalls).toEqual([]);
    } else {
      expect(result.needsRetry).toBe(false);
      expect(result.toolCalls).toEqual(c.expected_tool_calls);
    }
  });
});
