/**
 * GPT-OSS Harmony format: render prompts and parse raw completions.
 *
 * GPT-OSS models are trained on the Harmony format. Over a chat-completions
 * API the server applies it; over raw `/v1/completions` the caller renders
 * the whole prompt and parses the completion text. {@link HarmonyFormatter}
 * is the {@link ToolFormatter} for the family; {@link renderHarmonyPrompt}
 * renders a whole conversation; {@link parseHarmonyCompletion} splits
 * completion text into messages, with the analysis channel as the reasoning
 * and the final channel as the answer.
 *
 * Harmony is opt-in: `getFormatter` never returns {@link HarmonyFormatter},
 * so chat-completions callers keep their formatter; raw-completions callers
 * construct it themselves.
 *
 * The rendering follows the reference renderer in openai/harmony
 * (`src/encoding.rs`), including its JSON Schema to TypeScript mapping and
 * its schema key order. Mirrors `python/.../toolformat/harmony.py`; the shared
 * fixture `fixtures/toolformat/harmony-cases.json` replays openai/harmony's
 * test-data.
 */
import type { AnyTool, Result } from "../tools/index.js";
import type { ToolSpec } from "../toolspec/index.js";
import type { ToolDefinition } from "../llm/request.js";
import { resultText, type ParsedToolCall, type ToolFormatter } from "./formatter.js";

type HarmonyTool = AnyTool | ToolSpec | ToolDefinition;

const START = "<|start|>";
const END = "<|end|>";
const MESSAGE = "<|message|>";
const CHANNEL = "<|channel|>";
const CONSTRAIN = "<|constrain|>";
const CALL = "<|call|>";
const RETURN = "<|return|>";
const TERMINATORS = [END, CALL, RETURN];
const FUNCTIONS_PREFIX = "functions.";
const ROLES = new Set(["system", "developer", "user", "assistant", "tool"]);
const EFFORTS = new Set(["", "low", "medium", "high"]);

/** The content type of a JSON tool call: `<|constrain|>json`. */
export const HARMONY_JSON_CONTENT_TYPE = CONSTRAIN + "json";
/**
 * Stop sequences for a raw completion: the end of a final answer and the end
 * of a tool call. Servers usually strip the stop sequence from the text,
 * which the parser accepts.
 */
export const HARMONY_STOP_TOKENS: readonly string[] = Object.freeze([RETURN, CALL]);
/** The model identity GPT-OSS was trained with. */
export const DEFAULT_HARMONY_MODEL_IDENTITY = "You are ChatGPT, a large language model trained by OpenAI.";

export type HarmonyRole = "system" | "developer" | "user" | "assistant" | "tool";

/**
 * One Harmony message. A tool message's `name` is its author, such as
 * `functions.get_weather`; for other roles a `name` renders as `role:name`.
 * `recipient` is the `to=` target (`functions.NAME` for a call).
 * `contentType` follows the channel, such as {@link HARMONY_JSON_CONTENT_TYPE}.
 */
export interface HarmonyMessage {
  role: HarmonyRole;
  content: string;
  channel?: string;
  recipient?: string;
  contentType?: string;
  name?: string;
}

/**
 * The system message. A field left undefined takes the default GPT-OSS
 * expects (medium reasoning, no date); an empty string leaves it out.
 */
export interface HarmonySystem {
  modelIdentity?: string;
  knowledgeCutoff?: string;
  currentDate?: string;
  reasoningEffort?: "low" | "medium" | "high" | "";
  validChannels?: string[];
}

export interface HarmonyPromptOptions {
  /** The system message; undefined is the default, null leaves it out. */
  system?: HarmonySystem | null;
  /** Instructions and tools go in the developer message. */
  instructions?: string;
  tools?: HarmonyTool[];
  /**
   * Drop analysis messages before the first final message once the last
   * assistant message is a final one (default true).
   */
  autoDropAnalysis?: boolean;
  /** The turn to open for sampling (default assistant); null or "" renders the conversation alone. */
  nextRole?: string | null;
}

/**
 * A parsed completion. `reasoning` joins the assistant's analysis-channel
 * messages and `content` its final-channel (or channel-less) messages, each
 * with a newline. `toolCalls` are the calls to the functions namespace whose
 * arguments are a JSON object; others are left out (fail closed), and
 * {@link HarmonyFormatter.validateFormat} reports them.
 */
export interface HarmonyCompletion {
  messages: Required<HarmonyMessage>[];
  reasoning: string;
  content: string;
  toolCalls: ParsedToolCall[];
}

/** GPT-OSS Harmony tool definitions, calls and results. */
export class HarmonyFormatter implements ToolFormatter {
  formatToolDefinitions(tools: HarmonyTool[]): string {
    return tools.length ? toolsSection(tools) : "";
  }

  parseToolCalls(response: string): ParsedToolCall[] {
    return parseHarmonyCompletion(response).toolCalls;
  }

  /** Harmony results carry no call id; the author names the tool. */
  formatToolResult(name: string, result: Result, _callId?: string): string {
    const text = result.success ? resultText(result.data) : JSON.stringify({ error: result.error });
    return renderHarmonyMessage({
      role: "tool",
      content: text,
      channel: "commentary",
      recipient: "assistant",
      name: FUNCTIONS_PREFIX + name,
    });
  }

  modelFamily(): string {
    return "gpt-oss";
  }

  validateFormat(response: string): string | null {
    for (const message of parseMessages(response)) {
      if (message.role !== "assistant" || !message.recipient.startsWith(FUNCTIONS_PREFIX)) continue;
      const name = message.recipient.slice(FUNCTIONS_PREFIX.length);
      if (!name) return "tool call missing function name";
      let args: unknown;
      try {
        args = JSON.parse(message.content);
      } catch (error) {
        return `tool call "${name}" has invalid JSON arguments: ${(error as Error).message}`;
      }
      if (!isObject(args)) return `tool call "${name}" arguments must be a JSON object`;
    }
    return null;
  }
}

/**
 * Render one message. An assistant message with a recipient ends with the
 * call token; every other message ends with the end token.
 */
export function renderHarmonyMessage(message: HarmonyMessage): string {
  if (!ROLES.has(message.role)) throw new Error(`unknown Harmony role ${JSON.stringify(message.role)}`);
  const { role, content, channel = "", recipient = "", contentType = "", name = "" } = message;
  let out = START;
  if (role === "tool") {
    if (!name) throw new Error("a Harmony tool message needs a name, such as functions.get_weather");
    out += name;
  } else {
    out += role + (name ? ":" + name : "");
  }
  if (recipient && recipient !== "all") out += " to=" + recipient;
  if (channel) out += CHANNEL + channel;
  if (contentType) out += " " + contentType;
  out += MESSAGE + content;
  out += role === "assistant" && recipient ? CALL : END;
  return out;
}

/**
 * Render a conversation as a Harmony prompt for a raw completion: the system
 * message, a developer message holding the instructions and the tools
 * namespace when either is given, then `messages`. Analysis messages before
 * the first final message are dropped once the last assistant message is a
 * final one, as the reference renderer does: the chain of thought of a
 * finished answer is not sent back, while an unfinished tool-calling turn
 * keeps it.
 */
export function renderHarmonyPrompt(messages: HarmonyMessage[], options: HarmonyPromptOptions = {}): string {
  const tools = options.tools ?? [];
  let out = "";
  if (options.system !== null) {
    out += renderHarmonyMessage({ role: "system", content: systemText(options.system ?? {}, tools.length > 0) });
  }
  if (options.instructions || tools.length) {
    const sections: string[] = [];
    if (options.instructions) sections.push("# Instructions", options.instructions);
    if (tools.length) sections.push(toolsSection(tools));
    out += renderHarmonyMessage({ role: "developer", content: sections.join("\n\n") });
  }
  const history = options.autoDropAnalysis === false ? messages : dropAnalysis(messages);
  out += history.map(renderHarmonyMessage).join("");
  const nextRole = options.nextRole === undefined ? "assistant" : options.nextRole;
  if (nextRole) out += START + nextRole;
  return out;
}

/**
 * Parse completion text that follows a prompt ending in `<|start|>assistant`.
 * The first message may have no header role (the prompt opened it). A
 * message ends at an end, call or return token, or at the end of the text,
 * since servers strip the stop sequence. A call's recipient is read from
 * either header position: after the role (the reference renderer) or after
 * the channel (what the model emits).
 */
export function parseHarmonyCompletion(text: string): HarmonyCompletion {
  const messages = parseMessages(text);
  const reasoning: string[] = [];
  const content: string[] = [];
  const toolCalls: ParsedToolCall[] = [];
  for (const message of messages) {
    if (message.role !== "assistant") continue;
    if (message.recipient.startsWith(FUNCTIONS_PREFIX)) {
      const call = decodeCall(message, text);
      if (call) toolCalls.push(call);
    } else if (message.channel === "analysis") {
      reasoning.push(message.content);
    } else if (message.channel === "final" || (!message.channel && !message.recipient)) {
      content.push(message.content);
    }
  }
  return { messages, reasoning: reasoning.join("\n"), content: content.join("\n"), toolCalls };
}

function isObject(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function decodeCall(message: Required<HarmonyMessage>, raw: string): ParsedToolCall | null {
  const name = message.recipient.slice(FUNCTIONS_PREFIX.length);
  if (!name) return null;
  try {
    const args: unknown = JSON.parse(message.content);
    return isObject(args) ? { id: "", name, args, raw } : null;
  } catch {
    return null;
  }
}

function parseMessages(text: string): Required<HarmonyMessage>[] {
  const messages: Required<HarmonyMessage>[] = [];
  text.split(START).forEach((chunk, index) => {
    if (!chunk) return;
    const message = parseChunk(chunk, index === 0);
    if (message) messages.push(message);
  });
  return messages;
}

function words(text: string): string[] {
  return text.split(/\s+/).filter(Boolean);
}

function parseChunk(chunk: string, first: boolean): Required<HarmonyMessage> | null {
  const message: Required<HarmonyMessage> = {
    role: "assistant", content: "", channel: "", recipient: "", contentType: "", name: "",
  };
  const split = chunk.indexOf(MESSAGE);
  if (split < 0) {
    // Text before any header is a bare answer; a header with no message
    // token was cut off and carries no content.
    if (first && ![CHANNEL, CONSTRAIN, ...TERMINATORS].some((token) => chunk.includes(token))) {
      return { ...message, content: chunk };
    }
    return null;
  }
  let header = chunk.slice(0, split);
  const body = chunk.slice(split + MESSAGE.length);
  const ends = TERMINATORS.map((t) => body.indexOf(t)).filter((i) => i >= 0);
  message.content = body.slice(0, ends.length ? Math.min(...ends) : body.length);

  const constrain = header.indexOf(CONSTRAIN);
  if (constrain >= 0) {
    message.contentType = CONSTRAIN + header.slice(constrain + CONSTRAIN.length).trim();
    header = header.slice(0, constrain);
  }
  const channelAt = header.indexOf(CHANNEL);
  const head = channelAt >= 0 ? header.slice(0, channelAt) : header;
  const channelPart = channelAt >= 0 ? header.slice(channelAt + CHANNEL.length) : "";
  const roleWords: string[] = [];
  for (const word of words(head)) {
    if (word.startsWith("to=")) message.recipient = word.slice(3);
    else roleWords.push(word);
  }
  const extra: string[] = [];
  const channelWords = words(channelPart);
  if (channelWords.length) {
    message.channel = channelWords[0];
    for (const word of channelWords.slice(1)) {
      if (word.startsWith("to=")) message.recipient = word.slice(3);
      else extra.push(word);
    }
  }
  if (!message.contentType && extra.length) message.contentType = extra.join(" ");
  if (roleWords.length) {
    const author = roleWords[0];
    const colon = author.indexOf(":");
    const base = colon >= 0 ? author.slice(0, colon) : author;
    if (ROLES.has(base) && base !== "tool") {
      message.role = base as HarmonyRole;
      message.name = colon >= 0 ? author.slice(colon + 1) : "";
    } else {
      message.role = "tool";
      message.name = author;
    }
  }
  return message;
}

function dropAnalysis(messages: HarmonyMessage[]): HarmonyMessage[] {
  const lastAssistant = [...messages].reverse().find((m) => m.role === "assistant");
  const firstFinal = messages.findIndex((m) => m.channel === "final");
  if (lastAssistant?.channel !== "final" || firstFinal < 0) return messages;
  return messages.filter((m, i) => !(i < firstFinal && m.channel === "analysis"));
}

function systemText(system: HarmonySystem, hasFunctionTools: boolean): string {
  const {
    modelIdentity = DEFAULT_HARMONY_MODEL_IDENTITY,
    knowledgeCutoff = "2024-06",
    currentDate = "",
    reasoningEffort = "medium",
    validChannels = ["analysis", "commentary", "final"],
  } = system;
  if (!EFFORTS.has(reasoningEffort)) {
    throw new Error(`reasoningEffort must be low, medium or high, not ${JSON.stringify(reasoningEffort)}`);
  }
  const sections: string[] = [];
  const top = [
    modelIdentity,
    knowledgeCutoff && `Knowledge cutoff: ${knowledgeCutoff}`,
    currentDate && `Current date: ${currentDate}`,
  ].filter(Boolean);
  if (top.length) sections.push(top.join("\n"));
  if (reasoningEffort) sections.push(`Reasoning: ${reasoningEffort}`);
  if (validChannels.length) {
    let header = `# Valid channels: ${validChannels.join(", ")}. Channel must be included for every message.`;
    if (hasFunctionTools) header += "\nCalls to these tools must go to the commentary channel: 'functions'.";
    sections.push(header);
  }
  return sections.join("\n\n");
}

function schemaOf(tool: HarmonyTool): unknown {
  if ("parameters" in tool) return tool.parameters;
  if ("input_schema" in tool) return tool.input_schema;
  if ("inputSchema" in tool && typeof (tool as Record<string, unknown>).inputSchema === "function") {
    return (tool as { inputSchema: () => unknown }).inputSchema() ?? undefined;
  }
  return undefined;
}

function toolsSection(tools: HarmonyTool[]): string {
  const lines = ["## functions\n", "namespace functions {\n"];
  for (const tool of tools) {
    lines.push(...textLines(tool.description ?? "").map((line) => `// ${line}`));
    const schema = schemaOf(tool);
    lines.push(
      schema === undefined || schema === null
        ? `type ${tool.name} = () => any;\n`
        : `type ${tool.name} = (_: ${typescript(schema, "")}) => any;\n`,
    );
  }
  lines.push("} // namespace functions");
  return "# Tools\n\n" + lines.join("\n");
}

/** Split like Rust's str::lines: no trailing empty line, and \r\n allowed. */
function textLines(text: string): string[] {
  if (!text) return [];
  const parts = text.split("\n");
  if (parts[parts.length - 1] === "") parts.pop();
  return parts.map((part) => (part.endsWith("\r") ? part.slice(0, -1) : part));
}

function get(schema: unknown, key: string): unknown {
  return isObject(schema) ? schema[key] : undefined;
}

function has(schema: unknown, key: string): boolean {
  return isObject(schema) && key in schema;
}

function isEnum(schema: unknown): boolean {
  const values = get(schema, "enum");
  return Array.isArray(values) && values.length > 0;
}

/** A default beside a property: a string is quoted unless the schema is an enum. */
function defaultText(value: unknown, schema: unknown): string {
  if (typeof value === "string") return isEnum(schema) ? `default: ${value}` : `default: "${value}"`;
  return `default: ${JSON.stringify(value)}`;
}

function nullable(typeText: string, schema: unknown): string {
  return get(schema, "nullable") === true && !typeText.includes("null") ? `${typeText} | null` : typeText;
}

/** Port of json_schema_to_typescript in openai/harmony src/encoding.rs. */
function typescript(schema: unknown, indent: string): string {
  const oneOf = get(schema, "oneOf");
  if (Array.isArray(oneOf)) {
    let out = "";
    for (const variant of oneOf) {
      out += `\n${indent} | ` + nullable(typescript(variant, indent + "   "), variant);
      const comments: string[] = [];
      const description = get(variant, "description");
      if (typeof description === "string") comments.push(description);
      if (has(variant, "default")) {
        const value = get(variant, "default");
        // Here the reference quotes a string default even for an enum.
        comments.push(
          typeof value === "string" && !isEnum(variant) ? `default: "${value}"` : `default: ${JSON.stringify(value)}`,
        );
      }
      if (comments.length) out += " // " + comments.join(" ");
    }
    return out;
  }
  const kind = get(schema, "type");
  if (Array.isArray(kind)) {
    const names = kind.filter((k): k is string => typeof k === "string").map((k) => (k === "integer" ? "number" : k));
    return names.length ? names.join(" | ") : "any";
  }
  switch (kind) {
    case "object":
      return typescriptObject(schema as Record<string, unknown>, indent);
    case "string": {
      const values = get(schema, "enum");
      const literals = Array.isArray(values)
        ? values.filter((v): v is string => typeof v === "string").map((v) => `"${v}"`)
        : [];
      return literals.length ? literals.join(" | ") : "string";
    }
    case "number":
    case "integer":
      return "number";
    case "boolean":
      return "boolean";
    case "array":
      return has(schema, "items") ? `${typescript(get(schema, "items"), indent)}[]` : "Array<any>";
    default:
      return "any";
  }
}

function typescriptObject(schema: Record<string, unknown>, indent: string): string {
  let out = "";
  if (typeof schema.description === "string") out += `${indent}// ${schema.description}\n`;
  out += "{\n";
  const properties = schema.properties;
  if (isObject(properties)) {
    const required = new Set(
      Array.isArray(schema.required) ? schema.required.filter((r): r is string => typeof r === "string") : [],
    );
    for (const [key, value] of Object.entries(properties)) {
      const optional = required.has(key) ? "" : "?";
      const title = get(value, "title");
      if (typeof title === "string") out += `${indent}// ${title}\n${indent}//\n`;
      const hasOneOf = has(value, "oneOf");
      const description = get(value, "description");
      if (!hasOneOf && typeof description === "string") out += `${indent}// ${description}\n`;
      const examples = get(value, "examples");
      if (Array.isArray(examples) && examples.length) {
        out += `${indent}// Examples:\n`;
        for (const example of examples) {
          if (typeof example === "string") out += `${indent}// - "${example}"\n`;
        }
      }
      const oneOf = get(value, "oneOf");
      if (Array.isArray(oneOf)) {
        out += oneOfProperty(key, optional, value as Record<string, unknown>, oneOf, indent);
        continue;
      }
      out += `${indent}${key}${optional}: ` + nullable(typescript(value, indent + "    "), value) + ",";
      if (!hasOneOf && has(value, "default")) out += " // " + defaultText(get(value, "default"), value);
      out += "\n";
    }
  }
  return out + `${indent}}`;
}

function oneOfProperty(
  key: string,
  optional: string,
  value: Record<string, unknown>,
  variants: unknown[],
  indent: string,
): string {
  let out = "";
  const propDescription = typeof value.description === "string" ? value.description : undefined;
  const skip = propDescription !== undefined && get(variants[0], "description") === propDescription;
  let describedAbove = false;
  if (propDescription !== undefined && !skip) {
    out += `${indent}// ${propDescription}\n`;
    describedAbove = true;
  }
  if ("default" in value) out += `${indent}// ${defaultText(value.default, value)}\n`;
  out += `${indent}${key}${optional}:\n`;
  variants.forEach((variant, i) => {
    out += `${indent} | ` + nullable(typescript(variant, indent + "   "), variant);
    const comments: string[] = [];
    const description = get(variant, "description");
    if ((i !== 0 || !describedAbove) && typeof description === "string" && description !== propDescription) {
      comments.push(description);
    }
    if (has(variant, "default")) comments.push(defaultText(get(variant, "default"), variant));
    if (comments.length) out += " // " + comments.join(" ");
    out += "\n";
  });
  return out + `${indent},\n`;
}
