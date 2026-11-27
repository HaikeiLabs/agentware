# Model Format Reference

The five supported model families and their tool-call / reasoning formats.
Each family has a corresponding `ToolFormatter` in Go, Python, and TypeScript,
and the reasoning adapter handles its reasoning shape.

---

## Quick Reference

| Family | Tool Definitions | Tool Calls (response) | Tool Result | Reasoning Field | Thinking Tags |
|--------|------------------|----------------------|-------------|-----------------|---------------|
| **OpenAI** | JSON function array (Generic) | JSON array: `{name, arguments}` | JSON | `reasoning_content` | none |
| **Anthropic** | Native `tools` or JSON `{name, description, input_schema}` array | Native or JSON `tool_use` blocks (legacy XML `<invoke>` fallback) | `tool_result` blocks | `thinking` (content block) | none |
| **Qwen** | Hermes JSON lines in `<tools>` | JSON in `<tool_call>` | Result text in `<tool_response>` | `reasoning_content` | `<thinking>...</thinking>`; the plain think pair (`THINK_OPEN`/`THINK_CLOSE`) without a reasoning parser |
| **DeepSeek** | JSON function array (Generic) | JSON array: `{name, arguments}` | JSON | `reasoning_content` | `[THINK]...[/THINK]` |
| **GLM** | JSON function array (Generic) | JSON array: `{name, arguments}` | JSON | `reasoning_content` (GLM-4); `reasoning` (GLM-5.3-Flash on vLLM) | the plain think pair (`THINK_OPEN`/`THINK_CLOSE`) without a reasoning parser |

---

## Per-Family Details

### Tool result serialization

Every formatter in every port turns a successful result into the same text
before wrapping it in its family's envelope:

| Result data | Text |
|---|---|
| A string | The string verbatim, with no added quotes, even when it looks like JSON |
| A dict/object, list/array, number or boolean | Compact JSON (no spaces), in key insertion order, non-ASCII text kept as is |
| None / null / undefined | The empty string |
| A value JSON cannot encode (Python, TypeScript) | `str()` / `String()` of it |

Go `tools.Result.Output` and Rust `ToolResult::output` are already strings, so
those ports render `Output` verbatim: the caller that fills `Output` with
structured data serializes it as compact JSON in key order. A failure keeps
each family's own error convention, shown in its table below.

Qwen follows the same rule. The official Qwen chat template (the
`chat_template` in the Qwen2.5 and Qwen3 `tokenizer_config.json`) emits a tool
message's content unchanged between the `<tool_response>` tags, so a string
result is not JSON-quoted. The shared fixtures pin all of this in every
family's `format_result` section.

### OpenAI (`OpenAIFOrmatter` / `OpenAIFOrmatter` / `OpenAIFOrmatter`)

| | Detail |
|---|---|
| **Model names** | `gpt-*`, `o1-*`, `o3-*` |
| **Definitions format** | OpenAI-compatible JSON array of `{type: "function", function: {name, description, parameters}}` (via `GenericFormatter`) |
| **Parse format** | JSON array of `{id, name, arguments}` |
| **Result format** | The result text (see [Tool result serialization](#tool-result-serialization)); a failure is `Error: <message>` |
| **Validate** | Validates JSON array, requires `name` on each entry |
| **Reasoning field** | `reasoning_content` — top-level field on the choice message |
| **Inline tags** | None. o-series models do not use inline thinking tags. |
| **Adapter** | No model-specific field registration needed; `reasoning_content` is in the known set. |

OpenAI-compatible native tool calls may carry opaque `extra_content` on each
call. The backend stores this as the call's `metadata` and the OpenAI-family
message formatter echoes it on that same call when formatting assistant
history. Gemini 3 uses `extra_content.google.thought_signature`; other
providers can use the same opaque field. An absent field stays absent. This
metadata is not reasoning display and must not be logged or displayed.

**Example response:**

```json
{
  "choices": [{
    "message": {
      "role": "assistant",
      "content": null,
      "reasoning_content": "I need to look up the weather...",
      "tool_calls": [{
        "id": "call_1",
        "type": "function",
        "function": {"name": "get_weather", "arguments": "{\"location\":\"Tokyo\"}"}
      }]
    }
  }]
}
```

### Anthropic native Messages API

Native-block formatters are available in all four ports alongside the
OpenAI-compatible Gemini 3 provider-metadata path. `formatAnthropicTools` /
`format_anthropic_tools` converts definitions, `parseAnthropicResponse` /
`parse_anthropic_response` extracts tool uses, and `formatAnthropicMessages` /
`format_anthropic_messages` produces assistant tool uses and user tool results.
Go exports the corresponding `FormatAnthropicTools`, `ParseAnthropicResponse`,
and `FormatAnthropicMessages` functions. Supply system instructions in the
API's top-level `system` parameter and use an Anthropic HTTP transport to send
the resulting blocks.

Signed `thinking` and `redacted_thinking` blocks remain opaque in each tool
call's `metadata`, and are replayed without alteration or logging. Interleaved
content stays in order, and adjacent tool results are grouped in a user turn.
These native formatters complement the legacy text-mode formatter below. See
[Anthropic’s thinking documentation](https://platform.claude.com/docs/en/build-with-claude/thinking)
for the Messages API replay requirements.

### Anthropic (`AnthropicFormatter` / `AnthropicFormatter` / `AnthropicFormatter`)

The text-mode formatter renders the shapes of the Anthropic Messages API
([tool use overview](https://platform.claude.com/docs/en/agents-and-tools/tool-use/overview)):
tool definitions, `tool_use` blocks and `tool_result` blocks, as JSON text.

| | Detail |
|---|---|
| **Model names** | `claude-*`, `anthropic-*` |
| **Definitions format** | JSON array of Anthropic tool definitions: `{"name", "description", "input_schema"}`. A tool without parameters gets `{"type": "object", "properties": {}}`, because the API requires `input_schema`. |
| **Parse format** | `tool_use` blocks, `{"type": "tool_use", "id", "name", "input"}`: one block, or a content array whose other blocks (such as text) are ignored. The block's `id` becomes the call id. A malformed block (missing `id` or `name`, or `input` not an object) is never returned as a call. When the response holds no `tool_use` block at all, the legacy `<function_calls><invoke tool="name">JSON args</invoke></function_calls>` form is parsed instead, and those calls have no id. |
| **Result format** | A `tool_result` block, `{"type": "tool_result", "tool_use_id", "content", "is_error"}`; `is_error: true` appears only on failure, and the content is then the error text. On success the content is the result text (see [Tool result serialization](#tool-result-serialization)). When the caller passes no call id, `tool_name` takes the place of `tool_use_id`. That block is a text rendering only: the Messages API rejects a `tool_result` without `tool_use_id`. |
| **Validate** | For `tool_use` blocks: a non-empty string `id` and `name`, an object `input`, and no duplicate ids. Otherwise, the legacy `<invoke>` checks (valid JSON arguments and non-empty names). |
| **Reasoning field** | `thinking` — a separate content block of `type: "thinking"` in the Messages API response |
| **Inline tags** | None. Extended thinking is a structured content block, not inline. |
| **Adapter** | `thinking` is in the known field set. No registration needed for Claude. |

**The call id.** The result formatter takes an optional call id in every port:
Python `format_tool_result(name, result, call_id=None)`, Go
`FormatToolResult(name, result, callID ...string)` (only the first value is
used), TypeScript `formatToolResult(name, result, callId?)`, and Rust
`format_tool_result_with_call_id(name, result, Option<&str>)`, a default
trait method next to `format_tool_result`. The other families accept the id
and ignore it. The Go and Rust inference executors pass the id of the call
they answer.

**Example response (Messages API):**

```json
{
  "content": [
    {"type": "thinking", "thinking": "I need to call get_weather...", "signature": "..."},
    {"type": "tool_use", "name": "get_weather", "input": {"location": "Tokyo"}, "id": "toolu_..."}
  ]
}
```

**Example response (text with a `tool_use` block):**

```json
[
  {"type": "text", "text": "Let me check."},
  {"type": "tool_use", "id": "toolu_01A", "name": "get_weather", "input": {"location": "Tokyo"}}
]
```

**Example tool result for that call:**

```json
{"type":"tool_result","tool_use_id":"toolu_01A","content":"Sunny, 22°C"}
```

**Example response (legacy text with function_calls):**

```text
<function_calls>
<invoke tool="get_weather">{"location": "Tokyo", "unit": "celsius"}</invoke>
</function_calls>
```

### Qwen (`QwenFormatter` / `QwenFormatter` / `QwenFormatter`)

| | Detail |
|---|---|
| **Model names** | `qwen-*`, `qwq-*` |
| **Definitions format** | Function definitions as JSON lines inside `<tools>` |
| **Parse format** | JSON `{name, arguments}` inside `<tool_call>`; legacy nested tool tags are deprecated |
| **Result format** | The result text inside `<tool_response>`, without a tool name (see [Tool result serialization](#tool-result-serialization)); a failure is `{"error": <message>}` |
| **Validate** | Validates `<tool_call>` tags have valid JSON arguments and non-empty names |
| **Reasoning field** | `reasoning_content` — top-level field on the choice message |
| **Inline tags** | `<thinking>...</thinking>` — may appear in the content text alongside tool calls. Qwen3/QwQ served without a reasoning parser (llama.cpp, vLLM without `--reasoning-parser`) emit the plain think pair (`THINK_OPEN`/`THINK_CLOSE`) instead |
| **Adapter** | `reasoning_content` and every pair in the [reasoning-tag constants](#reasoning-tag-constants) are handled generically. |

**Example response:**

```json
{
  "choices": [{
    "message": {
      "content": "<thinking>Need weather data</thinking>\n<tool_call>{\"name\":\"get_weather\",\"arguments\":{\"location\":\"Tokyo\"}}</tool_call>",
      "reasoning_content": "Need weather data"
    }
  }]
}
```

### DeepSeek (`DeepSeekFormatter` / `DeepSeekFormatter` / `DeepSeekFormatter`)

| | Detail |
|---|---|
| **Model names** | `deepseek-*` |
| **Definitions format** | OpenAI-compatible JSON array (same as `GenericFormatter`) |
| **Parse format** | JSON array of `{id, name, arguments}` (same as OpenAI) |
| **Result format** | The result text (see [Tool result serialization](#tool-result-serialization)); a failure is `Error: <message>` |
| **Validate** | Validates JSON array, requires `name` on each entry |
| **Reasoning field** | `reasoning_content` — native field on the choice message |
| **Inline tags** | `[THINK]...[/THINK]` — used in text-completion mode. Also stripped by the guardrails response validator. |
| **Adapter** | Both `reasoning_content` and `[THINK]` tags are handled. The adapter treats unbalanced `[THINK]` as a fail-closed error. |

**Example response (chat mode):**

```json
{
  "choices": [{
    "message": {
      "content": "Based on data, Tokyo is 22°C.",
      "reasoning_content": "Called get_weather for Tokyo..."
    }
  }]
}
```

**Example response (text mode):**

```text
[THINK]
Called get_weather for Tokyo, got 22°C.
[/THINK]
Based on data, Tokyo is 22°C.
```

### GLM (`GLMFormatter` / `GLMFormatter` / `GLMFormatter`)

| | Detail |
|---|---|
| **Model names** | `glm-*`, `chatglm-*` |
| **Definitions format** | OpenAI-compatible JSON array (same as `GenericFormatter`) |
| **Parse format** | JSON array of `{id, name, arguments}` (same as OpenAI) |
| **Result format** | The result text (see [Tool result serialization](#tool-result-serialization)); a failure is `Error: <message>` |
| **Validate** | Validates JSON array, requires `name` on each entry |
| **Reasoning field** | `reasoning_content` on GLM-4. GLM-5.3-Flash on vLLM uses `reasoning` (see below) |
| **Inline tags** | None with a reasoning parser. Served without one, GLM emits the plain think pair (`THINK_OPEN`/`THINK_CLOSE`) in `content`, which the adapter strips |
| **Adapter** | `reasoning_content` and `reasoning` are both in the known field set; no registration needed. |

**Example response:**

```json
{
  "choices": [{
    "message": {
      "content": null,
      "reasoning_content": "Querying weather API for Tokyo...",
      "tool_calls": [{
        "id": "call_1",
        "type": "function",
        "function": {"name": "get_weather", "arguments": "{\"location\":\"Tokyo\"}"}
      }]
    }
  }]
}
```

#### GLM-5.3-Flash (verified 2026-10-29)

Captured from `nvidia/GLM-5.3-Flash-FP4-Local` served by vLLM on the Spark
cluster, using `/v1/chat/completions` with and without streaming. The
`glm-5.3` cases in `fixtures/toolformat/glm-cases.json` and
`fixtures/reasoning/glm-cases.json` come from these responses.

| | Same as GLM-4? | GLM-5.3-Flash |
|---|---|---|
| Tool calls | Yes | `message.tool_calls[]` with `type: "function"`; `function.arguments` is a JSON **string** |
| Tool-call ids | Format only | `chatcmpl-tool-<16 hex>` |
| `finish_reason` | Yes | `tool_calls` after a tool call, `stop` otherwise |
| Reasoning | **No** | `message.reasoning`, and `delta.reasoning` when streaming. `reasoning_content` is absent |
| Thinking disabled | — | With `chat_template_kwargs: {"enable_thinking": false}`, `reasoning` is `null`, and the reasoning text can show up untagged in `content`. The adapter cannot separate it |
| Streaming tool calls | — | The first `tool_calls` delta carries `id`, `type`, `name` and the start of `arguments`; later deltas carry only `index` and argument fragments. The last fragment arrives in the same chunk as `finish_reason` |

**Example response (non-streaming):**

```json
{
  "choices": [{
    "message": {
      "role": "assistant",
      "content": null,
      "reasoning": "The user wants to know the weather in Tokyo. I have a get_weather tool available. Let me call it with location \"Tokyo\".",
      "tool_calls": [{
        "id": "chatcmpl-tool-b740f94d8f903c3d",
        "type": "function",
        "function": {"name": "get_weather", "arguments": "{\"location\": \"Tokyo\"}"}
      }]
    },
    "finish_reason": "tool_calls"
  }]
}
```

The adapter already finds `reasoning` (priority 4 in the known field set), so
no registration is needed. To parse these tool calls with `ParseToolCalls`,
flatten them to `{id, name, arguments}`, which leaves `arguments` as a string.
Python and TypeScript decode that string. Go's `GenericFormatter.ParseToolCalls`
currently drops such a call without an error (case "glm-5.3 single call,
arguments as JSON string").

### MiniMax (`MiniMaxFormatter` in Go, Python, TypeScript and Rust)

Sources: the `chat_template.jinja` of `MiniMaxAI/MiniMax-M2` (M2.1 and M2.5
render tool calls the same way) and `MiniMaxAI/MiniMax-M3`, and vLLM's
`vllm/parser/minimax_m2.py` and `minimax_m3.py`. The shared fixture is
`fixtures/toolformat/minimax-cases.json`.

| | Detail |
|---|---|
| **Model names** | `minimax-m2*` routes to the M2 version, `minimax-m3*` to the M3 version (case-insensitive substring, so `MiniMaxAI/MiniMax-M2.5` matches). Other MiniMax ids (MiniMax-Text-01, MiniMax-M1) use a different format and fall through to `GenericFormatter` |
| **Version** | Python `MiniMaxFormatter(version="m2" \| "m3")`, Go `MiniMaxFormatter{Version: MiniMaxM2 \| MiniMaxM3}` (zero value is M2), TypeScript `new MiniMaxFormatter({ version })`, Rust `MiniMaxFormatter::new(MiniMaxVersion::M2 \| M3)`. Default M2. Only the definitions prompt differs |
| **Definitions format** | The template's `# Tools` block: one `<tool>{function JSON}</tool>` line per tool inside `<tools>`, then the version's calling instructions. The function JSON is compact with sorted keys, as for Qwen |
| **Parse format, M2** | `<invoke name="…">` blocks of `<parameter name="…">value</parameter>` tags, inside a `minimax:tool_call` block. Names may be double-quoted, single-quoted or bare. A value ends at its closing tag or at the next parameter tag; an unterminated last parameter is dropped |
| **Parse format, M3** | Every structural tag starts with the namespace marker (`MINIMAX_M3_NAMESPACE`, `MiniMaxM3Namespace` in Go). Invokes count only after the marker's `tool_call` opener. Parameters are elements named after the argument and may nest: a nested element becomes an object, a repeated name becomes a list (list items arrive as `item` elements), and text mixed with child elements is kept under `$text`. Parsing an invoke stops at the first malformed element and keeps the parameters before it |
| **Argument values** | Not coerced to the tool schema, which the formatter does not see at parse time: every leaf value is a string (`"3"`, not `3`), as vLLM returns without a request schema. Coerce by schema in the tool |
| **Result format** | The result text inside `<response>` (see [Tool result serialization](#tool-result-serialization)); a failure is `{"error": <message>}` |
| **Validate** | Fails on an unterminated invoke, an invoke without a name, an unterminated last M2 parameter, a malformed M3 element, a tool-call block with no invoke, and an M3 invoke outside a `tool_call` block |
| **Reasoning, M2** | M2 emits the plain think pair (`THINK_OPEN`/`THINK_CLOSE`), but the chat template writes the opening tag into the generation prompt, so the response starts inside the block and carries only the close. Turn on `implicit_open` for MiniMax M2 (see below) |
| **Reasoning, M3** | M3 emits the mm:think pair (`MMTHINK_OPEN`/`MMTHINK_CLOSE`) inline in content. The adapter strips it by default; no option needed |

A response that contains the M3 namespace marker is parsed as M3; any other
is parsed as M2. Both versions parse both syntaxes. The earlier Go and Rust
patterns (`function_call: name(args)`, a `tool_call` block with `tool_name`
and `parameters` children, and a bare JSON array) are no longer parsed: no
MiniMax version emits them.

**MiniMax M2 reasoning (implicit open):** without the option, the leading
close tag is a stray close and the adapter fails closed. Turn it on when the
model is MiniMax M2:

| Port | Adapter with `implicit_open` |
|---|---|
| Python | `ReasoningAdapter(ReasoningOptions(implicit_open=True))` |
| Go | `reasoning.New(reasoning.Options{ImplicitOpen: true})` |
| TypeScript | `new ReasoningAdapter({ implicitOpen: true })` |
| Rust | `reasoning::Adapter::with_options(reasoning::Options { implicit_open: true, ..Default::default() })` |

**Example M2 tool call** (as the template renders an assistant turn):

```text
<minimax:tool_call>
<invoke name="get_weather">
<parameter name="location">Tokyo</parameter>
<parameter name="unit">celsius</parameter>
</invoke>
</minimax:tool_call>
```

parses to `get_weather` with `{"location": "Tokyo", "unit": "celsius"}`. The
same call from M3 writes `<invoke name="get_weather">`, `<location>Tokyo`,
`</location>` and each other tag with the namespace marker in front; see the
"M3 call as the chat template renders it" case in the fixture.

---

## Implementation Matrix

| Component | Go | Python | TypeScript |
|-----------|----|--------|------------|
| `OpenAIFOrmatter` | `go/toolformat/openai.go` | `python/.../toolformat/formatter.py` | `typescript/src/toolformat/formatter.ts` |
| `AnthropicFormatter` | `go/toolformat/anthropic.go` | same | same |
| `QwenFormatter` | `go/toolformat/qwen.go` | same | same |
| `DeepSeekFormatter` | `go/toolformat/deepseek.go` | same | same |
| `GLMFormatter` | `go/toolformat/glm.go` | same | same |
| `get_formatter()` / `GetFormatter()` / `getFormatter()` | `go/toolformat/selector.go` | `formatter.py:get_formatter()` | `formatter.ts:getFormatter()` |
| Shared fixtures | `fixtures/toolformat/{family}-cases.json` | same | same |
| Reasoning fixtures | `fixtures/reasoning/{family}-cases.json` | same | same |
| Reasoning adapter | `go/reasoning/adapter.go` | `python/.../reasoning/adapter.py` | `typescript/src/reasoning/adapter.ts` |

All formatters share the same `ToolFormatter` interface (5 methods: `FormatToolDefinitions`,
`ParseToolCalls`, `FormatToolResult`, `ModelFamily`, `ValidateFormat`).

---

## Reasoning Adapter Recognition

The adapter's known field set (`knownReasoningFields` / `KNOWN_REASONING_FIELDS`):

```text
reasoning_content  (priority 1 — all families except Anthropic)
thinking           (priority 2 — Anthropic)
thinking_content   (priority 3 — Anthropic, alternative SDK naming)
reasoning          (priority 4 — some model-specific naming)
```

Arbitrary additional field names can be registered per model via
`RegisterModelField` / `register_model_field`.

### Reasoning-tag constants

Inline reasoning tags are defined once per port, and both the reasoning
adapter and the guardrails response validator use those constants, so the two
stripping layers recognize the same tags. Matching is case-insensitive.

| Pair | Go (`reasoning`) | Python (`haikeilabs.agentware.reasoning`) | TypeScript (`@haikeilabs/agentware`) | Rust (`reasoning`) |
| --- | --- | --- | --- | --- |
| think: Qwen3, QwQ, DeepSeek-R1 and GLM served without a reasoning parser | `ThinkOpen` / `ThinkClose` | `THINK_OPEN` / `THINK_CLOSE` | `THINK_OPEN` / `THINK_CLOSE` | `THINK_OPEN` / `THINK_CLOSE` |
| mm:think: MiniMax M3 emits inline in content | `MMThinkOpen` / `MMThinkClose` | `MMTHINK_OPEN` / `MMTHINK_CLOSE` | `MMTHINK_OPEN` / `MMTHINK_CLOSE` | `MMTHINK_OPEN` / `MMTHINK_CLOSE` |
| thinking: generic, llama.cpp/o1-style | `ThinkingOpen` / `ThinkingClose` | `THINKING_OPEN` / `THINKING_CLOSE` | `THINKING_OPEN` / `THINKING_CLOSE` | `THINKING_OPEN` / `THINKING_CLOSE` |
| bracket THINK: Mistral/Magistral, DeepSeek text mode | `BracketThinkOpen` / `BracketThinkClose` | `BRACKET_THINK_OPEN` / `BRACKET_THINK_CLOSE` | `BRACKET_THINK_OPEN` / `BRACKET_THINK_CLOSE` | `BRACKET_THINK_OPEN` / `BRACKET_THINK_CLOSE` |
| every pair, in this order (think, mm:think, thinking, bracket THINK) | `TagPairs()` (`[]TagPair`) | `REASONING_TAG_PAIRS` (tuple of `(open, close)`) | `REASONING_TAG_PAIRS` (frozen `ReasoningTagPair[]`) | `REASONING_TAG_PAIRS` (`[TagPair; 4]`) |

The adapter strips these blocks from text content (an unbalanced tag is a
fail-closed error). The guardrails response validator strips the same blocks,
across newlines, before its tool-call rescue. Both stripping layers are
idempotent. The shared fixtures `fixtures/reasoning/*-cases.json` (adapter)
and `fixtures/reasoning/validator-rescue.v1.json` (validator) keep every port
in agreement.

Close-tag-only reasoning (MiniMax M2): MiniMax M2 emits the closing tag
without the opening one, so a stray close tag is not always malformed input.
By default the adapter stays fail-closed on it. Opting in with the
`implicit_open` reasoning option (`ReasoningOptions.implicit_open` in Python,
`Options{ImplicitOpen}` in Go, `implicitOpen` in TypeScript, `implicit_open`
in Rust; default false) makes everything before a leading think or mm:think
close tag count as reasoning. Only those two closes may open implicitly, and
only once per output: every other close tag (e.g. thinking) and a second
stray close stays unbalanced and fails closed.

The tags are a blind spot for reasoning-model workers: a model that emits
them cannot reliably read or type them. Each constant is built from parts, and
no source file, test, fixture or doc types a reasoning tag literally (fixtures
write the `<` as the JSON escape `\u003c`). Use the constants, or build a tag
from parts.

See `docs/reasoning-adapter.md` for the full adapter contract.
