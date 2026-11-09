# OpenAI-compatible provider extras parity

| Feature | TypeScript | Python | Go | Rust |
|---|---|---|---|---|
| Per-tool-call opaque `metadata` round-trips OpenAI-compatible `extra_content` | Supported | Supported | Supported | Supported |
| Anthropic native tools and signed/redacted thinking blocks | Supported | Supported | Supported | Supported |

Each backend reads `extra_content` from its individual tool call and emits it
on the same call in assistant history. Missing metadata stays absent. The
shared cases in `fixtures/llm/provider-extras/` exercise Gemini-style thought
signatures, other opaque provider data, and calls without metadata. Metadata
is model-family tool-call state, not reasoning display; do not log it.

The native Anthropic formatters map declarations to `input_schema`, `tool_use`
blocks to tool calls, and tool results to user `tool_result` blocks. They keep
ordered surrounding blocks in the corresponding call's Anthropic metadata and
restore them in assistant history. Pass system instructions separately as the
Messages API's top-level `system` parameter. These are native-block formatting
functions; callers provide the Anthropic HTTP transport and request envelope.
Fixtures live in `fixtures/llm/anthropic-native/`.
