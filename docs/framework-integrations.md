# Framework integrations

Agentware wraps tool calls in other agent frameworks through adapters; this page lists what is in the repo today and its status.

| Framework | Language | Path | What it wraps | Tests | Packaging |
| --- | --- | --- | --- | --- | --- |
| Google ADK-style hooks | Go | go/adapters/adk | Before and after tool-call callbacks around Agentware middleware. It does not import the Google ADK SDK. | go/adapters/adk/adapter_test.go | Main Go module |
| Hermes | Go | go/adapters/hermes | Runs tool calls through a Hermes agent client | go/adapters/hermes/adapter_test.go | Main Go module |
| Hermes | Python | python/adapters/hermes | Runs the Hermes CLI as a subprocess for tool calls | python/tests/adapters/test_hermes.py | Separate package in the repo, not published |
| Pydantic AI | Python | python/adapters/pydantic | Runs tool calls through a Pydantic AI agent | python/tests/adapters/test_pydantic.py | Separate package in the repo, not published |
| Kitaru | Python | python/adapters/kitaru | Maps tool names to Kitaru flows over REST | python/tests/adapters/test_kitaru.py | Separate package in the repo, not published |
| LangGraph | Python | python/src/haikeilabs/agentware/memory/langgraph.py | Memory tools for a LangGraph agent (not tool-call middleware) | none | Main package (haikeilabs-agentware) |

## Not supported yet

Strands Agents, and any framework not in the table. Open an issue or ask in [Discord](https://discord.gg/g6QaTYWXDa).

## Wrapping a framework yourself

Follow the "Middleware first" section in python/README.md: wrap the framework's tool function with `AuditedToolClient`.
