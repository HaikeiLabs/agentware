import type { Message, ToolDefinition } from "../llm/request.js";
import type { Response, ToolCall as LlmToolCall } from "../llm/response.js";
import type { Backend } from "../llm/backend.js";
import type { ContextWindowManager } from "../llmcontext/context_window.js";
import { Role } from "../llm/request.js";
import { MessageType } from "./types.js";
import type { ResponseValidator, ToolCall, ValidationResult } from "./guardrails/response_validator.js";
import { ErrorTracker, ErrorCategory } from "./guardrails/error_tracker.js";
import { StepEnforcer } from "./guardrails/step_enforcer.js";
import { stepNudge } from "./guardrails/nudge.js";
import { ReasoningAdapter, ReasoningError, type ContextTree } from "../reasoning/index.js";

export class RetriesExhaustedError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "RetriesExhaustedError";
  }
}

export interface InferenceResult {
  response: Response;
  newMessages: Message[];
  toolCallCounter: number;
  attempts: number;
  /** Normalized context tree for the final turn's reasoning (native
   * reasoning_content and/or thinking tags). Null when the turn carried no
   * reasoning. Holds bounded summaries only; raw reasoning is never attached. */
  reasoningTree: ContextTree | null;
}

export interface InferenceConfig {
  client: Backend;
  contextManager?: ContextWindowManager;
  validator?: ResponseValidator;
  errorTracker?: ErrorTracker;
  stepEnforcer?: StepEnforcer;
  toolSpecs: ToolDefinition[];
  maxAttempts: number;
  stepIndex?: number;
  /** Optional reasoning adapter override for limits and registered model
   * fields. */
  reasoning?: ReasoningAdapter;
}

// Default adapter strips and normalizes reasoning in the inference loop.
const defaultAdapter = new ReasoningAdapter();

export async function runInference(
  messages: Message[],
  cfg: InferenceConfig,
  sessionId: string = ""
): Promise<InferenceResult | null> {
  let maxAttempts = cfg.maxAttempts;
  if (maxAttempts <= 0) {
    maxAttempts = 3;
  }

  const currentMessages: Message[] = [...messages];

  let lastResponse: Response | null = null;
  let toolCallCounter = 0;
  let attempts = 0;

  while (attempts < maxAttempts) {
    attempts++;

    if (cfg.contextManager) {
      if (cfg.contextManager.shouldCompact(currentMessages)) {
        const compacted = cfg.contextManager.compact(currentMessages);
        currentMessages.length = 0;
        currentMessages.push(...compacted);
      }

      const warning = cfg.contextManager.checkThresholds(currentMessages);
      if (warning) {
        const warningMsg: Message = {
          role: Role.USER,
          content: warning,
          meta: { type: MessageType.CONTEXT_WARNING },
        };
        currentMessages.push(warningMsg);
      }
    }

    let resp: Response;
    try {
      resp = cfg.client.complete(currentMessages, cfg.toolSpecs);
    } catch (e) {
      if (cfg.errorTracker && e instanceof Error) {
        cfg.errorTracker.recordError(sessionId, "", {}, e, ErrorCategory.UNKNOWN);
      }
      throw e;
    }

    if (cfg.contextManager && resp.usage_tokens.total_tokens > 0) {
      cfg.contextManager.updateTokenCount(resp.usage_tokens.total_tokens);
    }

    let validationResult: ValidationResult | null = null;

    // AR-1: normalize reasoning and strip it before ordinary tool-call
    // parsing. Reasoning can arrive as a native reasoning_content field or as
    // embedded thinking tags in content; both are combined into a synthetic
    // structured input so the adapter sees the full picture. Malformed or
    // unbounded reasoning fails closed: this turn is treated as invalid and
    // retried rather than parsed partially.
    let reasoningTree: ContextTree | null = null;
    let cleanContent = resp.content;
    let reasoningFailed = false;
    if (resp.content || resp.reasoning) {
      const reasoningInput: Record<string, unknown> = { content: resp.content };
      if (resp.reasoning) {
        reasoningInput["reasoning_content"] = resp.reasoning;
      }
      const adapter = cfg.reasoning ?? defaultAdapter;
      try {
        reasoningTree = adapter.extract(reasoningInput, cfg.client.modelName(), "llm-backend");
        cleanContent = adapter.strip(reasoningInput);
      } catch (e) {
        if (e instanceof ReasoningError) {
          reasoningFailed = true;
          // Fail closed: never echo content that may carry reasoning we could
          // not parse.
          cleanContent = "";
          validationResult = {
            toolCalls: [],
            nudge: null,
            needsRetry: true,
          };
        } else {
          throw e;
        }
      }
    }

    if (!reasoningFailed) {
      if (resp.tool_calls && resp.tool_calls.length > 0) {
        const guardrailsToolCalls: ToolCall[] = resp.tool_calls.map((tc) => ({
          tool: tc.name,
          args: tc.arguments,
        }));
        if (cfg.validator) {
          validationResult = cfg.validator.validateToolCalls(guardrailsToolCalls);
        } else {
          validationResult = {
            toolCalls: guardrailsToolCalls,
            nudge: null,
            needsRetry: false,
          };
        }
      } else if (cleanContent) {
        if (cfg.validator) {
          validationResult = cfg.validator.validateTextResponse(cleanContent);
        } else {
          validationResult = { toolCalls: [], nudge: null, needsRetry: false };
        }

        if (!validationResult.needsRetry && validationResult.toolCalls.length > 0) {
          resp.tool_calls = validationResult.toolCalls.map(
            (tc) =>
              ({
                id: "",
                name: tc.tool,
                arguments: tc.args,
              } as LlmToolCall)
          );
        }
      } else {
        if (cfg.validator) {
          validationResult = cfg.validator.validateTextResponse("");
        } else {
          validationResult = { toolCalls: [], nudge: null, needsRetry: true };
        }
      }
    }

    lastResponse = resp;

    if (validationResult && !validationResult.needsRetry) {
      if (cfg.errorTracker) {
        cfg.errorTracker.resetSession(sessionId);
      }

      if (cfg.stepEnforcer && resp.tool_calls && resp.tool_calls.length > 0) {
        for (const tc of resp.tool_calls) {
          const [allowed, missing] = cfg.stepEnforcer.canExecute(sessionId, tc.name);
          if (!allowed) {
            const nudge = stepNudge(tc.name, missing, 1);
            const nudgeMsg: Message = {
              role: Role.USER,
              content: nudge.content,
              meta: { type: MessageType.STEP_NUDGE },
            };
            currentMessages.push(nudgeMsg);
            continue;
          }
        }
      }

      toolCallCounter += resp.tool_calls ? resp.tool_calls.length : 0;
      return {
        response: lastResponse,
        newMessages: currentMessages,
        toolCallCounter,
        attempts,
        reasoningTree,
      };
    }

    if (cfg.errorTracker) {
      cfg.errorTracker.recordError(
        sessionId,
        "",
        {},
        new Error("validation failed"),
        ErrorCategory.UNKNOWN
      );
    }

    if (attempts >= maxAttempts) {
      throw new RetriesExhaustedError(`retries exhausted after ${attempts} attempts`);
    }

    if (validationResult && validationResult.nudge) {
      const nudgeMsg: Message = {
        role: Role.USER,
        content: validationResult.nudge.content,
        meta: { type: MessageType.RETRY_NUDGE },
      };
      currentMessages.push(nudgeMsg);
    }

    const failedMsg: Message = {
      role: Role.ASSISTANT,
      content: cleanContent,
      meta: { type: MessageType.TEXT_RESPONSE },
    };
    currentMessages.push(failedMsg);
  }

  throw new RetriesExhaustedError(`retries exhausted after ${attempts} attempts`);
}