// Shared reasoning-tag constants (src/reasoning/tags.ts).
//
// The expected tag names are spelled without their delimiters on purpose:
// the tags themselves are never typed literally (see src/reasoning/tags.ts).
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import {
  BRACKET_THINK_CLOSE,
  BRACKET_THINK_OPEN,
  REASONING_TAG_PAIRS,
  ReasoningAdapter,
  THINK_CLOSE,
  THINK_OPEN,
  THINKING_CLOSE,
  THINKING_OPEN,
} from "../src/reasoning/index.js";
import { ResponseValidator } from "../src/middleware/guardrails/response_validator.js";

describe("reasoning tag constants", () => {
  it("covers think, thinking and bracket THINK", () => {
    const want = [
      ["<", "think", ">"],
      ["<", "thinking", ">"],
      ["[", "THINK", "]"],
    ];
    expect(REASONING_TAG_PAIRS).toHaveLength(3);
    REASONING_TAG_PAIRS.forEach((pair, i) => {
      const [lead, name, trail] = want[i];
      expect(pair.open).toBe(lead + name + trail);
      expect(pair.close).toBe(lead + "/" + name + trail);
    });
  });

  it("names every pair with a constant", () => {
    expect(REASONING_TAG_PAIRS).toEqual([
      { open: THINK_OPEN, close: THINK_CLOSE },
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
