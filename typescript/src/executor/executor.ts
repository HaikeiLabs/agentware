import type { Backend, Message } from "../llm/index.js";
// eslint-disable-next-line @typescript-eslint/no-unused-vars
import type { Response, ToolCall } from "../llm/response.js";
import { Role } from "../llm/index.js";
import type { CallerContext } from "../middleware/index.js";
import type { ToolRegistry } from "../tools/index.js";
import type { ToolFormatter } from "../toolformat/index.js";

export enum TerminationReason {
  COMPLETE = "complete",
  MAX_ITERATIONS = "max_iterations",
  ERROR = "error",
  CANCELED = "canceled",
}

export interface ExecuteRequest {
  system_prompt: string;
  user_message: string;
  history: Message[];
  max_iterations?: number;
  caller_ctx: CallerContext;
  job_id?: string;
}

export interface ExecuteResult {
  final_response: string;
  iterations: number;
  tool_calls_made: number;
  termination_reason: TerminationReason;
  conversation: Message[];
}

export interface Executor {
  execute(req: ExecuteRequest): ExecuteResult;
}

export interface InferenceExecutorConfig {
  backend: Backend;
  registry: ToolRegistry;
  tool_executor: unknown;
  formatter: ToolFormatter;
  max_iterations: number;
  completion_signal: string;
}

export class InferenceExecutor implements Executor {
  private config: InferenceExecutorConfig;

  constructor(config: InferenceExecutorConfig) {
    this.config = {
      ...config,
      max_iterations: config.max_iterations || 20,
      completion_signal: config.completion_signal || "TASK_COMPLETE",
    };
  }

  execute(req: ExecuteRequest): ExecuteResult {
    // Cross-language contract: the system prompt leads the conversation,
    // ahead of any caller-supplied history (mirrors Go buildConversation).
    const conversation: Message[] = [
      { role: Role.SYSTEM, content: req.system_prompt },
      ...req.history,
    ];
    conversation.push({ role: Role.USER, content: req.user_message });

    let iterations = 0;
    let tool_calls_made = 0;
    let final_response = "";

    const max_iters = req.max_iterations || this.config.max_iterations;

    while (iterations < max_iters) {
      const response = this.config.backend.complete(conversation);

      if (!response.tool_calls || response.tool_calls.length === 0) {
        final_response = response.content;
        break;
      }

      conversation.push({ role: Role.ASSISTANT, content: response.content, tool_calls: response.tool_calls });
      tool_calls_made += response.tool_calls.length;

      for (const tool_call of response.tool_calls) {
        const [result, success, error] = (this.config.tool_executor as {
          execute: (
            toolName: string,
            args: Record<string, unknown>,
            caller: CallerContext
          ) => [unknown, boolean, string];
        }).execute(tool_call.name, tool_call.arguments, req.caller_ctx);

        conversation.push({
          role: Role.TOOL,
          content: success
            ? `Tool ${tool_call.name} result: ${JSON.stringify(result)}`
            : `Tool ${tool_call.name} error: ${error}`,
          tool_call_id: tool_call.id,
        });
      }

      iterations++;
    }

    let termination: TerminationReason;
    if (iterations >= max_iters) {
      termination = TerminationReason.MAX_ITERATIONS;
    } else if (final_response) {
      termination = TerminationReason.COMPLETE;
    } else {
      termination = TerminationReason.ERROR;
    }

    return {
      final_response,
      iterations,
      tool_calls_made,
      termination_reason: termination,
      conversation,
    };
  }
}