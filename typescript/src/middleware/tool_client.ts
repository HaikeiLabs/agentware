import type { AuditFilter, Auditor, AuditRecord } from "./audit.js";
import { InMemoryAuditor } from "./audit.js";
import type { CallerContext, Decision } from "./types.js";
import { Action } from "./types.js";

export type ToolExecution = [unknown, boolean, string];
export type AsyncToolExecutor = (
  toolName: string,
  args: Record<string, unknown>,
) => ToolExecution | Promise<ToolExecution>;

/**
 * A policy evaluator that may need to await local evaluation work.
 *
 * Async support lets local runtimes such as KeiProxyEvaluator await the
 * locally spawned `kei-proxy authorize` process. It does not define or imply
 * a policy request to the Kei console; policy decisions remain local.
 */
export interface LocalPolicyEvaluator {
  evaluate(
    toolName: string,
    args: Record<string, unknown>,
    caller: CallerContext,
  ): Decision | Promise<Decision>;
}

/** Gate, execute, and audit a tool call, including async policy decisions. */
export class AuditedToolClient {
  private evaluator: LocalPolicyEvaluator | null = null;
  private auditor: Auditor;

  constructor(
    private readonly source: string,
    private readonly executor: AsyncToolExecutor,
    auditor?: Auditor,
  ) {
    this.auditor = auditor ?? new InMemoryAuditor();
  }

  withPolicy(evaluator: LocalPolicyEvaluator): this {
    this.evaluator = evaluator;
    return this;
  }

  withAuditor(auditor: Auditor): this {
    this.auditor = auditor;
    return this;
  }

  records(filter: AuditFilter = {}): AuditRecord[] {
    return this.auditor.query(filter);
  }

  async execute(
    toolName: string,
    args: Record<string, unknown>,
    caller: CallerContext,
  ): Promise<ToolExecution> {
    let decision: Decision;
    try {
      decision = this.evaluator
        ? await this.evaluator.evaluate(toolName, args, caller)
        : {
            action: Action.DENY,
            rule: "default",
            reason: "no policy configured; default deny",
            timestamp: new Date(),
          };
    } catch {
        decision = {
          action: Action.DENY,
        rule: "default",
        reason: "policy evaluation failed; default deny",
        timestamp: new Date(),
      };
    }

    this.auditor.record({
      session_id: caller.session_id || "",
      tool_name: toolName,
      args,
      decision,
      timestamp: new Date(),
      invoking_subject: caller.invoking_subject || "",
      parent_span: caller.parent_span || "",
      delegation_depth: caller.delegation_depth ?? 0,
      framework: this.source,
    });

    if (decision.action === Action.DENY) {
      return [null, false, `denied by policy: ${decision.reason}`];
    }

    const executionArgs = decision.action === Action.FILTER && decision.redacted_args
      ? { ...args, ...decision.redacted_args }
      : args;
    return await this.executor(toolName, executionArgs);
  }
}
