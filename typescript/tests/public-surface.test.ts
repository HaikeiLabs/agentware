import {
  Action,
  CONTRACT_VERSION,
  ErrorCategory,
  ErrorTracker,
  Format,
  InMemoryAuditor,
  MiddlewareImpl,
  NodeKind,
  NodeStatus,
  NudgeKind,
  Operator,
  ReasoningAdapter,
  ReasoningError,
  ResponseValidator,
  SimplePolicyEvaluator,
  StepEnforcer,
  StepNotAllowedError,
  ToolRegistry,
  newRuntimeLink,
  prerequisiteNudge,
  retryNudge,
  stepNudge,
  unknownToolNudge,
  wrapTool,
} from "../src/index.js";
import type {
  CallerContext,
  Condition,
  ContextTree,
  Decision,
  Middleware,
  Node,
  Nudge,
  Policy,
  ReasoningOptions,
  Rule,
  ToolError,
  ToolExecutor,
  ValidatedToolCall,
  ValidationResult,
} from "../src/index.js";

describe("@haikeilabs/agentware public surface", () => {
  it("exports the middleware, guardrail, and reasoning APIs from the root barrel", () => {
    const values = [
      Action,
      CONTRACT_VERSION,
      ErrorCategory,
      ErrorTracker,
      Format,
      InMemoryAuditor,
      MiddlewareImpl,
      NodeKind,
      NodeStatus,
      NudgeKind,
      Operator,
      ReasoningAdapter,
      ReasoningError,
      ResponseValidator,
      SimplePolicyEvaluator,
      StepEnforcer,
      StepNotAllowedError,
      ToolRegistry,
      newRuntimeLink,
      prerequisiteNudge,
      retryNudge,
      stepNudge,
      unknownToolNudge,
      wrapTool,
    ];
    for (const value of values) {
      expect(value).toBeDefined();
    }
  });

  it("keeps the guardrail and reasoning types importable from the root barrel", () => {
    const caller: CallerContext = { trusted: false };
    const condition: Condition = {
      field: "caller.trusted",
      operator: Operator.EQ,
      value: "false",
    };
    const rule: Rule = { name: "deny-untrusted", tools: ["*"], action: Action.DENY };
    const policy: Policy = { default_deny: false, rules: [rule] };
    const decision: Decision = {
      action: Action.ALLOW,
      rule: "default",
      reason: "no policy configured",
      timestamp: new Date(),
    };
    const nudge: Nudge = retryNudge("oops", ["read_file"]);
    const toolError: ToolError = {
      timestamp: new Date(),
      tool: "read_file",
      args: {},
      category: ErrorCategory.UNKNOWN,
      message: "boom",
      sessionId: "session-1",
      retryCount: 0,
    };
    const validated: ValidatedToolCall = { tool: "read_file", args: {} };
    const result: ValidationResult = {
      toolCalls: [validated],
      nudge: null,
      needsRetry: false,
    };
    const options: ReasoningOptions = { max_nodes: 8 };
    const node: Node = {
      id: "n1",
      parent_id: "",
      kind: NodeKind.GOAL,
      status: NodeStatus.ACTIVE,
      summary: "goal",
      evidence_refs: [],
      tool_call_refs: [],
      created_at: new Date(),
      updated_at: new Date(),
    };
    const tree: ContextTree = {
      version: CONTRACT_VERSION,
      format: Format.NONE,
      model: "test",
      backend: "test",
      created_at: new Date(),
      root_id: "n1",
      nodes: [node],
    };

    expect(caller.trusted).toBe(false);
    expect(policy.rules).toHaveLength(1);
    expect(condition.operator).toBe(Operator.EQ);
    expect(decision.action).toBe(Action.ALLOW);
    expect(nudge.kind).toBe(NudgeKind.RETRY);
    expect(toolError.category).toBe(ErrorCategory.UNKNOWN);
    expect(validated.tool).toBe("read_file");
    expect(result.needsRetry).toBe(false);
    expect(options.max_nodes).toBe(8);
    expect(tree.nodes).toHaveLength(1);
  });

  it("lets an assistant consumer drive the guardrails through the barrel", () => {
    const executor: ToolExecutor = {
      execute: (toolName) => [{ tool: toolName }, true, ""],
    };
    const middleware: Middleware = new MiddlewareImpl(executor).withPolicy(
      new SimplePolicyEvaluator({ default_deny: false, rules: [] })
    );
    const [result, success] = middleware.execute("read_file", {}, { trusted: false });
    expect(success).toBe(true);
    expect(result).toEqual({ tool: "read_file" });

    const validator = new ResponseValidator(["read_file"]);
    const retry = validator.validateTextResponse("I will read the file");
    expect(retry.needsRetry).toBe(true);
    expect(retry.nudge?.kind).toBe(NudgeKind.RETRY);

    const tracker = new ErrorTracker();
    tracker.recordError("session-1", "read_file", {}, new Error("boom"), ErrorCategory.UNKNOWN);
    expect(tracker.getErrorCount("session-1", "read_file")).toBe(1);

    const enforcer = new StepEnforcer();
    enforcer.addStep("submit", ["plan"]);
    const [allowed, missing] = enforcer.canExecute("session-1", "submit");
    expect(allowed).toBe(false);
    expect(missing).toEqual(["plan"]);
    expect(() => enforcer.validateExecution("session-1", "submit")).toThrow(
      StepNotAllowedError
    );
  });
});
