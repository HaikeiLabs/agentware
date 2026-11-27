// GPT-OSS Harmony prompt renderer and completion parser.
//
// Replays the Harmony-only sections of fixtures/toolformat/harmony-cases.json:
// render_prompt (whole conversations for raw /v1/completions) and
// parse_completion. The four standard sections replay in
// toolformat-reasoning-fixtures.test.ts.
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import {
  HARMONY_STOP_TOKENS,
  HarmonyFormatter,
  OpenAIFOrmatter,
  getFormatter,
  parseHarmonyCompletion,
  renderHarmonyMessage,
  renderHarmonyPrompt,
} from "../src/toolformat/index.js";
import type { HarmonyMessage, HarmonySystem } from "../src/toolformat/index.js";

type Case = Record<string, any>;
const cases = (section: string): Array<[string, Case]> =>
  (fixture[section] as Case[]).map((c) => [c.name as string, c]);

const fixture = JSON.parse(
  readFileSync(resolve(process.cwd(), "../fixtures/toolformat/harmony-cases.json"), "utf8"),
) as Record<string, any>;

function message(spec: Record<string, string>): HarmonyMessage {
  return {
    role: spec.role as HarmonyMessage["role"],
    content: spec.content,
    channel: spec.channel ?? "",
    recipient: spec.recipient ?? "",
    contentType: spec.content_type ?? "",
    name: spec.name ?? "",
  };
}

// A missing "system" key is the default system message and null leaves it out.
function system(spec: Record<string, any> | null | undefined): HarmonySystem | null | undefined {
  if (spec === null || spec === undefined) return spec;
  return {
    modelIdentity: spec.model_identity,
    knowledgeCutoff: spec.knowledge_cutoff,
    currentDate: spec.current_date,
    reasoningEffort: spec.reasoning_effort,
    validChannels: spec.valid_channels,
  };
}

describe("renderHarmonyPrompt fixtures", () => {
  test.each(cases("render_prompt"))("%s", (_name, c) => {
    const actual = renderHarmonyPrompt(c.messages.map(message), {
      system: system(c.system),
      instructions: c.instructions,
      tools: c.tools ?? [],
      autoDropAnalysis: c.auto_drop_analysis,
      nextRole: c.next_role,
    });
    expect(actual).toBe(c.expected);
  });
});

describe("parseHarmonyCompletion fixtures", () => {
  test.each(cases("parse_completion"))("%s", (_name, c) => {
    const parsed = parseHarmonyCompletion(c.input);
    expect(parsed.reasoning).toBe(c.expected.reasoning);
    expect(parsed.content).toBe(c.expected.content);
    expect(parsed.toolCalls.map(({ name, args }) => ({ name, args }))).toEqual(c.expected.tool_calls);
    expect(parsed.messages).toEqual(c.expected.messages.map(message));
  });
});

describe("Harmony history", () => {
  test("parsed messages render back in the reference form", () => {
    const parsed = parseHarmonyCompletion(
      '<|channel|>analysis<|message|>Need weather for Tokyo.<|end|><|start|>assistant<|channel|>commentary to=functions.get_weather <|constrain|>json<|message|>{"city":"Tokyo"}<|call|>',
    );
    expect(parsed.messages.map(renderHarmonyMessage)).toEqual([
      "<|start|>assistant<|channel|>analysis<|message|>Need weather for Tokyo.<|end|>",
      '<|start|>assistant to=functions.get_weather<|channel|>commentary <|constrain|>json<|message|>{"city":"Tokyo"}<|call|>',
    ]);
  });

  test("a return token becomes an end token in history", () => {
    const [final] = parseHarmonyCompletion("<|channel|>final<|message|>done<|return|>").messages;
    expect(renderHarmonyMessage(final)).toBe("<|start|>assistant<|channel|>final<|message|>done<|end|>");
  });

  test("stop tokens", () => {
    expect(HARMONY_STOP_TOKENS).toEqual(["<|return|>", "<|call|>"]);
  });

  test("invalid messages and efforts are rejected", () => {
    expect(() => renderHarmonyMessage({ role: "robot" as HarmonyMessage["role"], content: "x" })).toThrow();
    expect(() => renderHarmonyMessage({ role: "tool", content: "x" })).toThrow();
    expect(() =>
      renderHarmonyPrompt([], { system: { reasoningEffort: "max" as HarmonySystem["reasoningEffort"] } }),
    ).toThrow();
  });
});

describe("Harmony is opt-in", () => {
  // Chat-completions users keep the OpenAI formatter; raw-completions callers
  // construct HarmonyFormatter themselves.
  test.each(["openai/gpt-oss-20b", "gpt-oss-120b", "ggml-org/gpt-oss-20b-GGUF"])("%s", (model) => {
    expect(getFormatter(model)).toBeInstanceOf(OpenAIFOrmatter);
    expect(new HarmonyFormatter().modelFamily()).toBe("gpt-oss");
  });

  test("no tools renders nothing", () => {
    expect(new HarmonyFormatter().formatToolDefinitions([])).toBe("");
  });
});
