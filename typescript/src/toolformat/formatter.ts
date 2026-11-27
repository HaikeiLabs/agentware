import type { AnyTool, Result } from "../tools/index.js";
import type { ToolSpec } from "../toolspec/index.js";
import type { ToolDefinition } from "../llm/request.js";

export interface ParsedToolCall {
  id: string;
  name: string;
  args: Record<string, unknown>;
  raw: string;
}

export interface ToolFormatter {
  formatToolDefinitions(tools: Array<AnyTool | ToolSpec | ToolDefinition>): string;
  parseToolCalls(response: string): ParsedToolCall[];
  /**
   * Render a tool result for the next model turn. `callId` is the id of the
   * tool call this result answers, when the caller has one. Families whose
   * result format carries no id ignore it; AnthropicFormatter renders it as
   * `tool_use_id`.
   */
  formatToolResult(name: string, result: Result, callId?: string): string;
  modelFamily(): string;
  validateFormat(response: string): string | null;
}

type FormatterTool = AnyTool | ToolSpec | ToolDefinition;

/**
 * Renders a successful tool result's data as model-facing text, with one rule
 * for every formatter: a string is returned verbatim; null and undefined are
 * the empty string; objects, arrays, numbers and booleans are compact JSON in
 * key insertion order. A value JSON cannot encode falls back to String().
 */
function resultText(data: unknown): string {
  if (data === null || data === undefined) return "";
  if (typeof data === "string") return data;
  try {
    return JSON.stringify(data) ?? String(data);
  } catch {
    return String(data);
  }
}

const qwenToolCallRegex = /<tool_call>\s*<tool name="([^"]+)">\s*([\s\S]*?)\s*<\/tool>\s*<\/tool_call>/g;

const anthropicInvokeRegex = /<invoke tool="([^"]+)">([\s\S]*?)<\/invoke>/g;

export class GenericFormatter implements ToolFormatter {
  formatToolDefinitions(tools: FormatterTool[]): string {
    if (tools.length === 0) return "";

    const schemas: Record<string, unknown>[] = [];
    for (const tool of tools) {
      const func: Record<string, unknown> = {
        name: tool.name,
        description: tool.description,
      };
      if ("parameters" in tool) {
        func.parameters = sortedJsonValue(tool.parameters);
      } else if ("input_schema" in tool) {
        func.parameters = sortedJsonValue(tool.input_schema);
      } else if ("inputSchema" in tool && typeof (tool as Record<string, unknown>).inputSchema === "function") {
        const schema = (tool as any).inputSchema();
        if (schema) {
          func.parameters = schema;
        }
      }
      schemas.push({ type: "function", function: func });
    }
    return JSON.stringify(schemas);
  }

  parseToolCalls(response: string): ParsedToolCall[] {
    if (!response) return [];

    let parsed: any[];
    try {
      parsed = JSON.parse(response);
    } catch {
      return [];
    }
    if (!Array.isArray(parsed)) return [];

    const result: ParsedToolCall[] = [];
    for (const c of parsed) {
      if (!c || !c.name) continue;
      let args: Record<string, unknown> = {};
      if (typeof c.arguments === "string") {
        try {
          args = JSON.parse(c.arguments);
        } catch {
          args = {};
        }
      } else if (typeof c.arguments === "object" && c.arguments !== null) {
        args = c.arguments as Record<string, unknown>;
      }
      result.push({
        id: typeof c.id === "string" ? c.id : "",
        name: c.name,
        args,
        raw: response,
      });
    }
    return result;
  }

  formatToolResult(_name: string, result: Result, _callId?: string): string {
    if (result.success) {
      return resultText(result.data);
    }
    return `Error: ${result.error}`;
  }

  modelFamily(): string {
    return "generic";
  }

  validateFormat(response: string): string | null {
    if (!response) return null;

    let parsed: any[];
    try {
      parsed = JSON.parse(response);
    } catch {
      return "invalid JSON format";
    }
    if (!Array.isArray(parsed)) {
      return "expected JSON array";
    }

    for (const c of parsed) {
      if (!c || !c.name) {
        return "tool call missing 'name' field";
      }
      if (c.arguments === undefined || c.arguments === null) {
        return `tool call "${c.name}" missing 'arguments'`;
      }
    }
    return null;
  }
}

export class OpenAIFOrmatter extends GenericFormatter {
  modelFamily(): string {
    return "openai";
  }
}

export class DeepSeekFormatter extends GenericFormatter {
  modelFamily(): string {
    return "deepseek";
  }
}

export class GLMFormatter extends GenericFormatter {
  modelFamily(): string {
    return "glm";
  }
}

/**
 * Anthropic Claude tool use, rendered as text.
 *
 * Definitions are Anthropic tool definitions: `{name, description,
 * input_schema}`. Calls are Anthropic `tool_use` blocks, `{type: "tool_use",
 * id, name, input}`, given as one block or as a content array (its other
 * blocks, such as text, are ignored); each call keeps the block's id. When the
 * response holds no `tool_use` block, the legacy
 * `<function_calls><invoke tool="name">{json}</invoke></function_calls>` form
 * is parsed instead, and those calls have no id. Results are `tool_result`
 * blocks, `{type: "tool_result", tool_use_id, content, is_error}`. Without a
 * call id, `tool_name` takes the place of `tool_use_id`; that block is a text
 * rendering only, not a valid Messages API block.
 *
 * See https://platform.claude.com/docs/en/agents-and-tools/tool-use/overview
 */
export class AnthropicFormatter implements ToolFormatter {
  formatToolDefinitions(tools: FormatterTool[]): string {
    if (tools.length === 0) return "";

    const definitions = tools.map((tool) => {
      let schema: unknown;
      if ("parameters" in tool) {
        schema = sortedJsonValue(tool.parameters);
      } else if ("input_schema" in tool) {
        schema = sortedJsonValue(tool.input_schema);
      } else if ("inputSchema" in tool && typeof (tool as Record<string, unknown>).inputSchema === "function") {
        schema = (tool as { inputSchema: () => unknown }).inputSchema();
      }
      return {
        name: tool.name,
        description: tool.description,
        // The Messages API requires input_schema; a tool without parameters
        // takes an empty object schema.
        input_schema: schema ?? { type: "object", properties: {} },
      };
    });
    return JSON.stringify(definitions);
  }

  parseToolCalls(response: string): ParsedToolCall[] {
    if (!response) return [];

    const blocks = anthropicToolUseBlocks(response);
    if (blocks.length === 0) return this.parseInvoke(response);

    // Fail closed: a malformed tool_use block is never returned as a call
    // (validateFormat names the problem).
    const result: ParsedToolCall[] = [];
    for (const block of blocks) {
      if (anthropicToolUseError(block) !== null) continue;
      result.push({
        id: block.id as string,
        name: block.name as string,
        args: block.input as Record<string, unknown>,
        raw: response,
      });
    }
    return result;
  }

  /** Parse the legacy <function_calls><invoke> form; those calls carry no id. */
  private parseInvoke(response: string): ParsedToolCall[] {
    const matches = response.matchAll(anthropicInvokeRegex);
    const result: ParsedToolCall[] = [];
    for (const m of matches) {
      if (m.length < 3) continue;
      const name = m[1];
      const argsRaw = m[2];
      let args: Record<string, unknown>;
      try {
        args = JSON.parse(argsRaw);
      } catch {
        args = { _raw: argsRaw };
      }
      result.push({
        id: "",
        name,
        args,
        raw: m[0],
      });
    }
    return result;
  }

  formatToolResult(name: string, result: Result, callId?: string): string {
    const block: Record<string, unknown> = { type: "tool_result" };
    if (callId) {
      block.tool_use_id = callId;
    } else {
      block.tool_name = name;
    }
    if (result.success) {
      block.content = resultText(result.data);
    } else {
      block.content = result.error ?? "";
      block.is_error = true;
    }
    return JSON.stringify(block);
  }

  modelFamily(): string {
    return "anthropic";
  }

  validateFormat(response: string): string | null {
    if (!response) return null;

    const blocks = anthropicToolUseBlocks(response);
    if (blocks.length === 0) return this.validateInvoke(response);

    const seen = new Set<string>();
    for (const block of blocks) {
      const error = anthropicToolUseError(block);
      if (error !== null) return error;
      const id = block.id as string;
      if (seen.has(id)) return `duplicate tool_use id "${id}"`;
      seen.add(id);
    }
    return null;
  }

  /** Validate the legacy <function_calls><invoke> form. */
  private validateInvoke(response: string): string | null {
    const matches = response.matchAll(anthropicInvokeRegex);
    let found = false;
    for (const m of matches) {
      found = true;
      if (m.length < 2 || !m[1]) {
        return "tool call missing 'tool' attribute";
      }
      const argsRaw = m[2];
      if (!argsRaw) {
        return `tool call "${m[1]}" missing arguments`;
      }
      try {
        JSON.parse(argsRaw);
      } catch {
        return `tool call "${m[1]}" has invalid JSON arguments`;
      }
    }
    if (!found && response.includes("<invoke")) {
      return "malformed tool call: missing tool attribute";
    }
    return null;
  }
}

/** The tool_use blocks of a response that is a JSON block or a JSON content array. */
function anthropicToolUseBlocks(response: string): Array<Record<string, unknown>> {
  const text = response.trim();
  if (!text.startsWith("{") && !text.startsWith("[")) return [];
  let value: unknown;
  try {
    value = JSON.parse(text);
  } catch {
    return [];
  }
  const items = Array.isArray(value) ? value : [value];
  return items.filter(
    (item): item is Record<string, unknown> =>
      item !== null && typeof item === "object" && !Array.isArray(item) && (item as Record<string, unknown>).type === "tool_use",
  );
}

/** Why a tool_use block is malformed, or null when it is valid. */
function anthropicToolUseError(block: Record<string, unknown>): string | null {
  const name = block.name;
  if (typeof name !== "string" || name === "") return "tool_use block missing name";
  if (typeof block.id !== "string" || block.id === "") return `tool_use block "${name}" missing id`;
  const input = block.input;
  if (input === null || typeof input !== "object" || Array.isArray(input)) {
    return `tool_use block "${name}" input must be a JSON object`;
  }
  return null;
}

export class QwenFormatter implements ToolFormatter {
  formatToolDefinitions(tools: FormatterTool[]): string {
    if (!tools.length) return "";
    const lines = tools.map((tool) => {
      const fn: Record<string, unknown> = { name: tool.name, description: tool.description };
      if ("parameters" in tool) fn.parameters = tool.parameters;
      else if ("input_schema" in tool) fn.parameters = tool.input_schema;
      else if ("inputSchema" in tool && typeof (tool as Record<string, unknown>).inputSchema === "function") {
        const schema = (tool as any).inputSchema();
        if (schema) fn.parameters = schema;
      }
      return JSON.stringify(sortedJsonValue({ type: "function", function: fn }));
    });
    return "# Tools\n\nYou may call one or more functions to assist with the user query.\n\n" +
      "You are provided with function signatures within <tools></tools> XML tags:\n<tools>\n" + lines.join("\n") +
      "\n</tools>\n\nFor each function call, return a json object with function name and arguments within <tool_call></tool_call> XML tags:\n" +
      '<tool_call>\n{"name": <function-name>, "arguments": <args-json-object>}\n</tool_call>';
  }

  private decode(body: string): { name: string; args: Record<string, unknown> } | null {
    try {
      const call = JSON.parse(body);
      if (!call || typeof call.name !== "string" || !call.name) return null;
      const args = typeof call.arguments === "string" ? JSON.parse(call.arguments) : call.arguments;
      if (!args || typeof args !== "object" || Array.isArray(args)) return null;
      return { name: call.name, args };
    } catch { return null; }
  }

  parseToolCalls(response: string): ParsedToolCall[] {
    const spans = [...response.matchAll(/<tool_call>([\s\S]*?)<\/tool_call>/g)];
    const hermes = spans.some((m) => !m[1].trimStart().startsWith("<tool"));
    const result: ParsedToolCall[] = [];
    if (hermes) {
      for (const span of spans) {
        if (span[1].trimStart().startsWith("<tool")) continue;
        const call = this.decode(span[1]);
        if (call) result.push({ id: "", ...call, raw: span[0] });
      }
    } else {
      for (const m of response.matchAll(qwenToolCallRegex)) {
        try {
          const args = m[2].trim() ? JSON.parse(m[2]) : {};
          if (args && typeof args === "object" && !Array.isArray(args)) result.push({ id: "", name: m[1], args, raw: m[0] });
        } catch { /* skip malformed legacy calls */ }
      }
    }
    return result;
  }

  formatToolResult(_name: string, result: Result, _callId?: string): string {
    // The official Qwen chat template emits a tool message's content verbatim
    // between the tool_response tags, so a string is not quoted.
    const text = result.success ? resultText(result.data) : JSON.stringify({ error: result.error });
    return `<tool_response>\n${text}\n</tool_response>`;
  }

  modelFamily(): string { return "qwen"; }

  validateFormat(response: string): string | null {
    const spans = [...response.matchAll(/<tool_call>([\s\S]*?)<\/tool_call>/g)];
    const hermes = spans.some((m) => !m[1].trimStart().startsWith("<tool"));
    if (hermes) {
      for (const m of spans) if (!m[1].trimStart().startsWith("<tool") && !this.decode(m[1])) return "invalid JSON tool call or arguments";
    } else {
      for (const span of spans) {
        const legacy = [...span[0].matchAll(qwenToolCallRegex)][0];
        if (!legacy) return "tool call missing function name";
        try {
          const args = JSON.parse(legacy[2]);
          if (!args || typeof args !== "object" || Array.isArray(args)) return "invalid JSON arguments";
        } catch { return "invalid JSON arguments"; }
      }
    }
    return null;
  }
}

export type MiniMaxVersion = "m2" | "m3";

/** The marker MiniMax M3 puts before every structural tag of a tool call. */
export const MINIMAX_M3_NAMESPACE = "]<]minimax[>[";
const MM3_TOOL_CALL = `${MINIMAX_M3_NAMESPACE}<tool_call>`;
const MM3_INVOKE = `${MINIMAX_M3_NAMESPACE}<invoke name=`;
const MM3_INVOKE_END = `${MINIMAX_M3_NAMESPACE}</invoke>`;
const MM3_ELEMENT = `${MINIMAX_M3_NAMESPACE}<`;
const MM3_MAX_DEPTH = 128;
const MM3_MIXED_TEXT = "$text";
const MM2_BLOCK = "<minimax:tool_call>";
const MM2_INVOKE_OPEN = "<invoke name=";
const MM_TOOLS_HEADER =
  "# Tools\nYou may call one or more tools to assist with the user query.\n" +
  "Here are the tools available in JSONSchema format:\n\n<tools>\n";
const MM2_INSTRUCTIONS =
  "When making tool calls, use XML format to invoke tools and pass parameters:\n\n" +
  '<minimax:tool_call>\n<invoke name="tool-name-1">\n' +
  '<parameter name="param-key-1">param-value-1</parameter>\n' +
  '<parameter name="param-key-2">param-value-2</parameter>\n' +
  "...\n</invoke>\n</minimax:tool_call>";

function mm3Instructions(): string {
  const ns = MINIMAX_M3_NAMESPACE;
  return (
    `To call tools, wrap all invocations in a single ${ns}<tool_call>${ns}</tool_call> block. ` +
    "Parameter values containing nested objects or arrays are recursively expanded into XML elements. Example:\n\n" +
    `${ns}<tool_call>\n` +
    `${ns}<invoke name="tool-name-1">${ns}<param-1>value-1${ns}</param-1>${ns}<param-2>` +
    `${ns}<item>${ns}<key-a>val-a${ns}</key-a>${ns}<key-b>val-b${ns}</key-b>${ns}</item>` +
    `${ns}</param-2>${ns}</invoke>\n` +
    `${ns}<invoke name="tool-name-2">${ns}<param-1>value-1${ns}</param-1>${ns}</invoke>\n` +
    `${ns}</tool_call>`
  );
}

const mm2InvokeRegex = /<invoke name=(?:"([^"]*)"|'([^']*)'|([^>\s]*))>([\s\S]*?)<\/invoke>/g;
const mm2ParamOpenRegex = /<\s*parameter\s+name\s*=\s*(?:"([^"]*)"|'([^']*)'|([^>\s]+))\s*>/g;
const mm2ParamCloseRegex = /<\s*\/\s*parameter\s*>/;

/** Sets an own property, so a model-chosen name such as __proto__ stays data. */
function setOwn(target: Record<string, unknown>, key: string, value: unknown): void {
  Object.defineProperty(target, key, { value, enumerable: true, writable: true, configurable: true });
}

interface MiniMaxInvoke { name: string; args: Record<string, unknown>; raw: string; complete: boolean }

/**
 * Parses M2 parameter tags; `complete` is false if one was dropped
 * unterminated. A value runs to its closing tag or, as in vLLM, to the next
 * parameter tag. Values stay verbatim strings.
 */
function mm2Params(body: string): { params: Record<string, unknown>; complete: boolean } {
  const openers = [...body.matchAll(mm2ParamOpenRegex)];
  const params: Record<string, unknown> = {};
  let complete = true;
  openers.forEach((opener, i) => {
    const next = openers[i + 1];
    const region = body.slice(opener.index! + opener[0].length, next ? next.index : body.length);
    const close = mm2ParamCloseRegex.exec(region);
    let value: string;
    if (close) value = region.slice(0, close.index);
    else if (next) value = region;
    else { complete = false; return; }
    const name = (opener[1] || opener[2] || opener[3] || "").trim();
    if (name) setOwn(params, name, value);
  });
  return { params, complete };
}

/** An M3 element value: its text, or its [name, value] children in order. */
type MM3Node = string | Array<[string, MM3Node]>;

/** Parses the M3 element starting at `pos`; null if it is malformed. */
function mm3Element(text: string, pos: number, depth: number): [string, MM3Node, number] | null {
  if (depth > MM3_MAX_DEPTH) return null;
  const nameStart = pos + MM3_ELEMENT.length;
  const nameEnd = text.indexOf(">", nameStart);
  if (nameEnd <= nameStart) return null;
  const name = text.slice(nameStart, nameEnd);
  if (name.startsWith("/") || !name.trim()) return null;
  const closeTag = `${MINIMAX_M3_NAMESPACE}</${name}>`;
  let cursor = nameEnd + 1;
  const parts: string[] = [];
  const children: Array<[string, MM3Node]> = [];
  for (;;) {
    const marker = text.indexOf(MINIMAX_M3_NAMESPACE, cursor);
    if (marker < 0) return null;
    parts.push(text.slice(cursor, marker));
    if (text.startsWith(closeTag, marker)) { cursor = marker + closeTag.length; break; }
    if (!text.startsWith(MM3_ELEMENT, marker)) return null;
    const child = mm3Element(text, marker, depth + 1);
    if (!child) return null;
    children.push([child[0], child[1]]);
    cursor = child[2];
  }
  const bodyText = parts.join("");
  if (!children.length) return [name, bodyText, cursor];
  if (bodyText.trim()) {
    let field = MM3_MIXED_TEXT;
    while (children.some(([childName]) => childName === field)) field = "$" + field;
    children.push([field, bodyText]);
  }
  return [name, children, cursor];
}

/** Parses an M3 invoke body; `complete` is false if parsing stopped at a malformed element. */
function mm3Params(body: string): { params: Array<[string, MM3Node]>; complete: boolean } {
  const params: Array<[string, MM3Node]> = [];
  let pos = 0;
  for (;;) {
    while (pos < body.length && /\s/.test(body[pos])) pos++;
    if (pos === body.length) return { params, complete: true };
    if (!body.startsWith(MM3_ELEMENT, pos)) return { params, complete: false };
    const element = mm3Element(body, pos, 1);
    if (!element) return { params, complete: false };
    params.push([element[0], element[1]]);
    pos = element[2];
  }
}

/** Elements to an object without a schema; a repeated name collects a list. */
function mm3Object(elements: Array<[string, MM3Node]>): Record<string, unknown> {
  const result: Record<string, unknown> = {};
  const repeated = new Set<string>();
  for (const [name, node] of elements) {
    const value = typeof node === "string" ? node : mm3Object(node);
    if (!Object.prototype.hasOwnProperty.call(result, name)) setOwn(result, name, value);
    else if (repeated.has(name)) (result[name] as unknown[]).push(value);
    else { setOwn(result, name, [result[name], value]); repeated.add(name); }
  }
  return result;
}

function mm3Invokes(response: string): { invokes: MiniMaxInvoke[]; error: string | null } {
  const start = response.indexOf(MM3_TOOL_CALL);
  if (start < 0) {
    return { invokes: [], error: response.includes(MM3_INVOKE) ? "MiniMax M3 invoke outside a tool_call block" : null };
  }
  const invokes: MiniMaxInvoke[] = [];
  let pos = start + MM3_TOOL_CALL.length;
  for (;;) {
    const begin = response.indexOf(MM3_INVOKE, pos);
    if (begin < 0) break;
    let nameStart = begin + MM3_INVOKE.length;
    const quote = response[nameStart];
    let nameEnd: number;
    let bodyStart: number;
    if (quote === '"' || quote === "'") {
      nameStart += 1;
      nameEnd = response.indexOf(`${quote}>`, nameStart);
      bodyStart = nameEnd + 2;
    } else {
      nameEnd = response.indexOf(">", nameStart);
      bodyStart = nameEnd + 1;
    }
    if (nameEnd < 0) return { invokes, error: "unterminated MiniMax invoke" };
    const end = response.indexOf(MM3_INVOKE_END, bodyStart);
    if (end < 0) return { invokes, error: "unterminated MiniMax invoke" };
    const { params, complete } = mm3Params(response.slice(bodyStart, end));
    pos = end + MM3_INVOKE_END.length;
    invokes.push({ name: response.slice(nameStart, nameEnd).trim(), args: mm3Object(params), raw: response.slice(begin, pos), complete });
  }
  if (!invokes.length) return { invokes: [], error: "MiniMax tool_call block has no invoke" };
  return { invokes, error: null };
}

/** M2 invokes anywhere in the response, as vLLM finds them. */
function mm2Invokes(response: string): { invokes: MiniMaxInvoke[]; error: string | null } {
  const invokes = [...response.matchAll(mm2InvokeRegex)].map((m) => {
    const { params, complete } = mm2Params(m[4]);
    return { name: (m[1] || m[2] || m[3] || "").trim(), args: params, raw: m[0], complete };
  });
  if (response.split(MM2_INVOKE_OPEN).length - 1 !== invokes.length) return { invokes, error: "unterminated MiniMax invoke" };
  if (!invokes.length && response.includes(MM2_BLOCK)) return { invokes: [], error: "MiniMax tool_call block has no invoke" };
  return { invokes, error: null };
}

/** A response with the M3 namespace marker is M3; any other is M2. */
function miniMaxInvokes(response: string): { invokes: MiniMaxInvoke[]; error: string | null } {
  return response.includes(MINIMAX_M3_NAMESPACE) ? mm3Invokes(response) : mm2Invokes(response);
}

/**
 * MiniMax M2 and M3 tool calls, as the official chat templates render them.
 * `version` picks the tool-definition prompt (M2 parameter tags or M3
 * namespaced elements). Parsing and validation accept both syntaxes, and a
 * tool result is a response block in both. Parameter values are not coerced
 * to the tool schema: they arrive as strings (M3 nested elements as objects,
 * repeated names as lists), the same as vLLM's parsers without a request
 * schema.
 */
export class MiniMaxFormatter implements ToolFormatter {
  readonly version: MiniMaxVersion;

  constructor(options: { version?: MiniMaxVersion } = {}) {
    const version = options.version ?? "m2";
    if (version !== "m2" && version !== "m3") {
      throw new Error(`unknown MiniMax version ${JSON.stringify(version)}; expected "m2" or "m3"`);
    }
    this.version = version;
  }

  formatToolDefinitions(tools: FormatterTool[]): string {
    if (!tools.length) return "";
    const lines = tools.map((tool) => {
      const fn: Record<string, unknown> = { name: tool.name, description: tool.description };
      if ("parameters" in tool) fn.parameters = tool.parameters;
      else if ("input_schema" in tool) fn.parameters = tool.input_schema;
      else if ("inputSchema" in tool && typeof (tool as Record<string, unknown>).inputSchema === "function") {
        const schema = (tool as any).inputSchema();
        if (schema) fn.parameters = schema;
      }
      if (fn.parameters === undefined || fn.parameters === null) delete fn.parameters;
      return `<tool>${JSON.stringify(sortedJsonValue(fn))}</tool>`;
    });
    const instructions = this.version === "m3" ? mm3Instructions() : MM2_INSTRUCTIONS;
    return MM_TOOLS_HEADER + lines.join("\n") + "\n</tools>\n\n" + instructions;
  }

  parseToolCalls(response: string): ParsedToolCall[] {
    return miniMaxInvokes(response).invokes
      .filter((invoke) => invoke.name)
      .map((invoke) => ({ id: "", name: invoke.name, args: invoke.args, raw: invoke.raw }));
  }

  formatToolResult(_name: string, result: Result, _callId?: string): string {
    // Both chat templates emit a tool message's content verbatim between the
    // response tags, so a string is not quoted.
    const text = result.success ? resultText(result.data) : JSON.stringify({ error: result.error });
    return `<response>${text}</response>`;
  }

  modelFamily(): string { return "minimax"; }

  validateFormat(response: string): string | null {
    const { invokes, error } = miniMaxInvokes(response);
    if (error) return error;
    for (const invoke of invokes) {
      if (!invoke.name) return "MiniMax invoke missing tool name";
      if (!invoke.complete) return `MiniMax invoke "${invoke.name}" has a malformed or unterminated parameter`;
    }
    return null;
  }
}

function sortedJsonValue(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(sortedJsonValue);
  if (value !== null && typeof value === "object") {
    return Object.fromEntries(
      Object.keys(value).sort().map((key) => [key, sortedJsonValue((value as Record<string, unknown>)[key])]),
    );
  }
  return value;
}

export function getFormatter(modelName: string): ToolFormatter {
  const lower = modelName.toLowerCase();
  if (lower.includes("minimax-m3")) return new MiniMaxFormatter({ version: "m3" });
  if (lower.includes("minimax-m2")) return new MiniMaxFormatter({ version: "m2" });
  if (lower.includes("anthropic") || lower.includes("claude")) return new AnthropicFormatter();
  if (lower.includes("qwen") || lower.includes("qwq")) return new QwenFormatter();
  if (lower.includes("deepseek")) return new DeepSeekFormatter();
  if (lower.includes("glm") || lower.includes("chatglm")) return new GLMFormatter();
  if (lower.includes("gpt") || lower.includes("o1") || lower.includes("o3")) return new OpenAIFOrmatter();
  return new GenericFormatter();
}
