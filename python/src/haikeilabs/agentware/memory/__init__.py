"""Wiki memory - client for the Go core's ontology-constrained wiki memory.

WikiMemory spawns the Go MCP stdio server (``memctl serve``) scoped to one
user and exposes the enforced memory tools. It implements the SDK's
ToolExecutor protocol, so it composes into MiddlewareImpl like any other
executor.
"""

from .client import (
    MEMORY_TOOLS,
    DiagnosticViolation,
    MemoryServerError,
    WikiMemory,
    parse_diagnostics,
)
from .langgraph import LangGraphMemoryTools

__all__ = [
    "WikiMemory",
    "MemoryServerError",
    "DiagnosticViolation",
    "parse_diagnostics",
    "MEMORY_TOOLS",
    "LangGraphMemoryTools",
]
