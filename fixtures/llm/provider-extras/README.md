# OpenAI-compatible tool-call provider metadata

`cases.json` pairs a recorded-style response with the assistant tool calls to
send on the next request. `extra_content` is opaque and belongs to its own tool
call. Preserve it exactly when present; omit the key when absent. Values are
synthetic, not credentials. Every language replays these same cases.
