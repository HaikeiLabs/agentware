import type { Message, ToolDefinition } from "../src/llm/request.js";
import { Role } from "../src/llm/request.js";
import type { Response, ToolCall as LlmToolCall, TokenUsage } from "../src/llm/response.js";
import { ContextWindowManager } from "../src/llmcontext/context_window.js";
import { ResponseValidator } from "../src/middleware/guardrails/response_validator.js";
import { ErrorTracker } from "../src/middleware/guardrails/error_tracker.js";
import { StepEnforcer } from "../src/middleware/guardrails/step_enforcer.js";
import { MessageType } from "../src/middleware/types.js";
import { Format } from "../src/reasoning/index.js";
import {
  InferenceConfig,
  RetriesExhaustedError,
  runInference,
} from "../src/middleware/inference.js";

interface MockBackend {
  complete(messages: Message[]): Response;
  supportsNativeToolCalling(): boolean;
  modelName(): string;
  contextWindowSize(): number;
}

function createMockResponse(
  content: string,
  toolCalls: LlmToolCall[] = [],
  usageTokens?: TokenUsage,
  reasoning = ""
): Response {
  return {
    content,
    reasoning,
    tool_calls: toolCalls,
    finish_reason: toolCalls.length > 0 ? "tool_calls" : "stop",
    usage_tokens: usageTokens ?? {
      prompt_tokens: 10,
      completion_tokens: 5,
      total_tokens: 15,
    },
  };
}

function createToolDefinition(name: string): ToolDefinition {
  return {
    name,
    description: `A ${name} tool`,
    input_schema: { type: "object", properties: {} },
  };
}

describe("runInference", () => {
  let mockResponses: Response[];
  let callCount: number;

  beforeEach(() => {
    callCount = 0;
    mockResponses = [];
  });

  const createMockBackend = (): MockBackend => ({
    complete: (messages: Message[]): Response => {
      if (callCount >= mockResponses.length) {
        return mockResponses[mockResponses.length - 1];
      }
      const resp = mockResponses[callCount];
      callCount++;
      return resp;
    },
    supportsNativeToolCalling: () => true,
    modelName: () => "mock",
    contextWindowSize: () => 8192,
  });

  // Records the messages of every request so tests can assert on the retry
  // history the backend actually sees.
  const createRecordingBackend = (): MockBackend & { requests: Message[][] } => {
    const requests: Message[][] = [];
    return {
      requests,
      complete: (messages: Message[]): Response => {
        requests.push([...messages]);
        if (callCount >= mockResponses.length) {
          return mockResponses[mockResponses.length - 1];
        }
        const resp = mockResponses[callCount];
        callCount++;
        return resp;
      },
      supportsNativeToolCalling: () => true,
      modelName: () => "mock",
      contextWindowSize: () => 8192,
    };
  };

  it("should return inference result on successful tool call", async () => {
    const messages: Message[] = [{ role: Role.USER, content: "test" }];
    const toolDef = createToolDefinition("test_tool");

    mockResponses = [
      createMockResponse("", [
        { id: "1", name: "test_tool", arguments: {} },
      ]),
    ];

    const backend = createMockBackend();
    const validator = new ResponseValidator(["test_tool"], false);

    const cfg: InferenceConfig = {
      client: backend,
      toolSpecs: [toolDef],
      validator,
      maxAttempts: 3,
      stepIndex: 0,
    };

    const result = await runInference(messages, cfg);

    expect(result).not.toBeNull();
    expect(result!.response.tool_calls).toHaveLength(1);
    expect(result!.response.tool_calls[0].name).toBe("test_tool");
    expect(result!.attempts).toBe(1);
  });

  it("should rescue text response into tool calls", async () => {
    const messages: Message[] = [{ role: Role.USER, content: "test" }];
    const toolDef = createToolDefinition("test_tool");

    mockResponses = [
      createMockResponse('{"tool": "test_tool", "args": {"key": "value"}}', []),
    ];

    const backend = createMockBackend();
    const validator = new ResponseValidator(["test_tool"], true);

    const cfg: InferenceConfig = {
      client: backend,
      toolSpecs: [toolDef],
      validator,
      maxAttempts: 3,
      stepIndex: 0,
    };

    const result = await runInference(messages, cfg);

    expect(result).not.toBeNull();
    expect(result!.response.tool_calls).toHaveLength(1);
  });

  it("passes tool definitions on every retry", async () => {
    const calls: (ToolDefinition[] | undefined)[] = [];
    const backend = {
      complete: (_messages: Message[], tools?: ToolDefinition[]): Response => {
        calls.push(tools);
        return calls.length === 1
          ? createMockResponse("invalid text")
          : createMockResponse("", [{ id: "1", name: "test_tool", arguments: {} }]);
      },
      supportsNativeToolCalling: () => false,
      modelName: () => "mock",
      contextWindowSize: () => 8192,
    };
    const cfg: InferenceConfig = {
      client: backend,
      toolSpecs: [createToolDefinition("test_tool")],
      validator: new ResponseValidator(["test_tool"], false),
      maxAttempts: 2,
    };

    await runInference([{ role: Role.USER, content: "test" }], cfg);
    expect(calls).toHaveLength(2);
    expect(calls).toEqual([cfg.toolSpecs, cfg.toolSpecs]);
  });

  it("should retry on invalid response", async () => {
    const messages: Message[] = [{ role: Role.USER, content: "test" }];
    const toolDef = createToolDefinition("test_tool");

    mockResponses = [
      createMockResponse("This is just text, not a tool call", []),
      createMockResponse("", [{ id: "1", name: "test_tool", arguments: {} }]),
    ];

    const backend = createMockBackend();
    const validator = new ResponseValidator(["test_tool"], false);

    const cfg: InferenceConfig = {
      client: backend,
      toolSpecs: [toolDef],
      validator,
      maxAttempts: 3,
      stepIndex: 0,
    };

    const result = await runInference(messages, cfg);

    expect(result).not.toBeNull();
    expect(result!.attempts).toBe(2);
  });

  it("should throw RetriesExhaustedError when max attempts exceeded", async () => {
    const messages: Message[] = [{ role: Role.USER, content: "test" }];
    const toolDef = createToolDefinition("test_tool");

    mockResponses = [
      createMockResponse("This is just text", []),
    ];

    const backend = createMockBackend();
    const validator = new ResponseValidator(["test_tool"], false);

    const cfg: InferenceConfig = {
      client: backend,
      toolSpecs: [toolDef],
      validator,
      maxAttempts: 2,
      stepIndex: 0,
    };

    await expect(runInference(messages, cfg)).rejects.toThrow(RetriesExhaustedError);
  });

  it("should trigger context compaction when needed", async () => {
    const messages: Message[] = [{ role: Role.USER, content: "test" }];
    const toolDef = createToolDefinition("test_tool");

    mockResponses = [
      createMockResponse("", [{ id: "1", name: "test_tool", arguments: {} }]),
    ];

    const backend = createMockBackend();
    const validator = new ResponseValidator(["test_tool"], false);
    
    const mockCtxManager = {
      shouldCompact: () => true,
      compact: (msgs: Message[]) => [msgs[0]],
      addWarning: () => {},
      checkThresholds: () => undefined,
      updateTokenCount: () => {},
    } as unknown as ContextWindowManager;

    const cfg: InferenceConfig = {
      client: backend,
      contextManager: mockCtxManager,
      toolSpecs: [toolDef],
      validator,
      maxAttempts: 3,
      stepIndex: 0,
    };

    const result = await runInference(messages, cfg);
    expect(result).not.toBeNull();
  });

  it("should inject context threshold warnings", async () => {
    const messages: Message[] = [{ role: Role.USER, content: "test" }];
    const toolDef = createToolDefinition("test_tool");

    mockResponses = [
      createMockResponse("", [{ id: "1", name: "test_tool", arguments: {} }], {
        prompt_tokens: 10,
        completion_tokens: 5,
        total_tokens: 8000,
      }),
    ];

    const backend = createMockBackend();
    const validator = new ResponseValidator(["test_tool"], false);
    const ctxManager = new ContextWindowManager(10000);

    const cfg: InferenceConfig = {
      client: backend,
      contextManager: ctxManager,
      toolSpecs: [toolDef],
      validator,
      maxAttempts: 3,
      stepIndex: 0,
    };

    const result = await runInference(messages, cfg);
    expect(result).not.toBeNull();
  });

  it("should use error tracker on validation failures", async () => {
    const messages: Message[] = [{ role: Role.USER, content: "test" }];
    const toolDef = createToolDefinition("test_tool");

    mockResponses = [
      createMockResponse("This is just text", []),
      createMockResponse("", [{ id: "1", name: "test_tool", arguments: {} }]),
    ];

    const backend = createMockBackend();
    const validator = new ResponseValidator(["test_tool"], false);
    const errorTracker = new ErrorTracker(5);

    const cfg: InferenceConfig = {
      client: backend,
      toolSpecs: [toolDef],
      validator,
      errorTracker,
      maxAttempts: 5,
      stepIndex: 0,
    };

    const result = await runInference(messages, cfg, "test-session");
    expect(result).not.toBeNull();
  });

  it("should check step enforcer for tool prerequisites", async () => {
    const messages: Message[] = [{ role: Role.USER, content: "test" }];
    const toolDef = createToolDefinition("final_tool");

    mockResponses = [
      createMockResponse("", [{ id: "1", name: "final_tool", arguments: {} }]),
    ];

    const backend = createMockBackend();
    const validator = new ResponseValidator(["final_tool", "step1"], false);
    const stepEnforcer = new StepEnforcer();
    stepEnforcer.addStep("final_tool", ["step1"]);

    const cfg: InferenceConfig = {
      client: backend,
      toolSpecs: [toolDef],
      validator,
      stepEnforcer,
      maxAttempts: 3,
      stepIndex: 0,
    };

    const result = await runInference(messages, cfg, "test-session");
    expect(result).not.toBeNull();
  });

  it("should retry on empty response", async () => {
    const messages: Message[] = [{ role: Role.USER, content: "test" }];
    const toolDef = createToolDefinition("test_tool");

    mockResponses = [
      createMockResponse("", []),
      createMockResponse("", [{ id: "1", name: "test_tool", arguments: {} }]),
    ];

    const backend = createMockBackend();
    const validator = new ResponseValidator(["test_tool"], false);

    const cfg: InferenceConfig = {
      client: backend,
      toolSpecs: [toolDef],
      validator,
      maxAttempts: 3,
      stepIndex: 0,
    };

    const result = await runInference(messages, cfg);

    expect(result).not.toBeNull();
    expect(result!.attempts).toBe(2);
  });

  it("should respect max attempts from config", async () => {
    const messages: Message[] = [{ role: Role.USER, content: "test" }];
    const toolDef = createToolDefinition("test_tool");

    mockResponses = [
      createMockResponse("This is just text", []),
    ];

    const backend = createMockBackend();
    const validator = new ResponseValidator(["test_tool"], false);

    const cfg: InferenceConfig = {
      client: backend,
      toolSpecs: [toolDef],
      validator,
      maxAttempts: 5,
      stepIndex: 0,
    };

    await expect(runInference(messages, cfg)).rejects.toThrow(
      "retries exhausted after 5 attempts"
    );
  });

  it("should use default max attempts when zero", async () => {
    const messages: Message[] = [{ role: Role.USER, content: "test" }];
    const toolDef = createToolDefinition("test_tool");

    mockResponses = [
      createMockResponse("This is just text", []),
    ];

    const backend = createMockBackend();
    const validator = new ResponseValidator(["test_tool"], false);

    const cfg: InferenceConfig = {
      client: backend,
      toolSpecs: [toolDef],
      validator,
      maxAttempts: 0,
      stepIndex: 0,
    };

    await expect(runInference(messages, cfg)).rejects.toThrow(RetriesExhaustedError);
  });

  it("should work without optional guardrail components", async () => {
    const messages: Message[] = [{ role: Role.USER, content: "test" }];
    const toolDef = createToolDefinition("test_tool");

    mockResponses = [
      createMockResponse("", [{ id: "1", name: "test_tool", arguments: {} }]),
    ];

    const backend = createMockBackend();

    const cfg: InferenceConfig = {
      client: backend,
      toolSpecs: [toolDef],
      maxAttempts: 3,
      stepIndex: 0,
    };

    const result = await runInference(messages, cfg);

    expect(result).not.toBeNull();
    expect(result!.attempts).toBe(1);
  });

  describe("AR-1 reasoning", () => {
    it("strips thinking tags before tool-call parsing and attaches a tree", async () => {
      const messages: Message[] = [{ role: Role.USER, content: "test" }];
      const toolDef = createToolDefinition("test_tool");

      mockResponses = [
        createMockResponse(
          "<thinking>goal: answer\n\nobservation: the index is fresh</thinking>\n" +
            '{"tool": "test_tool", "args": {"key": "value"}}'
        ),
      ];

      const backend = createMockBackend();
      const validator = new ResponseValidator(["test_tool"], true);

      const cfg: InferenceConfig = {
        client: backend,
        toolSpecs: [toolDef],
        validator,
        maxAttempts: 3,
        stepIndex: 0,
      };

      const result = await runInference(messages, cfg);

      expect(result).not.toBeNull();
      expect(result!.response.tool_calls).toHaveLength(1);
      expect(result!.response.tool_calls[0].name).toBe("test_tool");

      expect(result!.reasoningTree).not.toBeNull();
      expect(result!.reasoningTree!.nodes).toHaveLength(2);
      for (const node of result!.reasoningTree!.nodes) {
        expect(Array.from(node.summary).length).toBeLessThanOrEqual(512);
        expect(node.summary).not.toBe("goal: answer\n\nobservation: the index is fresh");
      }
    });

    it("builds a native-format tree from reasoning_content", async () => {
      const messages: Message[] = [{ role: Role.USER, content: "test" }];
      const toolDef = createToolDefinition("test_tool");

      mockResponses = [
        createMockResponse(
          '{"tool": "test_tool", "args": {"key": "value"}}',
          [],
          undefined,
          "observation: the index is fresh\n\ndecision: call test_tool"
        ),
      ];

      const backend = createMockBackend();
      const validator = new ResponseValidator(["test_tool"], true);

      const cfg: InferenceConfig = {
        client: backend,
        toolSpecs: [toolDef],
        validator,
        maxAttempts: 3,
        stepIndex: 0,
      };

      const result = await runInference(messages, cfg);

      expect(result).not.toBeNull();
      expect(result!.reasoningTree?.format).toBe(Format.NATIVE);
      expect(result!.response.tool_calls).toHaveLength(1);
    });

    it("fails closed on unbalanced reasoning", async () => {
      const messages: Message[] = [{ role: Role.USER, content: "test" }];
      const toolDef = createToolDefinition("test_tool");

      mockResponses = [
        createMockResponse("<thinking>never closed\n" + '{"tool": "test_tool", "args": {}}'),
      ];

      const backend = createMockBackend();
      const validator = new ResponseValidator(["test_tool"], true);

      const cfg: InferenceConfig = {
        client: backend,
        toolSpecs: [toolDef],
        validator,
        maxAttempts: 3,
        stepIndex: 0,
      };

      await expect(runInference(messages, cfg)).rejects.toThrow(RetriesExhaustedError);
    });
  });

  describe("retry history order", () => {
    // The failed assistant turn must precede the retry nudge, so the next
    // request ends with a user message (current Claude models reject a request
    // that ends with an assistant turn, and the model reads the correction
    // before the answer it corrects). A failed reply that carries tool_calls
    // contributes no unanswered tool_calls to the history, and an empty or
    // whitespace-only failed turn is skipped entirely.
    it("appends the failed assistant turn before the nudge", async () => {
      const messages: Message[] = [{ role: Role.USER, content: "test" }];
      const toolDef = createToolDefinition("test_tool");

      mockResponses = [
        createMockResponse("Tokyo is warmer at 22 degrees today."),
        createMockResponse("", [{ id: "1", name: "test_tool", arguments: {} }]),
      ];

      const backend = createRecordingBackend();
      const validator = new ResponseValidator(["test_tool"], false);

      const cfg: InferenceConfig = {
        client: backend,
        toolSpecs: [toolDef],
        validator,
        maxAttempts: 3,
        stepIndex: 0,
      };

      const result = await runInference(messages, cfg);

      expect(result).not.toBeNull();
      expect(result!.attempts).toBe(2);
      expect(backend.requests).toHaveLength(2);

      const second = backend.requests[1];
      const tail = second.slice(-2);
      expect(tail[0].role).toBe(Role.ASSISTANT);
      expect(tail[0].content).toBe("Tokyo is warmer at 22 degrees today.");
      expect(tail[0].meta?.type).toBe(MessageType.TEXT_RESPONSE);
      expect(tail[1].role).toBe(Role.USER);
      expect(tail[1].content).toContain("not a valid tool call");
      expect(tail[1].meta?.type).toBe(MessageType.RETRY_NUDGE);

      // The returned history carries the same order.
      expect(result!.newMessages.slice(-2)).toEqual(tail);
    });

    it("skips an empty assistant turn on retry", async () => {
      const messages: Message[] = [{ role: Role.USER, content: "test" }];
      const toolDef = createToolDefinition("test_tool");

      mockResponses = [
        createMockResponse("", [{ id: "1", name: "weather_lookup", arguments: {} }]),
        createMockResponse("", [{ id: "2", name: "test_tool", arguments: {} }]),
      ];

      const backend = createRecordingBackend();
      const validator = new ResponseValidator(["test_tool"], false);

      const cfg: InferenceConfig = {
        client: backend,
        toolSpecs: [toolDef],
        validator,
        maxAttempts: 3,
        stepIndex: 0,
      };

      const result = await runInference(messages, cfg);

      expect(result).not.toBeNull();
      expect(result!.attempts).toBe(2);
      expect(backend.requests).toHaveLength(2);

      const second = backend.requests[1];
      for (const msg of second) {
        if (msg.role === Role.ASSISTANT) {
          expect(msg.content.trim()).not.toBe("");
        }
      }
      const last = second[second.length - 1];
      expect(last.role).toBe(Role.USER);
      // The nudge names the failed call, the only record of it left.
      expect(last.content).toContain("weather_lookup");
      expect(result!.newMessages[result!.newMessages.length - 1]).toBe(last);
    });

    it("skips a whitespace-only assistant turn on retry", async () => {
      const messages: Message[] = [{ role: Role.USER, content: "test" }];
      const toolDef = createToolDefinition("test_tool");

      mockResponses = [
        createMockResponse("   \n\t  "),
        createMockResponse("", [{ id: "1", name: "test_tool", arguments: {} }]),
      ];

      const backend = createRecordingBackend();
      const validator = new ResponseValidator(["test_tool"], false);

      const cfg: InferenceConfig = {
        client: backend,
        toolSpecs: [toolDef],
        validator,
        maxAttempts: 3,
        stepIndex: 0,
      };

      const result = await runInference(messages, cfg);

      expect(result).not.toBeNull();
      expect(backend.requests).toHaveLength(2);

      const second = backend.requests[1];
      for (const msg of second) {
        if (msg.role === Role.ASSISTANT) {
          expect(msg.content.trim()).not.toBe("");
        }
      }
      expect(second[second.length - 1].role).toBe(Role.USER);
      expect(result!.newMessages[result!.newMessages.length - 1].role).toBe(Role.USER);
    });

    it("carries no unanswered tool_calls in the retry history", async () => {
      const messages: Message[] = [{ role: Role.USER, content: "test" }];
      const toolDef = createToolDefinition("test_tool");

      // Text response with content AND a native tool call to an unknown tool:
      // the turn is kept for its text but the tool_calls are dropped.
      mockResponses = [
        createMockResponse("Let me check the weather first.", [
          { id: "1", name: "weather_lookup", arguments: {} },
        ]),
        createMockResponse("", [{ id: "2", name: "test_tool", arguments: {} }]),
      ];

      const backend = createRecordingBackend();
      const validator = new ResponseValidator(["test_tool"], false);

      const cfg: InferenceConfig = {
        client: backend,
        toolSpecs: [toolDef],
        validator,
        maxAttempts: 3,
        stepIndex: 0,
      };

      const result = await runInference(messages, cfg);

      expect(result).not.toBeNull();
      expect(backend.requests).toHaveLength(2);

      const second = backend.requests[1];
      for (const msg of second) {
        if (msg.role === Role.ASSISTANT) {
          expect(msg.tool_calls ?? []).toHaveLength(0);
        }
      }
      const tail = second.slice(-2);
      expect(tail[0].role).toBe(Role.ASSISTANT);
      expect(tail[0].content).toBe("Let me check the weather first.");
      expect(tail[1].role).toBe(Role.USER);
      expect(tail[1].content).toContain("weather_lookup");
      expect(result!.newMessages.slice(-2)).toEqual(tail);
    });
  });
});