import type { CallerContext, Decision } from "./types.js";

export interface PolicyEvaluator {
  evaluate(toolName: string, args: Record<string, unknown>, caller: CallerContext): Decision;
}