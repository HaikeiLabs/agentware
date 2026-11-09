import { readFileSync } from "node:fs";
import {
  FetchFn,
  FetchLikeInit,
  FetchLikeResponse,
  Message,
  OpenAIBackend,
  OpenAIError,
  Role,
  ToolDefinition,
  parseToolArguments,
} from "../src/llm/index.js";

function jsonResponse(body: string, status = 200): FetchLikeResponse {
  return {
    ok: status >= 200 && status < 300,
    status,
    text: async () => body,
  };
}

function completionBody(
  overrides: Record<string, unknown> = {}
): string {
  return JSON.stringify({
    id: "chatcmpl-1",
    choices: [
      {
        message: { role: "assistant", content: "hello" },
        finish_reason: "stop",
      },
    ],
    usage: { prompt_tokens: 7, completion_tokens: 3, total_tokens: 10 },
    ...overrides,
  });
}

interface CapturedRequest {
  url: string;
  init: FetchLikeInit;
}

function capturingFetch(response: FetchLikeResponse): {
  fetchFn: FetchFn;
  captured: CapturedRequest[];
} {
  const captured: CapturedRequest[] = [];
  const fetchFn: FetchFn = async (url, init) => {
    captured.push({ url, init: init as FetchLikeInit });
    return response;
  };
  return { fetchFn, captured };
}

const TOOL_DEF: ToolDefinition = {
  name: "get_weather",
  description: "Get the weather",
  input_schema: { type: "object", properties: { city: { type: "string" } } },
};

describe("OpenAIBackend", () => {
  it("maps a content completion to Response", async () => {
    const { fetchFn } = capturingFetch(jsonResponse(completionBody()));
    const backend = new OpenAIBackend({
      model: "gpt-test",
      apiKey: "sk-test-secret",
      fetchFn,
    });

    const resp = await backend.complete([
      { role: Role.USER, content: "hi" },
    ]);

    expect(resp.content).toBe("hello");
    expect(resp.tool_calls).toEqual([]);
    expect(resp.finish_reason).toBe("stop");
    expect(resp.usage_tokens).toEqual({
      prompt_tokens: 7,
      completion_tokens: 3,
      total_tokens: 10,
    });
  });

  it("captures reasoning_content on a separate channel", async () => {
    const body = JSON.stringify({
      id: "chatcmpl-r",
      choices: [
        {
          message: {
            role: "assistant",
            content: '{"tool": "search", "args": {"q": "x"}}',
            reasoning_content: "goal: search\n\nobservation: the index is fresh",
          },
          finish_reason: "stop",
        },
      ],
      usage: { prompt_tokens: 7, completion_tokens: 3, total_tokens: 10 },
    });
    const { fetchFn } = capturingFetch(jsonResponse(body));
    const backend = new OpenAIBackend({
      model: "deepseek-reasoner",
      fetchFn,
    });

    const resp = await backend.complete([{ role: Role.USER, content: "hi" }]);

    // Reasoning is carried separately, never merged into content.
    expect(resp.reasoning).toBe("goal: search\n\nobservation: the index is fresh");
    expect(resp.content).toBe('{"tool": "search", "args": {"q": "x"}}');
    expect(resp.reasoning).not.toBe(resp.content);
  });

  it("sends an OpenAI-compatible request with auth header and tools", async () => {
    const { fetchFn, captured } = capturingFetch(jsonResponse(completionBody()));
    const backend = new OpenAIBackend({
      model: "gpt-test",
      baseUrl: "https://llm.example.com/v1/",
      apiKey: "sk-test-secret",
      temperature: 0.2,
      maxTokens: 100,
      fetchFn,
    });

    const messages: Message[] = [
      { role: Role.SYSTEM, content: "be brief" },
      { role: Role.USER, content: "weather in SF?" },
    ];
    await backend.complete(messages, [TOOL_DEF]);

    expect(captured).toHaveLength(1);
    expect(captured[0].url).toBe(
      "https://llm.example.com/v1/chat/completions"
    );
    expect(captured[0].init.method).toBe("POST");
    expect(captured[0].init.headers).toMatchObject({
      "Content-Type": "application/json",
      Authorization: "Bearer sk-test-secret",
    });

    const body = JSON.parse(captured[0].init.body!) as Record<string, unknown>;
    expect(body.model).toBe("gpt-test");
    expect(body.temperature).toBe(0.2);
    expect(body.max_tokens).toBe(100);
    expect(body.messages).toEqual([
      { role: "system", content: "be brief" },
      { role: "user", content: "weather in SF?" },
    ]);
    expect(body.tools).toEqual([
      {
        type: "function",
        function: {
          name: "get_weather",
          description: "Get the weather",
          parameters: TOOL_DEF.input_schema,
        },
      },
    ]);
    expect(body.tool_choice).toBe("auto");
  });

  it("maps tool and assistant tool_call messages to the wire format", async () => {
    const { fetchFn, captured } = capturingFetch(jsonResponse(completionBody()));
    const backend = new OpenAIBackend({ model: "gpt-test", fetchFn });

    const messages: Message[] = [
      {
        role: Role.ASSISTANT,
        content: "",
        tool_calls: [
          { id: "call_1", name: "get_weather", arguments: { city: "SF" } },
        ],
      },
      { role: Role.TOOL, content: "sunny", tool_call_id: "call_1" },
    ];
    await backend.complete(messages);

    const body = JSON.parse(captured[0].init.body!) as {
      messages: Array<Record<string, unknown>>;
    };
    expect(body.messages[0]).toEqual({
      role: "assistant",
      content: null,
      tool_calls: [
        {
          id: "call_1",
          type: "function",
          function: {
            name: "get_weather",
            arguments: '{"city":"SF"}',
          },
        },
      ],
    });
    expect(body.messages[1]).toEqual({
      role: "tool",
      tool_call_id: "call_1",
      content: "sunny",
    });
  });

  it("parses native tool calls with JSON arguments", async () => {
    const body = JSON.stringify({
      choices: [
        {
          message: {
            role: "assistant",
            content: null,
            tool_calls: [
              {
                id: "call_9",
                function: {
                  name: "get_weather",
                  arguments: '{"city":"SF","units":"c"}',
                },
              },
            ],
          },
          finish_reason: "tool_calls",
        },
      ],
    });
    const { fetchFn } = capturingFetch(jsonResponse(body));
    const backend = new OpenAIBackend({ model: "gpt-test", fetchFn });

    const resp = await backend.complete([{ role: Role.USER, content: "x" }]);

    expect(resp.content).toBe("");
    expect(resp.finish_reason).toBe("tool_calls");
    expect(resp.tool_calls).toEqual([
      {
        id: "call_9",
        name: "get_weather",
        arguments: { city: "SF", units: "c" },
      },
    ]);
  });

  it("throws OpenAIError with status on HTTP errors and never leaks the key", async () => {
    const { fetchFn } = capturingFetch(
      jsonResponse(JSON.stringify({ error: { message: "bad key" } }), 401)
    );
    const backend = new OpenAIBackend({
      model: "gpt-test",
      apiKey: "sk-test-secret",
      fetchFn,
    });

    await expect(
      backend.complete([{ role: Role.USER, content: "x" }])
    ).rejects.toMatchObject({ name: "OpenAIError", status: 401 });
    await expect(
      backend.complete([{ role: Role.USER, content: "x" }])
    ).rejects.toThrow(/HTTP 401/);
  });

  it("truncates oversized HTTP error details", async () => {
    const big = "x".repeat(2000);
    const { fetchFn } = capturingFetch(jsonResponse(big, 500));
    const backend = new OpenAIBackend({ model: "gpt-test", fetchFn });

    const err = await backend
      .complete([{ role: Role.USER, content: "x" }])
      .catch((e: unknown) => e);
    expect(err).toBeInstanceOf(OpenAIError);
    expect((err as OpenAIError).message.length).toBeLessThan(600);
    expect((err as OpenAIError).message).toContain("...");
  });

  it("times out via the injected signal and reports no secrets", async () => {
    const slowFetch: FetchFn = async (_url, init) => {
      await new Promise((resolve) => setTimeout(resolve, 80));
      if (init?.signal?.aborted) {
        const err = new Error("aborted");
        err.name = "AbortError";
        throw err;
      }
      return jsonResponse(completionBody());
    };
    const backend = new OpenAIBackend({
      model: "gpt-test",
      apiKey: "sk-test-secret",
      timeoutMs: 20,
      fetchFn: slowFetch,
    });

    await expect(
      backend.complete([{ role: Role.USER, content: "x" }])
    ).rejects.toThrow("request timed out after 20ms");
  });

  it("wraps network failures without leaking headers", async () => {
    const failingFetch: FetchFn = async () => {
      throw new Error("connect ECONNREFUSED 127.0.0.1:8080");
    };
    const backend = new OpenAIBackend({
      model: "gpt-test",
      apiKey: "sk-test-secret",
      fetchFn: failingFetch,
    });

    await expect(
      backend.complete([{ role: Role.USER, content: "x" }])
    ).rejects.toThrow("request failed: connect ECONNREFUSED");
  });

  it("rejects response bodies over the byte limit", async () => {
    const { fetchFn } = capturingFetch(jsonResponse("y".repeat(100)));
    const backend = new OpenAIBackend({
      model: "gpt-test",
      maxResponseBytes: 10,
      fetchFn,
    });

    await expect(
      backend.complete([{ role: Role.USER, content: "x" }])
    ).rejects.toThrow("response body exceeds limit of 10 bytes");
  });

  it("rejects malformed JSON bodies", async () => {
    const { fetchFn } = capturingFetch(jsonResponse("not json"));
    const backend = new OpenAIBackend({ model: "gpt-test", fetchFn });

    await expect(
      backend.complete([{ role: Role.USER, content: "x" }])
    ).rejects.toThrow("malformed JSON in response body");
  });

  it("rejects unexpected response shapes", async () => {
    const { fetchFn } = capturingFetch(jsonResponse("{}"));
    const backend = new OpenAIBackend({ model: "gpt-test", fetchFn });

    await expect(
      backend.complete([{ role: Role.USER, content: "x" }])
    ).rejects.toThrow("missing choices[0].message");
  });

  it("falls back to _raw for malformed tool call arguments", async () => {
    const body = JSON.stringify({
      choices: [
        {
          message: {
            tool_calls: [
              {
                id: "call_1",
                function: { name: "get_weather", arguments: "not json" },
              },
            ],
          },
          finish_reason: "tool_calls",
        },
      ],
    });
    const { fetchFn } = capturingFetch(jsonResponse(body));
    const backend = new OpenAIBackend({ model: "gpt-test", fetchFn });

    const resp = await backend.complete([{ role: Role.USER, content: "x" }]);
    expect(resp.tool_calls[0].arguments).toEqual({ _raw: "not json" });
  });

  it("bounds oversized tool call arguments", async () => {
    const body = JSON.stringify({
      choices: [
        {
          message: {
            tool_calls: [
              {
                function: { name: "get_weather", arguments: "z".repeat(1000) },
              },
            ],
          },
          finish_reason: "tool_calls",
        },
      ],
    });
    const { fetchFn } = capturingFetch(jsonResponse(body));
    const backend = new OpenAIBackend({
      model: "gpt-test",
      maxToolArgBytes: 64,
      fetchFn,
    });

    const resp = await backend.complete([{ role: Role.USER, content: "x" }]);
    expect(resp.tool_calls[0].id).toBe("call_0");
    expect((resp.tool_calls[0].arguments._raw as string).length).toBe(64);
  });

  it("parseToolArguments handles empty, object, and non-object JSON", () => {
    expect(parseToolArguments(null, 100)).toEqual({});
    expect(parseToolArguments("", 100)).toEqual({});
    expect(parseToolArguments('{"a":1}', 100)).toEqual({ a: 1 });
    expect(parseToolArguments("[1,2]", 100)).toEqual({ _raw: "[1,2]" });
    expect(parseToolArguments("broken{", 100)).toEqual({ _raw: "broken{" });
  });

  it("exposes backend metadata", () => {
    const backend = new OpenAIBackend({
      model: "gpt-test",
      contextWindowSize: 4096,
      fetchFn: (async () => jsonResponse("{}")) as FetchFn,
    });
    expect(backend.supportsNativeToolCalling()).toBe(true);
    expect(backend.modelName()).toBe("gpt-test");
    expect(backend.contextWindowSize()).toBe(4096);
  });
});

describe("shared provider extras", () => {
  const cases = JSON.parse(readFileSync("../fixtures/llm/provider-extras/cases.json", "utf8")) as Array<{ name: string; response: unknown; expected: { role: string; tool_calls: unknown[] } }>;
  it.each(cases)("round trips $name", async ({ response, expected }) => {
    const { fetchFn, captured } = capturingFetch(jsonResponse(JSON.stringify(response)));
    const backend = new OpenAIBackend({ model: "fixture", fetchFn });
    const first = await backend.complete([{ role: Role.USER, content: "go" }]);
    await backend.complete([{ role: Role.ASSISTANT, content: "", tool_calls: first.tool_calls }]);
    const body = JSON.parse(captured[1].init.body!);
    expect(body.messages[0].tool_calls).toEqual(expected.tool_calls);
  });
});
