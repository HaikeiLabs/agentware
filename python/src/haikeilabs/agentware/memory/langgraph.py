"""LangGraph integration: expose wiki-memory tools to a LangGraph agent.

LangGraph accepts plain Python callables as tools (it introspects name,
docstring, and signature). ``LangGraphMemoryTools`` builds one callable per
memory tool bound to a WikiMemory client, so wrapping an agent is::

    memory = WikiMemory(user_id="alice", root=..., tbox_paths=[...])
    tools = LangGraphMemoryTools(memory).tools()
    graph = create_react_agent(model, tools=[*agent_tools, *tools])

Denied writes return the deny reason (including the structured diagnostics
payload) as the tool output, so the agent can self-correct and retry.
"""

from collections.abc import Callable
from typing import Any

from .client import WikiMemory


class LangGraphMemoryTools:
    """Builds LangGraph-compatible tool callables for a WikiMemory client."""

    def __init__(self, memory: WikiMemory) -> None:
        self._memory = memory

    def tools(self) -> list[Callable[..., str]]:
        """Return the memory tools as plain callables."""
        memory = self._memory

        def memory_ingest(source_id: str, text: str) -> str:
            """Store a raw source document in the user's memory vault."""
            return _render(memory.ingest(source_id, text))

        def memory_write_page(content: str) -> str:
            """Create or update an ontology-validated wiki page.

            content must be full page markdown with YAML frontmatter per the
            memory schema. On DENY the returned text includes structured
            diagnostics — fix the page and retry.
            """
            return _render(memory.write_page(content))

        def memory_query(question: str) -> str:
            """Query the user's wiki memory."""
            return _render(memory.query(question))

        def memory_get_claims(page_id: str = "") -> str:
            """List claims tracked in the user's vault, optionally per page."""
            return _render(memory.get_claims(page_id or None))

        def memory_lint() -> str:
            """Report contract, ontology, and graph issues in the vault."""
            return _render(memory.lint())

        return [
            memory_ingest,
            memory_write_page,
            memory_query,
            memory_get_claims,
            memory_lint,
        ]


def _render(result: tuple[Any, bool, str]) -> str:
    output, ok, err = result
    if not ok:
        return f"DENIED: {err}"
    if isinstance(output, str):
        return output
    import json

    return json.dumps(output, indent=2)
