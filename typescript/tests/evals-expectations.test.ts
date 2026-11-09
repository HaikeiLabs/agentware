import { readFileSync } from "node:fs";
import { ChatModel, EvalCase, EvalRunner } from "../src/evals/runner.js";
import { ChatMessage, ModelResult, ToolDefinition } from "../src/evals/models.js";

interface ScriptTurn {
  content?: string;
  tool_calls?: Array<{ name: string; arguments: Record<string, unknown> }>;
}

interface FixtureEntry {
  case: Omit<EvalCase, "tools">;
  script: ScriptTurn[][];
  expected: {
    success: boolean;
    passes: number;
    repeats: number;
    turns: number;
    toolCalls: string[];
    error?: string;
    errorContains?: string;
  };
}

const fixture = JSON.parse(
  readFileSync("../fixtures/evals/expectations/cases.json", "utf8"),
) as { tools: ToolDefinition[]; cases: FixtureEntry[] };

class ScriptedModel implements ChatModel {
  private next = 0;
  constructor(private readonly turns: ScriptTurn[]) {}

  async complete(_messages: ChatMessage[], _tools?: ToolDefinition[]): Promise<ModelResult> {
    if (this.next >= this.turns.length) {
      throw new Error("script exhausted");
    }
    const turn = this.turns[this.next++];
    return {
      content: turn.content ?? "",
      tool_calls: (turn.tool_calls ?? []).map((tc, i) => ({
        id: `call-${this.next}-${i}`,
        name: tc.name,
        arguments: JSON.stringify(tc.arguments),
      })),
      finish_reason: "",
      usage: { prompt_tokens: 0, completion_tokens: 0, total_tokens: 0 },
    };
  }

  get consumed(): number {
    return this.next;
  }
}

describe("eval runner expectations (shared fixture)", () => {
  it("has cases", () => {
    expect(fixture.cases.length).toBeGreaterThan(0);
  });

  for (const entry of fixture.cases) {
    it(entry.case.name, async () => {
      const turns = entry.script.flat();
      const model = new ScriptedModel(turns);
      const runner = new EvalRunner("http://unused", 10, { clientFactory: () => model });
      const executed: string[] = [];
      const result = await runner.runCase(
        { ...entry.case, tools: fixture.tools } as EvalCase,
        "scripted",
        async (name) => {
          executed.push(name);
          return "ok";
        },
      );

      expect(result.success).toBe(entry.expected.success);
      expect(result.passes).toBe(entry.expected.passes);
      expect(result.repeats).toBe(entry.expected.repeats);
      expect(result.turns).toBe(entry.expected.turns);
      expect(result.tool_calls.map((tc) => tc.name)).toEqual(entry.expected.toolCalls);
      if (entry.expected.error !== undefined) {
        expect(result.error).toBe(entry.expected.error);
      }
      if (entry.expected.errorContains !== undefined) {
        expect(result.error).toContain(entry.expected.errorContains);
      }
      expect(model.consumed).toBe(turns.length);
    });
  }

  it("keeps the saved report shape for a single-repeat case", async () => {
    const runner = new EvalRunner("http://unused", 10, {
      clientFactory: () => new ScriptedModel([{ content: "hi" }]),
    });
    const result = await runner.runCase(
      {
        name: "legacy",
        description: "",
        systemPrompt: "",
        userMessage: "hi",
        tools: fixture.tools,
        expectedTool: "get_weather",
        maxTurns: 1,
      },
      "scripted",
      async () => "ok",
    );
    const row = EvalRunner.reportRow(result);
    expect(Object.keys(row)).toEqual([
      "case",
      "model",
      "success",
      "turns",
      "tool_calls",
      "error",
      "duration_ms",
    ]);
  });

  it("adds passes and repeats to the saved report when a case repeats", async () => {
    const runner = new EvalRunner("http://unused", 10, {
      clientFactory: () => new ScriptedModel([{ content: "a" }, { content: "b" }]),
    });
    const result = await runner.runCase(
      {
        name: "repeated",
        description: "",
        systemPrompt: "",
        userMessage: "hi",
        tools: fixture.tools,
        expectedTool: "get_weather",
        expect: "no_call",
        repeats: 2,
        maxTurns: 1,
      },
      "scripted",
      async () => "ok",
    );
    const row = EvalRunner.reportRow(result);
    expect(row.passes).toBe(2);
    expect(row.repeats).toBe(2);
  });
});
