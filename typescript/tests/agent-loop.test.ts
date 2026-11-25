import { jest } from "@jest/globals";
import { createAllowDecision, createDenyDecision } from "../src/middleware/types.js";
import type { AsyncBackend, Message } from "../src/llm/index.js";
import { Role } from "../src/llm/index.js";
import type { Response, TokenUsage } from "../src/llm/response.js";
import {
  AgentLoop,
  AgentTerminationReason,
  categorizeError,
} from "../src/executor/index.js";
import {
  BaseTool,
  Result,
  ToolRegistry,
} from "../src/tools/index.js";
import {
  Action,
  InMemoryAuditor,
  ErrorCategory,
  ErrorTracker,
  ResponseValidator,
  StepEnforcer,
} from "../src/middleware/index.js";
import { Format } from "../src/reasoning/index.js";

const USAGE: TokenUsage = {
  prompt_tokens: 10,
  completion_tokens: 5,
  total_tokens: 15,
};

function textResponse(content: string): Response {
  return {
    content,
    reasoning: "",
    tool_calls: [],
    finish_reason: "stop",
    usage_tokens: USAGE,
  };
}

function toolCallResponse(name: string, args: Record<string, unknown>): Response {
  return {
    content: "",
    reasoning: "",
    tool_calls: [{ id: `call_${name}`, name, arguments: args }],
    finish_reason: "tool_calls",
    usage_tokens: USAGE,
  };
}

interface ScriptedBackend extends AsyncBackend {
  calls: Message[][];
}

function scriptedBackend(responses: Response[]): ScriptedBackend {
  const calls: Message[][] = [];
  let i = 0;
  return {
    calls,
    complete: async (messages: Message[]): Promise<Response> => {
      calls.push([...messages]);
      const resp = responses[Math.min(i, responses.length - 1)];
      i++;
      return resp;
    },
    supportsNativeToolCalling: () => true,
    modelName: () => "mock",
    contextWindowSize: () => 8192,
  };
}

class OkTool extends BaseTool {
  constructor() {
    super("ok_tool", "Succeeds");
  }

  execute(_args: Record<string, unknown>): Result {
    return new Result(true, "ok");
  }
}

class PrepTool extends BaseTool {
  constructor() {
    super("prep_tool", "Prepares state");
  }

  execute(_args: Record<string, unknown>): Result {
    return new Result(true, "prepared");
  }
}

class FinalTool extends BaseTool {
  constructor() {
    super("final_tool", "Finishes the task");
  }

  execute(_args: Record<string, unknown>): Result {
    return new Result(true, "finished");
  }
}

class FlakyTool extends BaseTool {
  constructor() {
    super("flaky_tool", "Fails with a timeout");
  }

  execute(_args: Record<string, unknown>): Result {
    return new Result(false, null, "request timed out after 5000ms");
  }
}

function makeRegistry(): ToolRegistry {
  const registry = new ToolRegistry();
  registry.register(new OkTool());
  registry.register(new PrepTool());
  registry.register(new FinalTool());
  registry.register(new FlakyTool());
  return registry;
}

describe("AgentLoop", () => {
  it.each(["deny", "allow", "throws"] as const)("enforces and audits policy: %s", async (outcome) => {
    const backend = scriptedBackend([toolCallResponse("ok_tool", { x: 1 }), textResponse("done")]);
    const registry = makeRegistry();
    const tool = registry.get("ok_tool")!;
    const execute = jest.spyOn(tool, "execute");
    const auditor = new InMemoryAuditor();
    const caller = { trusted: false, invoking_subject: "human", delegation_depth: 2 };
    const evaluate = jest.fn(async () => {
      if (outcome === "throws") throw new Error("unavailable");
      return outcome === "allow" ? createAllowDecision() : createDenyDecision("restricted");
    });
    const loop = new AgentLoop({ backend, registry,
      validator: new ResponseValidator(registry.names(), false),
      policy: { evaluate }, auditor, caller_context: caller,
    });
    const result = await loop.run("sys", "do it", [], "session");
    expect(evaluate).toHaveBeenCalledWith("ok_tool", { x: 1 }, caller);
    expect(execute).toHaveBeenCalledTimes(outcome === "allow" ? 1 : 0);
    const record = auditor.query({})[0];
    expect(record).toMatchObject({ session_id: "session", tool_name: "ok_tool",
      invoking_subject: "human", delegation_depth: 2,
      decision: { action: outcome === "allow" ? Action.ALLOW : Action.DENY } });
    expect(auditor.query({})).toHaveLength(1);
    const message = result.conversation.find((m) => m.role === Role.TOOL)!;
    expect(message.content).toContain(outcome === "allow" ? "result" : outcome === "deny" ? "restricted" : "policy evaluation failed");
  });

  it("executes a native tool call then completes on final text", async () => {
    const backend = scriptedBackend([
      toolCallResponse("ok_tool", { x: 1 }),
      textResponse("done"),
    ]);
    const registry = makeRegistry();
    const loop = new AgentLoop({
      backend,
      registry,
      validator: new ResponseValidator(registry.names(), false),
    });

    const result = await loop.run("sys", "do it");

    expect(result.termination_reason).toBe(AgentTerminationReason.COMPLETE);
    expect(result.final_response).toBe("done");
    expect(result.iterations).toBe(2);
    expect(result.tool_calls_made).toBe(1);
    expect(result.nudges).toBe(0);
    const toolMessages = result.conversation.filter(
      (m) => m.role === Role.TOOL
    );
    expect(toolMessages).toHaveLength(1);
    expect(toolMessages[0].content).toBe('Tool ok_tool result: "ok"');
    expect(backend.calls).toHaveLength(2);
  });

  it("rescues a tool call embedded in text via the validator", async () => {
    const backend = scriptedBackend([
      textResponse('{"tool": "ok_tool", "args": {}}'),
      textResponse("done"),
    ]);
    const registry = makeRegistry();
    const loop = new AgentLoop({
      backend,
      registry,
      validator: new ResponseValidator(registry.names(), true),
    });

    const result = await loop.run("sys", "do it");

    expect(result.termination_reason).toBe(AgentTerminationReason.COMPLETE);
    expect(result.tool_calls_made).toBe(1);
    expect(result.nudges).toBe(0);
  });

  it("nudges on invalid text and retries until a valid call", async () => {
    const backend = scriptedBackend([
      textResponse("I will think about it."),
      toolCallResponse("ok_tool", {}),
    ]);
    const registry = makeRegistry();
    const loop = new AgentLoop({
      backend,
      registry,
      validator: new ResponseValidator(registry.names(), false),
      require_tool_call: true,
      max_iterations: 2,
    });

    const result = await loop.run("sys", "do it");

    expect(result.termination_reason).toBe(
      AgentTerminationReason.MAX_ITERATIONS
    );
    expect(result.nudges).toBe(1);
    expect(result.tool_calls_made).toBe(1);
    const nudge = result.conversation.find(
      (m) => m.meta?.type === "retry_nudge"
    );
    expect(nudge).toBeDefined();
    expect(nudge!.role).toBe(Role.USER);
  });

  it("terminates with NUDGES_EXHAUSTED when the model never recovers", async () => {
    const backend = scriptedBackend([textResponse("still just text")]);
    const registry = makeRegistry();
    const loop = new AgentLoop({
      backend,
      registry,
      validator: new ResponseValidator(registry.names(), false),
      max_nudges: 2,
      require_tool_call: true,
    });

    const result = await loop.run("sys", "do it");

    expect(result.termination_reason).toBe(
      AgentTerminationReason.NUDGES_EXHAUSTED
    );
    expect(result.nudges).toBe(2);
  });

  it("treats plain text as the final answer by default", async () => {
    const backend = scriptedBackend([textResponse("all done")]);
    const registry = makeRegistry();
    const loop = new AgentLoop({
      backend,
      registry,
      validator: new ResponseValidator(registry.names(), false),
    });

    const result = await loop.run("sys", "do it");

    expect(result.termination_reason).toBe(AgentTerminationReason.COMPLETE);
    expect(result.final_response).toBe("all done");
    expect(result.iterations).toBe(1);
    expect(result.nudges).toBe(0);
    expect(result.tool_calls_made).toBe(0);
  });

  it("nudges a premature terminal via the step enforcer, then allows it", async () => {
    const backend = scriptedBackend([
      toolCallResponse("final_tool", {}),
      toolCallResponse("prep_tool", {}),
      toolCallResponse("final_tool", {}),
      textResponse("done"),
    ]);
    const registry = makeRegistry();
    const stepEnforcer = new StepEnforcer();
    stepEnforcer.addStep("final_tool", ["prep_tool"]);

    const loop = new AgentLoop({
      backend,
      registry,
      validator: new ResponseValidator(registry.names(), false),
      step_enforcer: stepEnforcer,
    });

    const result = await loop.run("sys", "do it", [], "session-1");

    expect(result.termination_reason).toBe(AgentTerminationReason.COMPLETE);
    expect(result.tool_calls_made).toBe(2);
    expect(result.nudges).toBe(1);
    const stepNudge = result.conversation.find(
      (m) => m.meta?.type === "step_nudge"
    );
    expect(stepNudge).toBeDefined();
    expect(stepNudge!.content).toContain("prep_tool");
  });

  it("blocks a tool after the error tracker threshold and records categories", async () => {
    const backend = scriptedBackend([
      toolCallResponse("flaky_tool", {}),
      toolCallResponse("flaky_tool", {}),
      toolCallResponse("flaky_tool", {}),
      textResponse("done"),
    ]);
    const registry = makeRegistry();
    const errorTracker = new ErrorTracker(2, 5);

    const loop = new AgentLoop({
      backend,
      registry,
      validator: new ResponseValidator(registry.names(), false),
      error_tracker: errorTracker,
    });

    const result = await loop.run("sys", "do it", [], "session-2");

    expect(result.termination_reason).toBe(AgentTerminationReason.COMPLETE);
    expect(result.tool_calls_made).toBe(2);
    expect(errorTracker.getErrorCount("session-2", "flaky_tool")).toBe(2);
    expect(
      errorTracker.getErrorsByCategory("session-2", ErrorCategory.TIMEOUT)
    ).toHaveLength(2);
    const blocked = result.conversation.find((m) =>
      m.content.includes("blocked after repeated errors")
    );
    expect(blocked).toBeDefined();
  });

  it("terminates with MAX_ITERATIONS when tools never finish", async () => {
    const backend = scriptedBackend([toolCallResponse("ok_tool", {})]);
    const registry = makeRegistry();
    const loop = new AgentLoop({
      backend,
      registry,
      validator: new ResponseValidator(registry.names(), false),
      max_iterations: 2,
    });

    const result = await loop.run("sys", "do it");

    expect(result.termination_reason).toBe(
      AgentTerminationReason.MAX_ITERATIONS
    );
    expect(result.iterations).toBe(2);
    expect(result.tool_calls_made).toBe(2);
  });

  it("terminates with ERROR and records the failure when the backend throws", async () => {
    const failing: AsyncBackend = {
      complete: async (): Promise<Response> => {
        throw new Error("backend down");
      },
      supportsNativeToolCalling: () => true,
      modelName: () => "mock",
      contextWindowSize: () => 8192,
    };
    const registry = makeRegistry();
    const errorTracker = new ErrorTracker(5, 5);
    const loop = new AgentLoop({
      backend: failing,
      registry,
      validator: new ResponseValidator(registry.names(), false),
      error_tracker: errorTracker,
    });

    const result = await loop.run("sys", "do it", [], "session-3");

    expect(result.termination_reason).toBe(AgentTerminationReason.ERROR);
    expect(errorTracker.getErrorCount("session-3", "")).toBe(1);
  });

  it("rejects unknown tools through the validator with an unknown-tool nudge", async () => {
    const backend = scriptedBackend([
      toolCallResponse("ghost_tool", {}),
      toolCallResponse("ok_tool", {}),
      textResponse("done"),
    ]);
    const registry = makeRegistry();
    const loop = new AgentLoop({
      backend,
      registry,
      validator: new ResponseValidator(registry.names(), false),
    });

    const result = await loop.run("sys", "do it");

    expect(result.termination_reason).toBe(AgentTerminationReason.COMPLETE);
    expect(result.nudges).toBe(1);
    const nudge = result.conversation.find(
      (m) => m.meta?.type === "retry_nudge"
    );
    expect(nudge!.content).toContain("ghost_tool");
  });
});

describe("AgentLoop reasoning parity (AR-1)", () => {
  it("builds a native-format tree from reasoning_content and keeps it out of the reply", async () => {
    const backend = scriptedBackend([
      {
        ...textResponse('{"tool": "ok_tool", "args": {}}'),
        reasoning: "observation: the index is fresh\n\ndecision: call ok_tool",
      },
      textResponse("done"),
    ]);
    const registry = makeRegistry();
    const loop = new AgentLoop({
      backend,
      registry,
      validator: new ResponseValidator(registry.names(), true),
    });

    const result = await loop.run("sys", "do it");

    expect(result.termination_reason).toBe(AgentTerminationReason.COMPLETE);
    expect(result.tool_calls_made).toBe(1);
    expect(result.reasoning_tree).not.toBeNull();
    expect(result.reasoning_tree!.format).toBe(Format.NATIVE);
    expect(result.reasoning_tree!.nodes).toHaveLength(2);
    expect(result.final_response).toBe("done");
  });

  it("strips tagged reasoning before validation and attaches a thinking tree", async () => {
    const backend = scriptedBackend([
      textResponse(
        "<thinking>goal: answer\n\nobservation: the index is fresh</thinking>\n" +
          '{"tool": "ok_tool", "args": {}}'
      ),
      textResponse("done"),
    ]);
    const registry = makeRegistry();
    const loop = new AgentLoop({
      backend,
      registry,
      validator: new ResponseValidator(registry.names(), true),
    });

    const result = await loop.run("sys", "do it");

    expect(result.termination_reason).toBe(AgentTerminationReason.COMPLETE);
    expect(result.tool_calls_made).toBe(1);
    expect(result.reasoning_tree).not.toBeNull();
    expect(result.reasoning_tree!.format).toBe(Format.THINKING);
    expect(result.reasoning_tree!.nodes).toHaveLength(2);
  });

  it("never exposes raw reasoning in the reply, conversation, or tree", async () => {
    const raw = "observation: secret chain of thought\n\ndecision: call ok_tool";
    const backend = scriptedBackend([
      {
        ...textResponse('{"tool": "ok_tool", "args": {}}'),
        reasoning: raw,
      },
      textResponse("done"),
    ]);
    const registry = makeRegistry();
    const loop = new AgentLoop({
      backend,
      registry,
      validator: new ResponseValidator(registry.names(), true),
    });

    const result = await loop.run("sys", "do it");

    expect(result.final_response).not.toContain("secret chain of thought");
    for (const message of result.conversation) {
      expect(message.content).not.toContain("secret chain of thought");
    }
    for (const node of result.reasoning_tree!.nodes) {
      expect(Array.from(node.summary).length).toBeLessThanOrEqual(512);
      expect(node.summary).not.toBe(raw);
    }
  });

  it("fails closed on malformed reasoning instead of a partial parse", async () => {
    const backend = scriptedBackend([
      textResponse('<thinking>never closed\n{"tool": "ok_tool", "args": {}}'),
    ]);
    const registry = makeRegistry();
    const loop = new AgentLoop({
      backend,
      registry,
      validator: new ResponseValidator(registry.names(), true),
      max_nudges: 2,
    });

    const result = await loop.run("sys", "do it");

    expect(result.termination_reason).toBe(
      AgentTerminationReason.NUDGES_EXHAUSTED
    );
    expect(result.tool_calls_made).toBe(0);
    expect(result.nudges).toBe(2);
    expect(result.reasoning_tree).toBeNull();
    for (const message of result.conversation) {
      expect(message.content).not.toContain("never closed");
    }
  });

  it("places the system prompt ahead of caller-supplied history", async () => {
    const backend = scriptedBackend([textResponse("done")]);
    const registry = makeRegistry();
    const loop = new AgentLoop({
      backend,
      registry,
      validator: new ResponseValidator(registry.names(), false),
    });
    const history: Message[] = [
      { role: Role.USER, content: "h1" },
      { role: Role.ASSISTANT, content: "h2" },
    ];

    await loop.run("sys", "do it", history);

    const sent = backend.calls[0];
    expect(sent[0].role).toBe(Role.SYSTEM);
    expect(sent[0].content).toBe("sys");
    expect(sent[1].content).toBe("h1");
    expect(sent[2].content).toBe("h2");
    expect(sent[3].role).toBe(Role.USER);
    expect(sent[3].content).toBe("do it");
  });
});

describe("categorizeError", () => {
  it("maps messages to error categories", () => {
    expect(categorizeError("request timed out")).toBe(ErrorCategory.TIMEOUT);
    expect(categorizeError("tool not found")).toBe(ErrorCategory.NOT_FOUND);
    expect(categorizeError("invalid arguments schema")).toBe(
      ErrorCategory.INVALID_ARGS
    );
    expect(categorizeError("permission denied")).toBe(ErrorCategory.PERMISSION);
    expect(categorizeError("rate limit exceeded")).toBe(ErrorCategory.RATE_LIMIT);
    expect(categorizeError("something else")).toBe(ErrorCategory.UNKNOWN);
  });
});
