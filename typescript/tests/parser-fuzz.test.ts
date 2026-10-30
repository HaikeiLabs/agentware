import { readFileSync, readdirSync } from "node:fs";
import { resolve } from "node:path";
import fc from "fast-check";
import { getFormatter } from "../src/toolformat/index.js";
import {
  ManifestV3Error,
  parseManifestV3,
  serializeManifestV3,
  type ToolSpec,
} from "../src/toolspec/index.js";
import { ReasoningAdapter } from "../src/reasoning/index.js";

const numRuns = Number.parseInt(process.env.FC_NUM_RUNS ?? "200", 10);
const familyModels: Record<string, string> = {
  openai: "gpt-4o",
  glm: "glm-4",
  deepseek: "deepseek-chat",
  anthropic: "claude-3",
  qwen: "qwen-2",
};
const toolformatRoot = resolve(process.cwd(), "../fixtures/toolformat");
const reasoningRoot = resolve(process.cwd(), "../fixtures/reasoning");
const manifestRoot = resolve(process.cwd(), "../fixtures/toolspec/manifest-v3");

function fixtureToolCallSeeds(): Array<[string, string]> {
  const seeds: Array<[string, string]> = [];
  for (const file of readdirSync(toolformatRoot).filter((name) =>
    name.endsWith("-cases.json"),
  )) {
    const fixture = JSON.parse(
      readFileSync(resolve(toolformatRoot, file), "utf8"),
    ) as {
      family: string;
      parse_calls: Array<{ input: string }>;
    };
    const model = familyModels[fixture.family];
    if (model)
      for (const example of fixture.parse_calls ?? [])
        seeds.push([model, example.input]);
  }
  return seeds;
}

const toolCallSeeds: Array<[string, string]> = [
  ...fixtureToolCallSeeds(),
  ["llama-3", '<|python_tag|>get_weather({"location": "Tokyo"})'],
  [
    "mistral-7b",
    '[TOOL_CALLS][tool_call>get_weather: {"location": "Tokyo"}][/TOOL_CALLS]',
  ],
  [
    "nemotron-70b",
    '[{"id":"1","name":"get_weather","arguments":{"location":"Tokyo"}}]',
  ],
  [
    "gpt-4o",
    '[{"id":"1","name":"get_weather","arguments":"{\\"location\\":\\"Tokyo\\"}"}]',
  ],
  ["gpt-4o", '[{"name":""}]'],
  ["gpt-4o", '[{"name":"x","arguments":null}]'],
  ["qwen-2", ""],
  ["minimax-7b", ""],
  ["some-model", '[{"name":"x","arguments":{"a":1}}]'],
  ["some-model", "not json at all"],
];

function manifestSeeds(): Uint8Array[] {
  const seeds: Uint8Array[] = [];
  for (const directory of ["valid", "invalid"]) {
    for (const file of readdirSync(resolve(manifestRoot, directory)).filter(
      (name) => name.endsWith(".json"),
    )) {
      seeds.push(readFileSync(resolve(manifestRoot, directory, file)));
    }
  }
  return seeds;
}

function reasoningSeeds(): string[] {
  const seeds: string[] = [];
  for (const file of readdirSync(reasoningRoot).filter((name) =>
    name.endsWith("-cases.json"),
  )) {
    const fixture = JSON.parse(
      readFileSync(resolve(reasoningRoot, file), "utf8"),
    ) as {
      examples: Array<{ raw_response: unknown }>;
    };
    for (const example of fixture.examples ?? [])
      seeds.push(JSON.stringify(example.raw_response));
  }
  return [
    ...seeds,
    "",
    "hello world",
    "<thinking>why</thinking>answer",
    "<thinking>unbalanced",
    "closed</thinking>first",
    "[THINK]why[/THINK]answer",
    "[THINK]unbalanced",
    '{"content": "hi", "reasoning_content": "why"}',
    '{"content": "hi", "reasoning_content": 5}',
    '{"content": {"a": 1}}',
    '{"content": null, "reasoning": "why"}',
    '{"content": [{"type": "text", "text": "hi"}, {"type": "thinking", "thinking": "why"}]}',
    '{"content": [{"type": "text"}]}',
    '{"message": {"content": "hi", "reasoning_content": "why"}}',
    '{"choices": [{"message": {"content": "hi"}}]}',
    '{"message": 42}',
    '{"unrelated": true}',
    "{invalid",
  ];
}

const toolCallArb = fc.oneof(
  { weight: 1, arbitrary: fc.constantFrom(...toolCallSeeds) },
  { weight: 4, arbitrary: fc.tuple(fc.string(), fc.string()) },
);
const manifestArb = fc.oneof(
  { weight: 1, arbitrary: fc.constantFrom(...manifestSeeds()) },
  { weight: 4, arbitrary: fc.uint8Array({ maxLength: 4096 }) },
);
const reasoningArb = fc.oneof(
  { weight: 1, arbitrary: fc.constantFrom(...reasoningSeeds()) },
  { weight: 4, arbitrary: fc.string({ maxLength: 4096 }) },
);

describe("parser property fuzzing", () => {
  test("tool-call formatters only return named calls with JSON object arguments", () => {
    fc.assert(
      fc.property(toolCallArb, ([model, response]) => {
        const calls = getFormatter(model).parseToolCalls(response);
        for (const call of calls) {
          expect(typeof call.name).toBe("string");
          expect(call.name.length).toBeGreaterThan(0);
          expect(call.args).not.toBeNull();
          expect(typeof call.args).toBe("object");
          expect(Array.isArray(call.args)).toBe(false);
          expect(() => JSON.stringify(call.args)).not.toThrow();
        }
      }),
      { numRuns },
    );
  });

  test("manifest parser errors are typed and accepted manifests round-trip", () => {
    fc.assert(
      fc.property(manifestArb, (data) => {
        let specs: ToolSpec[];
        try {
          specs = parseManifestV3(data);
        } catch (error) {
          expect(error).toBeInstanceOf(ManifestV3Error);
          return;
        }
        const serialized = serializeManifestV3(specs);
        const reparsed = parseManifestV3(JSON.stringify(serialized));
        const stripParameters = (items: ToolSpec[]) =>
          items.map(({ parameters: _parameters, ...item }) => item);
        expect(stripParameters(reparsed)).toEqual(stripParameters(specs));
      }),
      { numRuns },
    );
  });

  test("reasoning adapter entry points agree and fail closed", () => {
    fc.assert(
      fc.property(reasoningArb, (raw) => {
        const adapter = new ReasoningAdapter();
        let clean: string | undefined;
        let tree: ReturnType<ReasoningAdapter["extract"]> | undefined;
        let strip: string | undefined;
        let extracted: ReturnType<ReasoningAdapter["extract"]> | undefined;
        let parseError: unknown;
        let stripError: unknown;
        let extractError: unknown;
        try {
          ({ clean, tree } = adapter.parse(raw, "fuzz-model", "fuzz-backend"));
        } catch (error) {
          parseError = error;
        }
        try {
          strip = adapter.strip(raw);
        } catch (error) {
          stripError = error;
        }
        try {
          extracted = adapter.extract(raw, "fuzz-model", "fuzz-backend");
        } catch (error) {
          extractError = error;
        }
        try {
          adapter.strip(new TextEncoder().encode(raw));
        } catch {
          /* no panic is the property */
        }
        expect(parseError === undefined).toBe(stripError === undefined);
        expect(parseError === undefined).toBe(extractError === undefined);
        if (parseError !== undefined) {
          expect(clean).toBeUndefined();
          expect(tree).toBeUndefined();
          expect(strip).toBeUndefined();
          expect(extracted).toBeUndefined();
          return;
        }
        expect(strip).toBe(clean);
        expect(tree).toBeDefined();
        expect(
          tree!.nodes.length === 0
            ? tree!.root_id
            : tree!.root_id === tree!.nodes[0].id,
        ).toBe(tree!.nodes.length === 0 ? "" : true);
        for (let i = 0; i < tree!.nodes.length; i++) {
          expect(tree!.nodes[i].parent_id).toBe(
            i === 0 ? "" : tree!.nodes[i - 1].id,
          );
        }
      }),
      { numRuns },
    );
  });
});
