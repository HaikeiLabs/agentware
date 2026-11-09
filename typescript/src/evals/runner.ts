import * as fs from "fs";
import * as path from "path";
import { ModelClient, ChatMessage, ModelResult, ToolDefinition } from "./models.js";

export { ToolDefinition };

/** Whether a case expects the expected tool to be called or not. */
export type EvalExpect = "call" | "no_call";

export interface EvalCase {
  name: string;
  description: string;
  systemPrompt: string;
  userMessage: string;
  tools: ToolDefinition[];
  expectedTool: string;
  /** "call" (default): a repeat passes when `expectedTool` is called.
   * "no_call": a repeat passes when `expectedTool` (with `expectedArgs`, if
   * given) is NOT called. */
  expect?: EvalExpect;
  /** Partial match on the expected tool call's arguments: every key here
   * must be present with a deep-equal value; extra arguments are allowed. */
  expectedArgs?: Record<string, unknown>;
  /** How many times to run the case (default 1). */
  repeats?: number;
  /** The case passes when passes/repeats >= minPassRate (0..1, default 1.0). */
  minPassRate?: number;
  maxTurns: number;
}

export interface EvalResult {
  case_name: string;
  model_name: string;
  success: boolean;
  /** Total turns across all repeats. */
  turns: number;
  /** Tool calls across all repeats, in order. */
  tool_calls: Array<{ turn: number; name: string; arguments: string }>;
  error: string;
  duration_ms: number;
  /** Repeats that passed. */
  passes: number;
  /** Repeats run. */
  repeats: number;
}

export interface EvalReport {
  timestamp: string;
  models: string[];
  results: EvalResult[];
}

/** The model seam: `ModelClient` satisfies it; tests inject a scripted one. */
export interface ChatModel {
  complete(
    messages: ChatMessage[],
    tools?: ToolDefinition[],
    temperature?: number,
    maxTokens?: number,
  ): Promise<ModelResult>;
}

export interface EvalRunnerOptions {
  /** Builds the model client for a model name (default: `ModelClient`). */
  clientFactory?: (model: string) => ChatModel;
}

type ToolExecutorFn = (toolName: string, args: Record<string, unknown>) => Promise<string>;

interface RepeatOutcome {
  passed: boolean;
  turns: number;
  error: string;
}

/** Returns an error message when the case's expectation fields are invalid. */
export function validateEvalCase(case_: EvalCase): string {
  const expect = case_.expect ?? "call";
  if (expect !== "call" && expect !== "no_call") {
    return `invalid expect ${JSON.stringify(expect)}: must be "call" or "no_call"`;
  }
  const repeats = case_.repeats ?? 1;
  if (!Number.isInteger(repeats) || repeats < 1) {
    return `invalid repeats ${repeats}: must be an integer >= 1`;
  }
  const minPassRate = case_.minPassRate ?? 1.0;
  if (typeof minPassRate !== "number" || !(minPassRate >= 0 && minPassRate <= 1)) {
    return `invalid minPassRate ${minPassRate}: must be between 0 and 1`;
  }
  return "";
}

/** True when every key of `expected` is in `actual` with a deep-equal value. */
export function argsMatch(expected: Record<string, unknown>, actual: unknown): boolean {
  if (actual === null || typeof actual !== "object" || Array.isArray(actual)) {
    return false;
  }
  const obj = actual as Record<string, unknown>;
  return Object.keys(expected).every(
    (key) => Object.prototype.hasOwnProperty.call(obj, key) && deepEqual(expected[key], obj[key]),
  );
}

function deepEqual(a: unknown, b: unknown): boolean {
  if (a === b) return true;
  if (a === null || b === null || typeof a !== "object" || typeof b !== "object") return false;
  if (Array.isArray(a) !== Array.isArray(b)) return false;
  if (Array.isArray(a) && Array.isArray(b)) {
    return a.length === b.length && a.every((v, i) => deepEqual(v, b[i]));
  }
  const ao = a as Record<string, unknown>;
  const bo = b as Record<string, unknown>;
  const keys = Object.keys(ao);
  return (
    keys.length === Object.keys(bo).length &&
    keys.every((k) => Object.prototype.hasOwnProperty.call(bo, k) && deepEqual(ao[k], bo[k]))
  );
}

function parseArgs(raw: string): unknown {
  try {
    return raw ? JSON.parse(raw) : {};
  } catch {
    return undefined;
  }
}

export class EvalRunner {
  private baseURL: string;
  private maxTurns: number;
  private results: EvalResult[] = [];
  private clientFactory: (model: string) => ChatModel;

  constructor(baseURL: string, maxTurns: number = 10, options: EvalRunnerOptions = {}) {
    this.baseURL = baseURL;
    this.maxTurns = maxTurns;
    this.clientFactory = options.clientFactory ?? ((model) => new ModelClient(this.baseURL, model));
  }

  passRate(model: string): number {
    const modelResults = this.results.filter((r) => r.model_name === model);
    if (modelResults.length === 0) return 0.0;
    const passed = modelResults.filter((r) => r.success).length;
    return passed / modelResults.length;
  }

  /**
   * Runs a case `repeats` times. The case passes when
   * passes/repeats >= minPassRate.
   */
  async runCase(case_: EvalCase, model: string, toolExecutor: ToolExecutorFn): Promise<EvalResult> {
    const start = Date.now();
    const toolCallsMade: Array<{ turn: number; name: string; arguments: string }> = [];

    const invalid = validateEvalCase(case_);
    if (invalid) {
      return {
        case_name: case_.name,
        model_name: model,
        success: false,
        turns: 0,
        tool_calls: toolCallsMade,
        error: invalid,
        duration_ms: Date.now() - start,
        passes: 0,
        repeats: Number.isInteger(case_.repeats) && (case_.repeats as number) >= 1 ? (case_.repeats as number) : 1,
      };
    }

    const repeats = case_.repeats ?? 1;
    const minPassRate = case_.minPassRate ?? 1.0;
    let passes = 0;
    let turns = 0;
    let firstError = "";

    for (let i = 0; i < repeats; i++) {
      const outcome = await this.runRepeat(case_, model, toolExecutor, toolCallsMade);
      turns += outcome.turns;
      if (outcome.passed) {
        passes++;
      } else if (!firstError) {
        firstError = outcome.error;
      }
    }

    const success = passes / repeats >= minPassRate;
    let error = "";
    if (!success) {
      error =
        repeats === 1
          ? firstError
          : `passed ${passes}/${repeats} repeats (minPassRate ${minPassRate}): ${firstError}`;
    }
    return {
      case_name: case_.name,
      model_name: model,
      success,
      turns,
      tool_calls: toolCallsMade,
      error,
      duration_ms: Date.now() - start,
      passes,
      repeats,
    };
  }

  private async runRepeat(
    case_: EvalCase,
    model: string,
    toolExecutor: ToolExecutorFn,
    toolCallsMade: Array<{ turn: number; name: string; arguments: string }>,
  ): Promise<RepeatOutcome> {
    const client = this.clientFactory(model);
    const noCall = case_.expect === "no_call";
    const messages: ChatMessage[] = [
      { role: "system", content: case_.systemPrompt },
      { role: "user", content: case_.userMessage },
    ];
    let turns = 0;

    try {
      while (turns < case_.maxTurns) {
        const result = await client.complete(messages, case_.tools, 0.0, 2048);
        turns++;

        if (result.tool_calls.length === 0) {
          break;
        }

        for (const tc of result.tool_calls) {
          toolCallsMade.push({ turn: turns, name: tc.name, arguments: tc.arguments });

          if (tc.name === case_.expectedTool) {
            const args = parseArgs(tc.arguments);
            const matches = case_.expectedArgs === undefined || argsMatch(case_.expectedArgs, args);
            if (noCall) {
              if (matches) {
                return {
                  passed: false,
                  turns,
                  error: `Tool '${tc.name}' was called but expect is no_call`,
                };
              }
            } else {
              // Reaching the expected tool decides the repeat either way.
              return matches
                ? { passed: true, turns, error: "" }
                : {
                    passed: false,
                    turns,
                    error: `Tool '${tc.name}' called with arguments that do not match expectedArgs`,
                  };
            }
          }

          const parsed = parseArgs(tc.arguments);
          const args =
            parsed !== null && typeof parsed === "object" && !Array.isArray(parsed)
              ? (parsed as Record<string, unknown>)
              : {};
          const toolResult = await toolExecutor(tc.name, args);

          messages.push({ role: "assistant", content: "" });
          messages.push({ role: "tool", content: toolResult });
        }
      }

      if (noCall) {
        return { passed: true, turns, error: "" };
      }
      return {
        passed: false,
        turns,
        error: `Expected tool '${case_.expectedTool}' not called in ${turns} turns`,
      };
    } catch (e) {
      return { passed: false, turns, error: e instanceof Error ? e.message : String(e) };
    }
  }

  /** One result as a saved-report row. `passes` and `repeats` are added only
   * when the case ran more than once, so single-run reports keep their shape. */
  static reportRow(r: EvalResult): Record<string, unknown> {
    const row: Record<string, unknown> = {
      case: r.case_name,
      model: r.model_name,
      success: r.success,
      turns: r.turns,
      tool_calls: r.tool_calls,
      error: r.error,
      duration_ms: r.duration_ms,
    };
    if (r.repeats > 1) {
      row.passes = r.passes;
      row.repeats = r.repeats;
    }
    return row;
  }

  async runEvals(
    cases: EvalCase[],
    models: string[],
    toolExecutor: (toolName: string, args: Record<string, unknown>) => Promise<string>
  ): Promise<EvalReport> {
    const report: EvalReport = {
      timestamp: new Date().toISOString(),
      models: models,
      results: [],
    };

    for (const model of models) {
      console.log(`\n=== Testing model: ${model} ===`);
      for (const case_ of cases) {
        process.stdout.write(`  Running: ${case_.name}...`);
        const result = await this.runCase(case_, model, toolExecutor);
        this.results.push(result);
        report.results.push(result);

        const status = result.success ? "PASS" : "FAIL";
        console.log(`${status} (${result.turns} turns, ${result.duration_ms}ms)`);

        if (!result.success) {
          console.log(`    Error: ${result.error}`);
        }
      }
    }

    return report;
  }

  saveReport(report: EvalReport, outputPath: string): void {
    const dir = path.dirname(outputPath);
    fs.mkdirSync(dir, { recursive: true });

    const output = {
      timestamp: report.timestamp,
      models: report.models,
      results: report.results.map((r) => EvalRunner.reportRow(r)),
    };

    fs.writeFileSync(outputPath, JSON.stringify(output, null, 2));
  }
}