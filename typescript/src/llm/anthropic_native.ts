/** Native Anthropic content-block formatting. Signed blocks are opaque. */
import { Message, Role, ToolDefinition } from "./request.js";
import { Response, ToolCall } from "./response.js";

type Block = Record<string, unknown>;
type AnthropicMetadata = { anthropic: { before: Block[]; after: Block[] } };

/** Decode Anthropic's content blocks into Agentware calls. */
export function parseAnthropicResponse(data: {
  content?: Block[];
  stop_reason?: string;
}): Response {
  const calls: ToolCall[] = [];
  let pending: Block[] = [];
  let text = "";
  for (const block of data.content ?? []) {
    if (block.type === "tool_use") {
      if (
        typeof block.id !== "string" ||
        !block.id ||
        typeof block.name !== "string" ||
        !block.name ||
        !block.input ||
        typeof block.input !== "object" ||
        Array.isArray(block.input)
      )
        throw new Error("invalid Anthropic tool_use");
      const call: ToolCall = {
        id: block.id,
        name: block.name,
        arguments: structuredClone(block.input) as Record<string, unknown>,
      };
      if (pending.length)
        call.metadata = {
          anthropic: { before: pending, after: [] },
        } satisfies AnthropicMetadata;
      calls.push(call);
      pending = [];
    } else {
      if (block.type === "text" && typeof block.text === "string")
        text += block.text;
      pending.push(structuredClone(block));
    }
  }
  if (calls.length && pending.length) {
    const last = calls[calls.length - 1];
    const meta = (last.metadata ?? {
      anthropic: { before: [], after: [] },
    }) as AnthropicMetadata;
    meta.anthropic.after = pending;
    last.metadata = meta;
  }
  return {
    content: text,
    reasoning: "",
    tool_calls: calls,
    finish_reason: data.stop_reason ?? "",
    usage_tokens: { prompt_tokens: 0, completion_tokens: 0, total_tokens: 0 },
  };
}

/** Encode definitions for the Anthropic tools parameter. */
export function formatAnthropicTools(tools: ToolDefinition[]): Block[] {
  return tools.map((tool) => ({
    name: tool.name,
    description: tool.description,
    input_schema:
      tool.input_schema && Object.keys(tool.input_schema).length
        ? tool.input_schema
        : { type: "object", properties: {} },
  }));
}

/** Encode assistant calls and group adjacent tool results into a user turn.
 * System instructions belong in the top-level `system` parameter. */
export function formatAnthropicMessages(messages: Message[]): Block[] {
  const out: Block[] = [];
  for (const message of messages) {
    if (message.role === Role.SYSTEM)
      throw new Error("Anthropic system instructions must be top-level");
    if (message.role === Role.TOOL) {
      if (!message.tool_call_id)
        throw new Error("tool_result requires tool_call_id");
      const block = {
        type: "tool_result",
        tool_use_id: message.tool_call_id,
        content: message.content,
      };
      const prev = out[out.length - 1];
      if (
        prev?.role === "user" &&
        Array.isArray(prev.content) &&
        prev.content.every((v: Block) => v.type === "tool_result")
      )
        (prev.content as Block[]).push(block);
      else out.push({ role: "user", content: [block] });
      continue;
    }
    if (message.role === Role.ASSISTANT && message.tool_calls?.length) {
      const blocks: Block[] = [];
      const hasSavedBlocks = message.tool_calls.some(
        (call) =>
          (call.metadata as AnthropicMetadata | undefined)?.anthropic !==
          undefined,
      );
      if (!hasSavedBlocks && message.content)
        blocks.push({ type: "text", text: message.content });
      for (const call of message.tool_calls) {
        const saved = (call.metadata as AnthropicMetadata | undefined)
          ?.anthropic;
        if (saved) blocks.push(...saved.before);
        blocks.push({
          type: "tool_use",
          id: call.id,
          name: call.name,
          input: call.arguments,
        });
        if (saved) blocks.push(...saved.after);
      }
      out.push({ role: "assistant", content: blocks });
    } else out.push({ role: message.role, content: message.content });
  }
  return out;
}
