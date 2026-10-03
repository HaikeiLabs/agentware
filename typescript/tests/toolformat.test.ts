import {
  GenericFormatter,
  OpenAIFOrmatter,
  AnthropicFormatter,
  QwenFormatter,
  DeepSeekFormatter,
  GLMFormatter,
  getFormatter,
} from "../src/toolformat/index.js";
import { Result } from "../src/tools/index.js";
import type { AnyTool } from "../src/tools/index.js";

function makeTool(name: string, description: string, inputSchema?: Record<string, unknown>): AnyTool {
  const tool: AnyTool = {
    name,
    description,
    execute: async () => new Result(true, null),
  };
  if (inputSchema) {
    (tool as unknown as Record<string, unknown>).inputSchema = () => inputSchema;
  }
  return tool;
}

function makeResult(data?: unknown, error?: string): Result {
  return new Result(error == null, data, error ?? undefined);
}

// ---------------------------------------------------------------------------
// GenericFormatter
// ---------------------------------------------------------------------------
describe("GenericFormatter", () => {
  let fmt: GenericFormatter;

  beforeEach(() => {
    fmt = new GenericFormatter();
  });

  describe("formatToolDefinitions", () => {
    it("returns empty string for empty array", () => {
      expect(fmt.formatToolDefinitions([])).toBe("");
    });

    it("formats a single tool without inputSchema", () => {
      const tools = [makeTool("get_weather", "Get the weather")];
      const out = fmt.formatToolDefinitions(tools);
      const parsed = JSON.parse(out);
      expect(parsed).toHaveLength(1);
      expect(parsed[0].type).toBe("function");
      expect(parsed[0].function.name).toBe("get_weather");
      expect(parsed[0].function.description).toBe("Get the weather");
      expect(parsed[0].function.parameters).toBeUndefined();
    });

    it("formats a single tool with inputSchema", () => {
      const schema = { type: "object", properties: { loc: { type: "string" } } };
      const tools = [makeTool("get_weather", "Get the weather", schema)];
      const out = fmt.formatToolDefinitions(tools);
      const parsed = JSON.parse(out);
      expect(parsed[0].function.parameters).toEqual(schema);
    });

    it("formats multiple tools", () => {
      const tools = [
        makeTool("tool_a", "Tool A"),
        makeTool("tool_b", "Tool B"),
      ];
      const out = fmt.formatToolDefinitions(tools);
      const parsed = JSON.parse(out);
      expect(parsed).toHaveLength(2);
      expect(parsed[0].function.name).toBe("tool_a");
      expect(parsed[1].function.name).toBe("tool_b");
    });
  });

  describe("parseToolCalls", () => {
    it("returns empty array for empty string", () => {
      expect(fmt.parseToolCalls("")).toEqual([]);
    });

    it("parses valid JSON array", () => {
      const response = JSON.stringify([
        { id: "call_1", name: "get_weather", arguments: { loc: "NYC" } },
      ]);
      const calls = fmt.parseToolCalls(response);
      expect(calls).toHaveLength(1);
      expect(calls[0].id).toBe("call_1");
      expect(calls[0].name).toBe("get_weather");
      expect(calls[0].args).toEqual({ loc: "NYC" });
    });

    it("returns empty array for invalid JSON", () => {
      expect(fmt.parseToolCalls("not-json")).toEqual([]);
    });

    it("returns empty array for non-array JSON", () => {
      expect(fmt.parseToolCalls('{"a":1}')).toEqual([]);
    });

    it("skips entries with missing name", () => {
      const response = JSON.stringify([
        { id: "call_1", arguments: {} },
        { id: "call_2", name: "valid_tool", arguments: {} },
      ]);
      const calls = fmt.parseToolCalls(response);
      expect(calls).toHaveLength(1);
      expect(calls[0].name).toBe("valid_tool");
    });

    it("parses string arguments as JSON", () => {
      const response = JSON.stringify([
        { id: "c1", name: "tool", arguments: '{"a":1}' },
      ]);
      const calls = fmt.parseToolCalls(response);
      expect(calls[0].args).toEqual({ a: 1 });
    });

    it("handles unparseable string arguments as empty object", () => {
      const response = JSON.stringify([
        { id: "c1", name: "tool", arguments: "not-json" },
      ]);
      const calls = fmt.parseToolCalls(response);
      expect(calls[0].args).toEqual({});
    });

    it("handles object arguments directly", () => {
      const response = JSON.stringify([
        { id: "c1", name: "tool", arguments: { a: 1 } },
      ]);
      const calls = fmt.parseToolCalls(response);
      expect(calls[0].args).toEqual({ a: 1 });
    });
  });

  describe("formatToolResult", () => {
    it("returns data string for successful string result", () => {
      const r = makeResult("plain output");
      expect(fmt.formatToolResult("t", r)).toBe("plain output");
    });

    it("returns JSON-stringified data for successful object result", () => {
      const r = makeResult({ key: "val" });
      expect(fmt.formatToolResult("t", r)).toBe(JSON.stringify({ key: "val" }));
    });

    it("returns error prefixed string for failed result", () => {
      const r = makeResult(null, "something broke");
      expect(fmt.formatToolResult("t", r)).toBe("Error: something broke");
    });
  });

  describe("modelFamily", () => {
    it("returns 'generic'", () => {
      expect(fmt.modelFamily()).toBe("generic");
    });
  });

  describe("validateFormat", () => {
    it("returns null for empty string", () => {
      expect(fmt.validateFormat("")).toBeNull();
    });

    it("returns null for valid tool call array", () => {
      const response = JSON.stringify([
        { name: "tool", arguments: {} },
      ]);
      expect(fmt.validateFormat(response)).toBeNull();
    });

    it("returns error for invalid JSON", () => {
      expect(fmt.validateFormat("broken")).toBe("invalid JSON format");
    });

    it("returns error for non-array JSON", () => {
      expect(fmt.validateFormat('"string"')).toBe("expected JSON array");
    });

    it("returns error for missing name", () => {
      const response = JSON.stringify([
        { arguments: {} },
      ]);
      expect(fmt.validateFormat(response)).toBe("tool call missing 'name' field");
    });

    it("returns error for missing arguments", () => {
      const response = JSON.stringify([
        { name: "tool" },
      ]);
      expect(fmt.validateFormat(response)).toBe('tool call "tool" missing \'arguments\'');
    });

    it("returns error when arguments is null", () => {
      const response = JSON.stringify([
        { name: "tool", arguments: null },
      ]);
      expect(fmt.validateFormat(response)).toBe('tool call "tool" missing \'arguments\'');
    });
  });
});

// ---------------------------------------------------------------------------
// OpenAIFOrmatter
// ---------------------------------------------------------------------------
describe("OpenAIFOrmatter", () => {
  let fmt: OpenAIFOrmatter;

  beforeEach(() => {
    fmt = new OpenAIFOrmatter();
  });

  it("has modelFamily 'openai'", () => {
    expect(fmt.modelFamily()).toBe("openai");
  });

  it("inherits formatToolDefinitions from GenericFormatter", () => {
    const tools = [makeTool("foo", "Foo")];
    const out = fmt.formatToolDefinitions(tools);
    const parsed = JSON.parse(out);
    expect(parsed[0].function.name).toBe("foo");
  });

  it("inherits parseToolCalls from GenericFormatter", () => {
    const response = JSON.stringify([{ name: "foo", arguments: { x: 1 } }]);
    const calls = fmt.parseToolCalls(response);
    expect(calls).toHaveLength(1);
    expect(calls[0].name).toBe("foo");
  });

  it("inherits formatToolResult from GenericFormatter", () => {
    const r = makeResult("data");
    expect(fmt.formatToolResult("t", r)).toBe("data");
  });

  it("validates format through GenericFormatter", () => {
    expect(fmt.validateFormat("")).toBeNull();
    expect(fmt.validateFormat("broken")).toBe("invalid JSON format");
  });
});

// ---------------------------------------------------------------------------
// DeepSeekFormatter
// ---------------------------------------------------------------------------
describe("DeepSeekFormatter", () => {
  let fmt: DeepSeekFormatter;

  beforeEach(() => {
    fmt = new DeepSeekFormatter();
  });

  it("has modelFamily 'deepseek'", () => {
    expect(fmt.modelFamily()).toBe("deepseek");
  });

  it("inherits formatToolDefinitions from GenericFormatter", () => {
    const tools = [makeTool("foo", "Foo")];
    const out = fmt.formatToolDefinitions(tools);
    const parsed = JSON.parse(out);
    expect(parsed[0].function.name).toBe("foo");
  });

  it("inherits parseToolCalls from GenericFormatter", () => {
    const response = JSON.stringify([{ name: "foo", arguments: { x: 1 } }]);
    const calls = fmt.parseToolCalls(response);
    expect(calls).toHaveLength(1);
    expect(calls[0].name).toBe("foo");
  });

  it("inherits formatToolResult from GenericFormatter", () => {
    const r = makeResult("data");
    expect(fmt.formatToolResult("t", r)).toBe("data");
  });

  it("validates format through GenericFormatter", () => {
    expect(fmt.validateFormat("")).toBeNull();
    expect(fmt.validateFormat("broken")).toBe("invalid JSON format");
  });
});

// ---------------------------------------------------------------------------
// GLMFormatter
// ---------------------------------------------------------------------------
describe("GLMFormatter", () => {
  let fmt: GLMFormatter;

  beforeEach(() => {
    fmt = new GLMFormatter();
  });

  it("has modelFamily 'glm'", () => {
    expect(fmt.modelFamily()).toBe("glm");
  });

  it("inherits formatToolDefinitions from GenericFormatter", () => {
    const tools = [makeTool("foo", "Foo")];
    const out = fmt.formatToolDefinitions(tools);
    const parsed = JSON.parse(out);
    expect(parsed[0].function.name).toBe("foo");
  });

  it("inherits parseToolCalls from GenericFormatter", () => {
    const response = JSON.stringify([{ name: "foo", arguments: { x: 1 } }]);
    const calls = fmt.parseToolCalls(response);
    expect(calls).toHaveLength(1);
    expect(calls[0].name).toBe("foo");
  });

  it("inherits formatToolResult from GenericFormatter", () => {
    const r = makeResult("data");
    expect(fmt.formatToolResult("t", r)).toBe("data");
  });

  it("validates format through GenericFormatter", () => {
    expect(fmt.validateFormat("")).toBeNull();
    expect(fmt.validateFormat("broken")).toBe("invalid JSON format");
  });
});

// ---------------------------------------------------------------------------
// AnthropicFormatter
// ---------------------------------------------------------------------------
describe("AnthropicFormatter", () => {
  let fmt: AnthropicFormatter;

  beforeEach(() => {
    fmt = new AnthropicFormatter();
  });

  describe("parseToolCalls", () => {
    it("parses valid XML tool calls", () => {
      const response = [
        '<invoke tool="get_weather">',
        '{"loc": "NYC"}',
        "</invoke>",
      ].join("");
      const calls = fmt.parseToolCalls(response);
      expect(calls).toHaveLength(1);
      expect(calls[0].id).toBe("");
      expect(calls[0].name).toBe("get_weather");
      expect(calls[0].args).toEqual({ loc: "NYC" });
    });

    it("returns empty array for empty string", () => {
      expect(fmt.parseToolCalls("")).toEqual([]);
    });

    it("returns empty array when no invoke tags exist", () => {
      expect(fmt.parseToolCalls("<foo>bar</foo>")).toEqual([]);
    });

    it("parses multiple tool calls", () => {
      const response = [
        '<invoke tool="a">{"x":1}</invoke>',
        '<invoke tool="b">{"y":2}</invoke>',
      ].join("");
      const calls = fmt.parseToolCalls(response);
      expect(calls).toHaveLength(2);
      expect(calls[0].name).toBe("a");
      expect(calls[1].name).toBe("b");
    });

    it("falls back to _raw when JSON is unparseable", () => {
      const response = '<invoke tool="t">not-json</invoke>';
      const calls = fmt.parseToolCalls(response);
      expect(calls[0].args).toEqual({ _raw: "not-json" });
    });
  });

  describe("formatToolResult", () => {
    it("wraps success result in XML", () => {
      const r = makeResult("output");
      expect(fmt.formatToolResult("my_tool", r)).toBe(
        '<function_results><result tool="my_tool">output</result></function_results>',
      );
    });

    it("wraps JSON data in XML", () => {
      const r = makeResult({ a: 1 });
      expect(fmt.formatToolResult("t", r)).toBe(
        '<function_results><result tool="t">{"a":1}</result></function_results>',
      );
    });

    it("wraps error result in XML", () => {
      const r = makeResult(null, "fail");
      expect(fmt.formatToolResult("t", r)).toBe(
        '<function_results><result tool="t">Error: fail</result></function_results>',
      );
    });
  });

  describe("modelFamily", () => {
    it("returns 'anthropic'", () => {
      expect(fmt.modelFamily()).toBe("anthropic");
    });
  });

  describe("validateFormat", () => {
    it("returns null for empty string", () => {
      expect(fmt.validateFormat("")).toBeNull();
    });

    it("returns null for valid tool call", () => {
      expect(fmt.validateFormat('<invoke tool="t">{"a":1}</invoke>')).toBeNull();
    });

    it("returns error for missing tool attribute", () => {
      expect(fmt.validateFormat('<invoke>{"a":1}</invoke>')).toBe(
        "malformed tool call: missing tool attribute",
      );
    });

    it("returns error for missing arguments", () => {
      expect(fmt.validateFormat('<invoke tool="t"></invoke>')).toBe(
        'tool call "t" missing arguments',
      );
    });

    it("returns error for invalid JSON arguments", () => {
      expect(fmt.validateFormat('<invoke tool="t">bad-json</invoke>')).toBe(
        'tool call "t" has invalid JSON arguments',
      );
    });

    it("returns error for malformed partial invoke tag", () => {
      expect(fmt.validateFormat('<invoke>')).toBe(
        "malformed tool call: missing tool attribute",
      );
    });
  });
});

// ---------------------------------------------------------------------------
// QwenFormatter
// ---------------------------------------------------------------------------
describe("QwenFormatter", () => {
  let fmt: QwenFormatter;

  beforeEach(() => {
    fmt = new QwenFormatter();
  });

  describe("formatToolDefinitions", () => {
    it("returns empty string for empty array", () => {
      expect(fmt.formatToolDefinitions([])).toBe("");
    });

    it("formats a tool with XML tags", () => {
      const tools = [makeTool("get_weather", "Get the weather")];
      const out = fmt.formatToolDefinitions(tools);
      expect(out).toContain("<tool_description>");
      expect(out).toContain("<tool_name>get_weather</tool_name>");
      expect(out).toContain("<description>Get the weather</description>");
      expect(out).toContain("</tool_description>");
      expect(out).not.toContain("<parameters>");
    });

    it("includes parameters when inputSchema is present", () => {
      const schema = { type: "object", properties: { loc: { type: "string" } } };
      const tools = [makeTool("t", "desc", schema)];
      const out = fmt.formatToolDefinitions(tools);
      expect(out).toContain("<parameters>");
      expect(out).toContain(JSON.stringify(schema));
    });

    it("formats multiple tools", () => {
      const tools = [
        makeTool("a", "A"),
        makeTool("b", "B"),
      ];
      const out = fmt.formatToolDefinitions(tools);
      expect((out.match(/<tool_description>/g) || []).length).toBe(2);
    });
  });

  describe("parseToolCalls", () => {
    it("parses valid XML tool calls", () => {
      const response = [
        '<tool_call>',
        '<tool name="get_weather">',
        '{"loc": "NYC"}',
        '</tool>',
        '</tool_call>',
      ].join("");
      const calls = fmt.parseToolCalls(response);
      expect(calls).toHaveLength(1);
      expect(calls[0].name).toBe("get_weather");
      expect(calls[0].args).toEqual({ loc: "NYC" });
    });

    it("returns empty array for empty string", () => {
      expect(fmt.parseToolCalls("")).toEqual([]);
    });

    it("returns empty array when no tool_call tags exist", () => {
      expect(fmt.parseToolCalls("<foo>bar</foo>")).toEqual([]);
    });

    it("parses multiple tool calls", () => {
      const response = [
        '<tool_call><tool name="a">{"x":1}</tool></tool_call>',
        '<tool_call><tool name="b">{"y":2}</tool></tool_call>',
      ].join("");
      const calls = fmt.parseToolCalls(response);
      expect(calls).toHaveLength(2);
      expect(calls[0].name).toBe("a");
      expect(calls[1].name).toBe("b");
    });

    it("falls back to _raw when JSON is unparseable", () => {
      const response = '<tool_call><tool name="t">bad-json</tool></tool_call>';
      const calls = fmt.parseToolCalls(response);
      expect(calls[0].args).toEqual({ _raw: "bad-json" });
    });
  });

  describe("formatToolResult", () => {
    it("wraps success result in XML", () => {
      const r = makeResult("output");
      expect(fmt.formatToolResult("my_tool", r)).toBe(
        "<tool_response>\n<tool_name>my_tool</tool_name>\n<result>output</result>\n</tool_response>",
      );
    });

    it("wraps JSON data in XML", () => {
      const r = makeResult({ a: 1 });
      expect(fmt.formatToolResult("t", r)).toBe(
        "<tool_response>\n<tool_name>t</tool_name>\n<result>{\"a\":1}</result>\n</tool_response>",
      );
    });

    it("wraps error result in XML", () => {
      const r = makeResult(null, "fail");
      expect(fmt.formatToolResult("t", r)).toBe(
        "<tool_response>\n<tool_name>t</tool_name>\n<error>fail</error>\n</tool_response>",
      );
    });
  });

  describe("modelFamily", () => {
    it("returns 'qwen'", () => {
      expect(fmt.modelFamily()).toBe("qwen");
    });
  });

  describe("validateFormat", () => {
    it("returns null for empty string", () => {
      expect(fmt.validateFormat("")).toBeNull();
    });

    it("returns null for valid tool call", () => {
      expect(fmt.validateFormat(
        '<tool_call><tool name="t">{"a":1}</tool></tool_call>',
      )).toBeNull();
    });

    it("returns error for missing function name", () => {
      expect(fmt.validateFormat(
        '<tool_call><tool>{"a":1}</tool></tool_call>',
      )).toBe("malformed tool call");
    });

    it("returns error for missing arguments", () => {
      expect(fmt.validateFormat(
        '<tool_call><tool name="t"></tool></tool_call>',
      )).toBe('tool call "t" missing arguments');
    });

    it("returns error for invalid JSON arguments", () => {
      expect(fmt.validateFormat(
        '<tool_call><tool name="t">bad</tool></tool_call>',
      )).toBe('tool call "t" has invalid JSON arguments');
    });

    it("returns error for malformed partial tool tag", () => {
      expect(fmt.validateFormat("<tool_call>")).toBe("malformed tool call");
    });
  });
});

// ---------------------------------------------------------------------------
// getFormatter selector
// ---------------------------------------------------------------------------
describe("getFormatter", () => {
  it("returns AnthropicFormatter for anthropic model names", () => {
    expect(getFormatter("anthropic.claude-v2")).toBeInstanceOf(AnthropicFormatter);
    expect(getFormatter("Anthropic Claude")).toBeInstanceOf(AnthropicFormatter);
  });

  it("returns AnthropicFormatter for 'claude' model names too", () => {
    expect(getFormatter("claude-3-opus-20240229")).toBeInstanceOf(AnthropicFormatter);
  });

  it("returns QwenFormatter for qwen/qwq model names", () => {
    expect(getFormatter("qwen2.5-72b")).toBeInstanceOf(QwenFormatter);
    expect(getFormatter("QwQ-32B")).toBeInstanceOf(QwenFormatter);
    expect(getFormatter("qwen-turbo")).toBeInstanceOf(QwenFormatter);
  });

  it("returns DeepSeekFormatter for deepseek model names", () => {
    expect(getFormatter("deepseek-chat")).toBeInstanceOf(DeepSeekFormatter);
    expect(getFormatter("DeepSeek-R1")).toBeInstanceOf(DeepSeekFormatter);
  });

  it("returns GLMFormatter for glm/chatglm model names", () => {
    expect(getFormatter("glm-4")).toBeInstanceOf(GLMFormatter);
    expect(getFormatter("chatglm-turbo")).toBeInstanceOf(GLMFormatter);
    expect(getFormatter("ChatGLM3")).toBeInstanceOf(GLMFormatter);
  });

  it("returns OpenAIFOrmatter for gpt/o1/o3 model names", () => {
    expect(getFormatter("gpt-4o")).toBeInstanceOf(OpenAIFOrmatter);
    expect(getFormatter("o1-mini")).toBeInstanceOf(OpenAIFOrmatter);
    expect(getFormatter("o3-preview")).toBeInstanceOf(OpenAIFOrmatter);
    expect(getFormatter("GPT-4-turbo")).toBeInstanceOf(OpenAIFOrmatter);
  });

  it("returns GenericFormatter for unknown model names", () => {
    expect(getFormatter("unknown-model")).toBeInstanceOf(GenericFormatter);
    expect(getFormatter("llama-3")).toBeInstanceOf(GenericFormatter);
    expect(getFormatter("")).toBeInstanceOf(GenericFormatter);
  });
});
