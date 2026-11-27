// MiniMax checks the shared runner (toolformat-reasoning-fixtures.test.ts)
// does not cover: it replays minimax-cases.json through the M2 formatter, so
// this file checks the M3 variant against the fixture's m3_format_definitions
// section and the selector routing for MiniMax ids.
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { MiniMaxFormatter, getFormatter } from "../src/toolformat/index.js";
import { Result } from "../src/tools/index.js";

const root = resolve(process.cwd(), "../fixtures/toolformat");
const cases = JSON.parse(readFileSync(resolve(root, "minimax-cases.json"), "utf8")) as Record<string, any>;
const tools = JSON.parse(readFileSync(resolve(root, "tools.json"), "utf8")).tools as any[];

describe("MiniMaxFormatter", () => {
  test.each(cases.m3_format_definitions as Array<{ name: string; expected: string }>)("M3 definitions: $name", (c) => {
    expect(new MiniMaxFormatter({ version: "m3" }).formatToolDefinitions(tools)).toBe(c.expected);
  });

  test("the M3 variant parses, validates and formats like M2", () => {
    const m2 = new MiniMaxFormatter();
    const m3 = new MiniMaxFormatter({ version: "m3" });
    for (const c of cases.parse_calls) expect(m3.parseToolCalls(c.input)).toEqual(m2.parseToolCalls(c.input));
    for (const c of cases.validate_format) expect(m3.validateFormat(c.input) === null).toBe(c.valid);
    expect(m3.formatToolResult("get_weather", new Result(true, "Sunny"))).toBe("<response>Sunny</response>");
  });

  test("an empty tool list renders nothing", () => {
    expect(new MiniMaxFormatter().formatToolDefinitions([])).toBe("");
    expect(new MiniMaxFormatter({ version: "m3" }).formatToolDefinitions([])).toBe("");
  });

  test("an unknown version is rejected", () => {
    expect(() => new MiniMaxFormatter({ version: "m1" as never })).toThrow();
  });

  test("a parameter named __proto__ is an own argument", () => {
    const [call] = new MiniMaxFormatter().parseToolCalls(
      '<minimax:tool_call>\n<invoke name="t">\n<parameter name="__proto__">x</parameter>\n</invoke>\n</minimax:tool_call>',
    );
    expect(Object.prototype.hasOwnProperty.call(call.args, "__proto__")).toBe(true);
    expect(Object.getPrototypeOf(call.args)).toBe(Object.prototype);
  });

  test.each([
    ["MiniMax-M2", "m2"],
    ["MiniMaxAI/MiniMax-M2.5", "m2"],
    ["minimax-m2.1", "m2"],
    ["MiniMax-M3", "m3"],
    ["MiniMaxAI/MiniMax-M3", "m3"],
    ["minimax-m3-preview", "m3"],
  ])("getFormatter routes %s to %s", (model, version) => {
    const formatter = getFormatter(model);
    expect(formatter).toBeInstanceOf(MiniMaxFormatter);
    expect(formatter.modelFamily()).toBe("minimax");
    expect((formatter as MiniMaxFormatter).version).toBe(version);
  });

  test.each(["MiniMax-Text-01", "minimax-7b", "MiniMax-M1"])("getFormatter does not route %s", (model) => {
    expect(getFormatter(model)).not.toBeInstanceOf(MiniMaxFormatter);
  });
});
