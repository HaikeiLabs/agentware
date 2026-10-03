# Model Format Reference

The five supported model families and their tool-call / reasoning formats.
Each family has a corresponding `ToolFormatter` in Go, Python, and TypeScript,
and the reasoning adapter handles its reasoning shape.

---

## Quick Reference

| Family | Tool Definitions | Tool Calls (response) | Tool Result | Reasoning Field | Thinking Tags |
|--------|------------------|----------------------|-------------|-----------------|---------------|
| **OpenAI** | JSON function array (Generic) | JSON array: `{name, arguments}` | JSON | `reasoning_content` | none |
| **Anthropic** | JSON function array (Generic) | XML: `<invoke tool="name">JSON</invoke>` | XML: `<result tool="name">` | `thinking` (content block) | none |
| **Qwen** | XML: `<tool_description><tool_name>` | XML: `<tool_call><tool name="name">JSON</tool>` | XML: `<tool_response>` | `reasoning_content` | `<thinking>...</thinking>` |
| **DeepSeek** | JSON function array (Generic) | JSON array: `{name, arguments}` | JSON | `reasoning_content` | `[THINK]...[/THINK]` |
| **GLM** | JSON function array (Generic) | JSON array: `{name, arguments}` | JSON | `reasoning_content` | none |

---

## Per-Family Details

### OpenAI (`OpenAIFOrmatter` / `OpenAIFOrmatter` / `OpenAIFOrmatter`)

| | Detail |
|---|---|
| **Model names** | `gpt-*`, `o1-*`, `o3-*` |
| **Definitions format** | OpenAI-compatible JSON array of `{type: "function", function: {name, description, parameters}}` (via `GenericFormatter`) |
| **Parse format** | JSON array of `{id, name, arguments}` |
| **Result format** | JSON-encoded string of result data |
| **Validate** | Validates JSON array, requires `name` on each entry |
| **Reasoning field** | `reasoning_content` — top-level field on the choice message |
| **Inline tags** | None. o-series models do not use inline thinking tags. |
| **Adapter** | No model-specific field registration needed; `reasoning_content` is in the known set. |

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

### Anthropic (`AnthropicFormatter` / `AnthropicFormatter` / `AnthropicFormatter`)

| | Detail |
|---|---|
| **Model names** | `claude-*`, `anthropic-*` |
| **Definitions format** | OpenAI-compatible JSON array (same as `GenericFormatter`) |
| **Parse format** | XML: `<function_calls><invoke tool="name">JSON args</invoke></function_calls>` |
| **Result format** | XML: `<function_results><result tool="name">output</result></function_results>` |
| **Validate** | Validates `<invoke>` tags have valid JSON arguments and non-empty names |
| **Reasoning field** | `thinking` — a separate content block of `type: "thinking"` in the Messages API response |
| **Inline tags** | None. Extended thinking is a structured content block, not inline. |
| **Adapter** | `thinking` is in the known field set. No registration needed for Claude. |

**Example response (Messages API):**
```json
{
  "content": [
    {"type": "thinking", "thinking": "I need to call get_weather...", "signature": "..."},
    {"type": "tool_use", "name": "get_weather", "input": {"location": "Tokyo"}, "id": "toolu_..."}
  ]
}
```

**Example response (text with function_calls):**
```
<function_calls>
<invoke tool="get_weather">{"location": "Tokyo", "unit": "celsius"}</invoke>
</function_calls>
```

### Qwen (`QwenFormatter` / `QwenFormatter` / `QwenFormatter`)

| | Detail |
|---|---|
| **Model names** | `qwen-*`, `qwq-*` |
| **Definitions format** | XML: `<tool_description><tool_name>name</tool_name><description>...</description><parameters>...</parameters></tool_description>` |
| **Parse format** | XML: `<tool_call><tool name="name">JSON args</tool></tool_call>` |
| **Result format** | XML: `<tool_response><tool_name>name</tool_name><result>output</result></tool_response>` |
| **Validate** | Validates `<tool_call>` tags have valid JSON arguments and non-empty names |
| **Reasoning field** | `reasoning_content` — top-level field on the choice message |
| **Inline tags** | `<thinking>...</thinking>` — may appear in the content text alongside tool calls |
| **Adapter** | Both `reasoning_content` and `<thinking>` tags are handled generically. |

**Example response:**
```json
{
  "choices": [{
    "message": {
      "content": "<thinking>Need weather data</thinking>\n<tool_call><tool name=\"get_weather\">{\"location\":\"Tokyo\"}</tool></tool_call>",
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
| **Result format** | JSON-encoded string of result data |
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
```
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
| **Result format** | JSON-encoded string of result data |
| **Validate** | Validates JSON array, requires `name` on each entry |
| **Reasoning field** | `reasoning_content` — native field on the choice message |
| **Inline tags** | None. GLM models do not use inline thinking tags. |
| **Adapter** | `reasoning_content` is in the known field set; no registration needed. |

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
```
reasoning_content  (priority 1 — all families except Anthropic)
thinking           (priority 2 — Anthropic)
thinking_content   (priority 3 — Anthropic, alternative SDK naming)
reasoning          (priority 4 — some model-specific naming)
```

Arbitrary additional field names can be registered per model via
`RegisterModelField` / `register_model_field`.

Inline thinking tags recognized by the adapter:
- `<thinking>...</thinking>` — stripped from text content
- `[THINK]...[/THINK]` — stripped from text content

The guardrails response validator additionally strips `[THINK]...[/THINK]` as a
secondary rescue mechanism. Both stripping layers are idempotent.

See `docs/reasoning-adapter.md` for the full adapter contract.
