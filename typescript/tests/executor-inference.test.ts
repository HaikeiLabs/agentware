import type { Message, ToolDefinition } from "../src/llm/request.js";
import { InferenceExecutor } from "../src/executor/executor.js";
import { ToolRegistry, Result } from "../src/tools/index.js";
import type { CallerContext } from "../src/middleware/index.js";
import type { ToolFormatter } from "../src/toolformat/index.js";

it("sends registry tool definitions to the backend", () => {
  const schema = { type: "object", properties: { query: { type: "string" } } };
  const registry = new ToolRegistry();
  registry.register({ name: "lookup", description: "Look up data", inputSchema: () => schema,
    execute: () => new Result(true) } as Parameters<ToolRegistry["register"]>[0]);
  const calls: (ToolDefinition[] | undefined)[] = [];
  const backend = {
    complete: (_messages: Message[], tools?: ToolDefinition[]) => {
      calls.push(tools);
      return { content: "done", reasoning: "", tool_calls: [], finish_reason: "stop",
        usage_tokens: { prompt_tokens: 0, completion_tokens: 0, total_tokens: 0 } };
    },
    supportsNativeToolCalling: () => false,
    modelName: () => "mock",
    contextWindowSize: () => 0,
  };
  const executor = new InferenceExecutor({ backend, registry, tool_executor: {},
    formatter: {} as ToolFormatter, max_iterations: 2, completion_signal: "done" });
  executor.execute({ system_prompt: "", user_message: "go", history: [], caller_ctx: {} as CallerContext });
  expect(calls).toEqual([[{ name: "lookup", description: "Look up data", input_schema: schema }]]);
});
