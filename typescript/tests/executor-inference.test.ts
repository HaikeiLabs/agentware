import type { Message, ToolDefinition } from "../src/llm/request.js";
import { InferenceExecutor } from "../src/executor/executor.js";
import { ToolRegistry, Result } from "../src/tools/index.js";
import type { CallerContext } from "../src/middleware/index.js";
import type { ToolFormatter } from "../src/toolformat/index.js";
import { AnthropicFormatter, GenericFormatter, QwenFormatter } from "../src/toolformat/index.js";

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

describe("InferenceExecutor tool messages", () => {
  function toolMessage(formatter: ToolFormatter | undefined, outcome: [unknown, boolean, string]): Message {
    const calls: Message[][] = [];
    let turn = 0;
    const backend = {
      complete: (messages: Message[]) => {
        calls.push([...messages]);
        const first = turn++ === 0;
        return { content: first ? "" : "done", reasoning: "",
          tool_calls: first ? [{ id: "call_1", name: "lookup", arguments: { q: "x" } }] : [],
          finish_reason: first ? "tool_calls" : "stop",
          usage_tokens: { prompt_tokens: 0, completion_tokens: 0, total_tokens: 0 } };
      },
      supportsNativeToolCalling: () => true,
      modelName: () => "mock",
      contextWindowSize: () => 0,
    };
    const executor = new InferenceExecutor({ backend, registry: new ToolRegistry(),
      tool_executor: { execute: () => outcome }, formatter, max_iterations: 3, completion_signal: "done" });
    executor.execute({ system_prompt: "sys", user_message: "go", history: [], caller_ctx: {} as CallerContext });
    return calls[1][3];
  }

  it("sends the configured formatter's formatToolResult (mirrors Go)", () => {
    const formatter = new QwenFormatter();
    const message = toolMessage(formatter, ["found it", true, ""]);
    expect(message.tool_call_id).toBe("call_1");
    expect(message.content).toBe(formatter.formatToolResult("lookup", new Result(true, "found it"), "call_1"));
  });

  it("passes the tool call id to the formatter", () => {
    const message = toolMessage(new AnthropicFormatter(), ["found it", true, ""]);
    expect(JSON.parse(message.content)).toEqual({ type: "tool_result", tool_use_id: "call_1", content: "found it" });
  });

  it("formats a failed tool call through the formatter", () => {
    expect(toolMessage(new GenericFormatter(), [null, false, "rate limited"]).content).toBe("Error: rate limited");
  });

  it("keeps the plain text tool message with no formatter", () => {
    expect(toolMessage(undefined, ["found it", true, ""]).content).toBe('Tool lookup result: "found it"');
    expect(toolMessage(undefined, [null, false, "rate limited"]).content).toBe("Tool lookup error: rate limited");
  });
});
