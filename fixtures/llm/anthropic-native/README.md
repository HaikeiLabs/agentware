# Native Anthropic message fixtures

`cases.json` contains synthetic Messages API content blocks, tool definitions,
and their expected continuation messages. Signed and redacted thinking are
opaque and must round-trip unchanged, in order, alongside their tool use.
Tool results are user content blocks. Each SDK replays every case.
