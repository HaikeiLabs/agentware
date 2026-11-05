import type { AsyncBackend, Message, ToolDefinition } from "../llm/index.js";
import { Role } from "../llm/index.js";
import type { Response } from "../llm/response.js";
import type { ToolRegistry } from "../tools/index.js";
import { executeTool, Result } from "../tools/index.js";
import type {
  ResponseValidator,
  ValidationResult,
} from "../middleware/guardrails/response_validator.js";
import {
  ErrorTracker,
  ErrorCategory,
} from "../middleware/guardrails/error_tracker.js";
import { StepEnforcer } from "../middleware/guardrails/step_enforcer.js";
import {
  Nudge,
  NudgeKind,
  stepNudge,
} from "../middleware/guardrails/nudge.js";
import { MessageType } from "../middleware/types.js";
import {
  ReasoningAdapter,
  ReasoningError,
  isContextTreeEmpty,
  type ContextTree,
} from "../reasoning/index.js";

export enum AgentTerminationReason {
  COMPLETE = "complete",
  MAX_ITERATIONS = "max_iterations",
  NUDGES_EXHAUSTED = "nudges_exhausted",
  ERROR = "error",
}

export interface AgentLoopConfig {
  backend: AsyncBackend;
  registry: ToolRegistry;
  validator: ResponseValidator;
  error_tracker?: ErrorTracker;
  step_enforcer?: StepEnforcer;
  max_iterations?: number;
  max_nudges?: number;
  require_tool_call?: boolean;
  /** Optional reasoning adapter override for limits and registered model
   * fields. */
  reasoning?: ReasoningAdapter;
}

export interface AgentResult {
  final_response: string;
  iterations: number;
  tool_calls_made: number;
  nudges: number;
  termination_reason: AgentTerminationReason;
  conversation: Message[];
  /** Normalized context tree for the last turn that carried reasoning
   * (native reasoning_content and/or thinking tags). Null when no turn
   * carried reasoning. Holds bounded summaries only; raw reasoning is never
   * attached. */
  reasoning_tree: ContextTree | null;
}

// Default adapter strips and normalizes reasoning in the agent loop.
const defaultAdapter = new ReasoningAdapter();

export function registryToolDefinitions(
  registry: ToolRegistry
): ToolDefinition[] {
  return registry.all().map((tool) => ({
    name: tool.name,
    description: tool.description,
    input_schema:
      "inputSchema" in tool
        ? (tool as unknown as { inputSchema(): Record<string, unknown> }).inputSchema()
        : {},
  }));
}

export function categorizeError(message: string): ErrorCategory {
  const m = message.toLowerCase();
  if (m.includes("timeout") || m.includes("timed out")) {
    return ErrorCategory.TIMEOUT;
  }
  if (m.includes("not found") || m.includes("unknown tool")) {
    return ErrorCategory.NOT_FOUND;
  }
  if (
    m.includes("invalid arg") ||
    m.includes("schema") ||
    m.includes("validation")
  ) {
    return ErrorCategory.INVALID_ARGS;
  }
  if (
    m.includes("permission") ||
    m.includes("denied") ||
    m.includes("forbidden")
  ) {
    return ErrorCategory.PERMISSION;
  }
  if (m.includes("rate limit") || m.includes("rate_limit")) {
    return ErrorCategory.RATE_LIMIT;
  }
  return ErrorCategory.UNKNOWN;
}

function nudgeMetaType(kind: NudgeKind): MessageType {
  switch (kind) {
    case NudgeKind.STEP:
      return MessageType.STEP_NUDGE;
    case NudgeKind.PREREQUISITE:
      return MessageType.PREREQUISITE_NUDGE;
    default:
      return MessageType.RETRY_NUDGE;
  }
}

function toError(err: unknown): Error {
  return err instanceof Error ? err : new Error(String(err));
}

export class AgentLoop {
  private readonly config: AgentLoopConfig;
  private readonly maxIterations: number;
  private readonly maxNudges: number;

  constructor(config: AgentLoopConfig) {
    this.config = config;
    this.maxIterations =
      config.max_iterations && config.max_iterations > 0
        ? config.max_iterations
        : 20;
    this.maxNudges =
      config.max_nudges && config.max_nudges > 0 ? config.max_nudges : 3;
  }

  async run(
    system_prompt: string,
    user_message: string,
    history: Message[] = [],
    session_id: string = ""
  ): Promise<AgentResult> {
    // Cross-language contract: the system prompt leads the conversation,
    // ahead of any caller-supplied history (mirrors Go buildConversation).
    const conversation: Message[] = [
      { role: Role.SYSTEM, content: system_prompt },
      ...history,
    ];
    conversation.push({ role: Role.USER, content: user_message });

    const toolDefs = registryToolDefinitions(this.config.registry);

    let iterations = 0;
    let toolCallsMade = 0;
    let nudges = 0;
    let finalResponse = "";
    let reasoningTree: ContextTree | null = null;

    while (iterations < this.maxIterations) {
      iterations++;

      let resp: Response;
      try {
        resp = await this.config.backend.complete(conversation, toolDefs);
      } catch (err) {
        this.config.error_tracker?.recordError(
          session_id,
          "",
          {},
          toError(err),
          ErrorCategory.UNKNOWN
        );
        return this.finish(
          finalResponse,
          iterations,
          toolCallsMade,
          nudges,
          AgentTerminationReason.ERROR,
          conversation,
          reasoningTree
        );
      }

      // AR-1 parity: normalize reasoning and strip it before validation.
      // Reasoning can arrive as a native reasoning_content field or as
      // embedded thinking tags in content; both are combined into a synthetic
      // structured input so the adapter sees the full picture. Malformed or
      // unbounded reasoning fails closed: this turn is treated as invalid and
      // retried rather than parsed partially.
      let turnTree: ContextTree | null = null;
      let cleanContent = resp.content;
      let reasoningFailed = false;
      if (resp.content || resp.reasoning) {
        const reasoningInput: Record<string, unknown> = {
          content: resp.content,
        };
        if (resp.reasoning) {
          reasoningInput["reasoning_content"] = resp.reasoning;
        }
        const adapter = this.config.reasoning ?? defaultAdapter;
        try {
          turnTree = adapter.extract(
            reasoningInput,
            this.config.backend.modelName(),
            "agent-loop"
          );
          cleanContent = adapter.strip(reasoningInput);
        } catch (e) {
          if (e instanceof ReasoningError) {
            reasoningFailed = true;
            // Fail closed: never echo content that may carry reasoning we
            // could not parse.
            cleanContent = "";
          } else {
            throw e;
          }
        }
      }
      // Keep the last turn that actually carried reasoning; a later plain
      // turn must not blank out the tree the consumer is after.
      if (!isContextTreeEmpty(turnTree)) {
        reasoningTree = turnTree;
      }

      const validation: ValidationResult = reasoningFailed
        ? { toolCalls: [], nudge: null, needsRetry: true }
        : this.validate(resp, cleanContent);

      if (validation.needsRetry) {
        if (nudges >= this.maxNudges) {
          return this.finish(
            cleanContent,
            iterations,
            toolCallsMade,
            nudges,
            AgentTerminationReason.NUDGES_EXHAUSTED,
            conversation,
            reasoningTree
          );
        }
        nudges++;
        conversation.push({
          role: Role.ASSISTANT,
          content: cleanContent,
          meta: { type: MessageType.TEXT_RESPONSE },
        });
        if (validation.nudge) {
          conversation.push({
            role: Role.USER,
            content: validation.nudge.content,
            meta: { type: nudgeMetaType(validation.nudge.kind) },
          });
        }
        continue;
      }

      if (validation.toolCalls.length === 0) {
        finalResponse = cleanContent;
        return this.finish(
          finalResponse,
          iterations,
          toolCallsMade,
          nudges,
          AgentTerminationReason.COMPLETE,
          conversation,
          reasoningTree
        );
      }

      let stepNudged = false;
      const nativeCalls = resp.tool_calls.length > 0 ? resp.tool_calls : null;
      if (nativeCalls) {
        conversation.push({ role: Role.ASSISTANT, content: cleanContent, tool_calls: nativeCalls });
      }
      const deferredNudges: Message[] = [];

      for (const [index, call] of validation.toolCalls.entries()) {
        const toolCallId = nativeCalls?.[index]?.id;
        if (this.config.step_enforcer) {
          const [allowed, missing] = this.config.step_enforcer.canExecute(
            session_id,
            call.tool
          );
          if (!allowed) {
            if (nudges >= this.maxNudges) {
              return this.finish(
                cleanContent,
                iterations,
                toolCallsMade,
                nudges,
                AgentTerminationReason.NUDGES_EXHAUSTED,
                conversation,
                reasoningTree
              );
            }
            nudges++;
            stepNudged = true;
            const nudge: Nudge = stepNudge(
              call.tool,
              missing,
              Math.min(3, nudges)
            );
            if (nativeCalls) {
              conversation.push({ role: Role.TOOL, tool_call_id: toolCallId,
                content: `Tool ${call.tool} error: prerequisites not met`,
                meta: { type: MessageType.TOOL_RESULT } });
            }
            (nativeCalls ? deferredNudges : conversation).push({
              role: Role.USER,
              content: nudge.content,
              meta: { type: MessageType.STEP_NUDGE },
            });
            continue;
          }
        }

        if (
          this.config.error_tracker &&
          this.config.error_tracker.shouldBlockTool(session_id, call.tool)
        ) {
          conversation.push({
            role: Role.TOOL,
            ...(nativeCalls ? { tool_call_id: toolCallId } : {}),
            content: `Tool ${call.tool} error: blocked after repeated errors`,
            meta: { type: MessageType.TOOL_RESULT },
          });
          continue;
        }

        const tool = this.config.registry.get(call.tool);
        if (!tool) {
          conversation.push({
            role: Role.TOOL,
            ...(nativeCalls ? { tool_call_id: toolCallId } : {}),
            content: `Tool ${call.tool} error: unknown tool`,
            meta: { type: MessageType.TOOL_RESULT },
          });
          continue;
        }

        toolCallsMade++;

        let result: Result;
        try {
          result = await executeTool(tool, call.args);
        } catch (err) {
          result = new Result(false, null, toError(err).message);
        }

        if (result.success) {
          this.config.step_enforcer?.markStepComplete(session_id, call.tool);
          conversation.push({
            role: Role.TOOL,
            ...(nativeCalls ? { tool_call_id: toolCallId } : {}),
            content: `Tool ${call.tool} result: ${JSON.stringify(result.data)}`,
            meta: { type: MessageType.TOOL_RESULT },
          });
        } else {
          const message = result.error ?? "tool failed";
          this.config.error_tracker?.recordError(
            session_id,
            call.tool,
            call.args,
            new Error(message),
            categorizeError(message)
          );
          conversation.push({
            role: Role.TOOL,
            ...(nativeCalls ? { tool_call_id: toolCallId } : {}),
            content: `Tool ${call.tool} error: ${message}`,
            meta: { type: MessageType.TOOL_RESULT },
          });
        }
      }

      conversation.push(...deferredNudges);

      if (stepNudged) {
        continue;
      }
    }

    return this.finish(
      finalResponse,
      iterations,
      toolCallsMade,
      nudges,
      AgentTerminationReason.MAX_ITERATIONS,
      conversation,
      reasoningTree
    );
  }

  private validate(resp: Response, cleanContent: string): ValidationResult {
    if (resp.tool_calls.length > 0) {
      return this.config.validator.validateToolCalls(
        resp.tool_calls.map((tc) => ({ tool: tc.name, args: tc.arguments }))
      );
    }
    const textValidation =
      this.config.validator.validateTextResponse(cleanContent);
    if (textValidation.toolCalls.length > 0) {
      return textValidation;
    }
    if (this.config.require_tool_call) {
      return textValidation;
    }
    return { toolCalls: [], nudge: null, needsRetry: false };
  }

  private finish(
    finalResponse: string,
    iterations: number,
    toolCallsMade: number,
    nudges: number,
    termination: AgentTerminationReason,
    conversation: Message[],
    reasoningTree: ContextTree | null
  ): AgentResult {
    return {
      final_response: finalResponse,
      iterations,
      tool_calls_made: toolCallsMade,
      nudges,
      termination_reason: termination,
      conversation,
      reasoning_tree: reasoningTree,
    };
  }
}
