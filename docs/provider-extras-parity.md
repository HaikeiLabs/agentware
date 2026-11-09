# OpenAI-compatible provider extras parity

| Feature | TypeScript | Python | Go | Rust |
|---|---|---|---|---|
| Per-tool-call opaque `metadata` round-trips OpenAI-compatible `extra_content` | Supported | Supported | Supported | Supported |

Each backend reads `extra_content` from its individual tool call and emits it
on the same call in assistant history. Missing metadata stays absent. The
shared cases in `fixtures/llm/provider-extras/` exercise Gemini-style thought
signatures, other opaque provider data, and calls without metadata. Metadata
is model-family tool-call state, not reasoning display; do not log it.
