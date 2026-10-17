import { prerequisiteNudge, stepNudge, type Nudge } from "./nudge.js";
import { ResponseValidator, type ToolCall } from "./response_validator.js";

export interface GuardrailsPrerequisite {
  tool: string;
  requires: string;
  match_arg?: string;
}

export interface GuardrailsConfig {
  version: 1;
  tool_names: string[];
  required_steps?: string[];
  terminal_tool?: string;
  prerequisites?: GuardrailsPrerequisite[];
  max_prereq_violations?: number;
  step_nudge_tier?: number;
  nudges?: Partial<Record<"retry" | "step_blocked" | "prerequisite" | "fatal", string>>;
}

export interface GuardrailsState {
  completed_steps?: Array<{ tool: string; args?: Record<string, unknown> }>;
  prereq_violations?: number;
  tool_errors?: Array<{ tool: string; category: string }>;
}

export interface GuardrailsResult {
  outcome: "execute" | "retry" | "step_blocked" | "fatal" | "recorded";
  tool_calls?: ToolCall[];
  nudge?: Nudge;
  nudge_kind?: string;
  missing_steps?: string[];
  missing_prerequisites?: string[];
  prereq_violations?: number;
  tool_errors?: Array<{ tool: string; category: string }>;
}

/** Quality and progress guidance for agent responses. This facade never authorizes tool calls. */
export class Guardrails {
  private readonly validator: ResponseValidator;

  constructor(private readonly config: GuardrailsConfig) {
    this.validator = new ResponseValidator(config.tool_names, false);
  }

  evaluate(response: { tool_calls?: ToolCall[] }, state: GuardrailsState = {}): GuardrailsResult {
    const validation = this.validator.validateToolCalls(response.tool_calls ?? []);
    if (validation.needsRetry) {
      const nudge = this.customize(validation.nudge!, "retry");
      return { outcome: "retry", nudge, nudge_kind: nudge.kind };
    }
    const completed = state.completed_steps ?? [];
    for (const call of validation.toolCalls) {
      const prereq = this.config.prerequisites?.find((item) => item.tool === call.tool &&
        !completed.some((step) => step.tool === item.requires &&
          (item.match_arg === undefined || step.args?.[item.match_arg] === call.args[item.match_arg])));
      if (prereq) {
        const missingPrerequisites = [prereq.requires];
        const violations = (state.prereq_violations ?? 0) + 1;
        const nudge = this.customize(prerequisiteNudge(call.tool, missingPrerequisites), "prerequisite");
        return {
          outcome: violations > (this.config.max_prereq_violations ?? 2) ? "fatal" : "retry",
          missing_prerequisites: missingPrerequisites,
          nudge: this.customize(nudge, "fatal", violations > (this.config.max_prereq_violations ?? 2)),
          nudge_kind: nudge.kind,
          prereq_violations: violations,
        };
      }
      if (call.tool === this.config.terminal_tool) {
        const missing = (this.config.required_steps ?? []).filter((step) =>
          !completed.some((done) => done.tool === step));
        if (missing.length) {
          const nudge = this.customize(stepNudge(call.tool, missing, this.config.step_nudge_tier ?? 1), "step_blocked");
          return { outcome: "step_blocked", missing_steps: missing, nudge, nudge_kind: nudge.kind };
        }
      }
    }
    return { outcome: "execute", tool_calls: validation.toolCalls };
  }

  record(record: { tool: string; error: string; category: string }, state: GuardrailsState = {}): GuardrailsResult {
    const toolErrors = [...(state.tool_errors ?? []), { tool: record.tool, category: record.category }];
    return { outcome: "recorded", tool_errors: toolErrors };
  }

  private customize(nudge: Nudge, key: "retry" | "step_blocked" | "prerequisite" | "fatal", force = false): Nudge {
    const content = this.config.nudges?.[force ? "fatal" : key];
    return content === undefined ? nudge : { ...nudge, content };
  }
}
