import { readFileSync } from "node:fs";
import {
  formatAnthropicMessages,
  formatAnthropicTools,
  parseAnthropicResponse,
  Role,
} from "../src/llm/index.js";

const cases = JSON.parse(
  readFileSync("../fixtures/llm/anthropic-native/cases.json", "utf8"),
) as Array<{
  name: string;
  response: { content: Array<Record<string, unknown>>; stop_reason: string };
  tools: Array<{
    name: string;
    description: string;
    input_schema: Record<string, unknown>;
  }>;
  expected_tools: unknown;
  expected_messages: unknown;
}>;
test.each(cases)("$name native round-trip", (fixture) => {
  const parsed = parseAnthropicResponse(fixture.response);
  expect(formatAnthropicTools(fixture.tools)).toEqual(fixture.expected_tools);
  if (fixture.name === "no thinking")
    expect(parsed.tool_calls[0].metadata).toBeUndefined();
  else expect(parsed.tool_calls[0].metadata).toHaveProperty("anthropic.before");
  const messages = formatAnthropicMessages([
    {
      role: Role.ASSISTANT,
      content: parsed.content,
      tool_calls: parsed.tool_calls,
    },
    ...parsed.tool_calls.map((call) => ({
      role: Role.TOOL,
      tool_call_id: call.id,
      content: "done",
    })),
  ]);
  expect(messages).toEqual(fixture.expected_messages);
});
