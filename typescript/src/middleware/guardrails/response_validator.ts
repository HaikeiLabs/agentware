import { escapeRegExp, REASONING_TAG_PAIRS } from "../../reasoning/tags.js";
import { Nudge, retryNudge, unknownToolNudge } from "./nudge.js";

export interface ToolCall {
  tool: string;
  args: Record<string, unknown>;
}

export interface ValidationResult {
  toolCalls: ToolCall[];
  nudge: Nudge | null;
  needsRetry: boolean;
}

export class ResponseValidator {
  private toolNames: Set<string>;
  private rescueEnabled: boolean;
  private retryNudgeFn: (rawResponse: string, toolNames: string[]) => Nudge;

  private thinkPattern: RegExp;
  private pythonTagPattern: RegExp;
  private codeFencePattern: RegExp;
  private rehearsalPattern: RegExp;
  private qwenFunctionPattern: RegExp;

  constructor(
    toolNames: string[],
    rescueEnabled: boolean = true,
    retryNudgeFn?: (rawResponse: string, toolNames: string[]) => Nudge,
  ) {
    this.toolNames = new Set(toolNames);
    this.rescueEnabled = rescueEnabled;
    this.retryNudgeFn = retryNudgeFn ?? retryNudge;

    // A whole reasoning block for every pair in REASONING_TAG_PAIRS (think,
    // thinking, bracket THINK), case-insensitively and across newlines. Built
    // from the shared constants so the validator and the reasoning adapter
    // recognize the same tags; never type a reasoning tag literally here (see
    // src/reasoning/tags.ts).
    this.thinkPattern = new RegExp(
      REASONING_TAG_PAIRS.map((p) => `${escapeRegExp(p.open)}[\\s\\S]*?${escapeRegExp(p.close)}`).join("|"),
      "gi",
    );
    this.pythonTagPattern = new RegExp("\\<\\|python_tag\\|\\>", "gi");
    this.codeFencePattern = new RegExp("```(?:json)?\\s*\\n?", "g");
    this.rehearsalPattern = new RegExp("(\\w+)\\[ARGS\\](\\{.*?\\})", "g");
    this.qwenFunctionPattern = new RegExp("<function=([^>\\s]+)>(.*?)<\\/function>", "g");
  }

  validateTextResponse(response: string): ValidationResult {
    if (this.rescueEnabled) {
      const rescued = this.rescueToolCall(response);
      if (rescued.length > 0) {
        return { toolCalls: rescued, nudge: null, needsRetry: false };
      }
    }

    const nudge = this.retryNudgeFn(response, Array.from(this.toolNames));
    return { toolCalls: [], nudge, needsRetry: true };
  }

  validateToolCalls(toolCalls: ToolCall[]): ValidationResult {
    const unknown: string[] = [];
    const validCalls: ToolCall[] = [];

    for (const tc of toolCalls) {
      if (!this.toolNames.has(tc.tool)) {
        unknown.push(tc.tool);
      } else {
        validCalls.push(tc);
      }
    }

    if (unknown.length > 0) {
      const nudge = unknownToolNudge(unknown[0], Array.from(this.toolNames));
      return { toolCalls: [], nudge, needsRetry: true };
    }

    return { toolCalls: validCalls, nudge: null, needsRetry: false };
  }

  private rescueToolCall(response: string): ToolCall[] {
    let cleaned = response.replace(this.thinkPattern, "");
    cleaned = cleaned.replace(this.pythonTagPattern, "");
    cleaned = cleaned.trim();

    if (!cleaned) {
      return [];
    }

    let calls = this.extractJsonToolCalls(cleaned);
    if (calls.length > 0) {
      return calls;
    }

    calls = this.extractRehearsalToolCalls(cleaned);
    if (calls.length > 0) {
      return calls;
    }

    return this.extractQwenXmlToolCalls(cleaned);
  }

  private extractJsonToolCalls(text: string): ToolCall[] {
    const cleaned = text.replace(this.codeFencePattern, "").trim();

    const calls: ToolCall[] = [];
    let i = 0;
    while (i < cleaned.length) {
      if (cleaned[i] === "{") {
        let depth = 0;
        let j = i;
        while (j < cleaned.length) {
          if (cleaned[j] === "{") {
            depth++;
          } else if (cleaned[j] === "}") {
            depth--;
            if (depth === 0) {
              const candidate = cleaned.slice(i, j + 1);
              const call = this.tryParseToolCall(candidate);
              if (call) {
                calls.push(call);
              }
              i = j + 1;
              break;
            }
          }
          j++;
        }
        if (depth !== 0) {
          i++;
        }
      } else {
        i++;
      }
    }

    return calls;
  }

  private tryParseToolCall(jsonStr: string): ToolCall | null {
    let data: Record<string, unknown>;
    try {
      data = JSON.parse(jsonStr);
    } catch {
      return null;
    }

    const toolName = (data.tool as string) || (data.name as string);
    if (!toolName) {
      return null;
    }

    if (!this.toolNames.has(toolName)) {
      return null;
    }

    const args = (data.args as Record<string, unknown>) ||
      (data.arguments as Record<string, unknown>) || {};

    return { tool: toolName, args };
  }

  private extractRehearsalToolCalls(text: string): ToolCall[] {
    const calls: ToolCall[] = [];
    const regex = /(\w+)\[ARGS\](\{.*?\})/g;
    let match: RegExpExecArray | null;

    while ((match = regex.exec(text)) !== null) {
      const toolName = match[1];
      const argsStr = match[2];

      if (!this.toolNames.has(toolName)) {
        continue;
      }

      try {
        const args = JSON.parse(argsStr);
        if (typeof args === "object" && args !== null) {
          calls.push({ tool: toolName, args: args as Record<string, unknown> });
        }
      } catch {
        continue;
      }
    }

    return calls;
  }

  private extractQwenXmlToolCalls(_text: string): ToolCall[] {
    return [];
  }
}