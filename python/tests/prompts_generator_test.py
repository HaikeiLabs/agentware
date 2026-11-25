"""Prompt generator tool schemas: attribute-style and method-style input_schema.

The generator, the registry and the executor must read a tool's JSON schema
the same way, whether the tool declares it as a dict attribute or as a
zero-argument method.
"""

import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from haikeilabs.agentware.executor.executor import (
    ExecuteRequest,
    InferenceExecutor,
    InferenceExecutorConfig,
)
from haikeilabs.agentware.llm.request import ToolDefinition
from haikeilabs.agentware.llm.response import Response
from haikeilabs.agentware.prompts.generator import DefaultPromptGenerator
from haikeilabs.agentware.tools import ToolRegistry

SCHEMA_LIMIT = {"type": "object", "properties": {"limit": {"type": "integer"}}}
SCHEMA_QUERY = {"type": "object", "properties": {"query": {"type": "string"}}}


class AttrSchemaTool:
    """Tool that declares its JSON schema as a plain dict attribute."""

    name = "attr_tool"
    description = "attribute-style schema tool"

    def __init__(self) -> None:
        self.input_schema: dict[str, Any] = SCHEMA_LIMIT

    def execute(self, args: dict[str, Any]) -> Any:
        return None


class MethodSchemaTool:
    """Tool that declares its JSON schema via a zero-argument method."""

    name = "method_tool"
    description = "method-style schema tool"

    def input_schema(self) -> dict[str, Any]:
        return SCHEMA_QUERY

    def execute(self, args: dict[str, Any]) -> Any:
        return None


class NoSchemaTool:
    """Tool that declares no schema at all."""

    name = "no_schema_tool"
    description = "schema-less tool"

    def execute(self, args: dict[str, Any]) -> Any:
        return None


class NoopFormatter:
    """Formatter stub; the loop under test never formats tool calls."""

    def format_tool_definitions(self, tools: list[Any]) -> str:
        return ""

    def parse_tool_calls(self, response: str) -> list[Any]:
        return []

    def format_tool_result(self, name: str, result: Any) -> str:
        return ""

    def model_family(self) -> str:
        return "generic"


class RecordingBackend:
    """Records the tool definitions it is given, like the executor tests."""

    def __init__(self) -> None:
        self.tools_seen: list[list[ToolDefinition] | None] = []

    def complete(self, messages: list[Any], tools: Any = None) -> Response:
        self.tools_seen.append(tools)
        return Response(content="done")

    def supports_native_tool_calling(self) -> bool:
        return True

    def model_name(self) -> str:
        return "mock"

    def context_window_size(self) -> int:
        return 0


def _registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(AttrSchemaTool())
    registry.register(MethodSchemaTool())
    registry.register(NoSchemaTool())
    return registry


def test_generate_tool_schemas_serializes_both_schema_styles() -> None:
    """The schema block must JSON-serialize with the real schema per tool."""
    schemas = DefaultPromptGenerator().generate_tool_schemas(_registry())

    # A method-style schema previously put a bound method in `parameters`,
    # which raised TypeError here.
    dumped = json.dumps(schemas)
    assert json.loads(dumped) == schemas

    by_name = {
        entry["function"]["name"]: entry["function"]["parameters"] for entry in schemas
    }
    assert by_name["attr_tool"] == SCHEMA_LIMIT
    assert by_name["method_tool"] == SCHEMA_QUERY
    assert by_name["no_schema_tool"] == {}


def test_registry_schemas_returns_both_schema_styles() -> None:
    """ToolRegistry.schemas must accept dict attributes and methods alike."""
    assert _registry().schemas() == {
        "attr_tool": SCHEMA_LIMIT,
        "method_tool": SCHEMA_QUERY,
    }


def test_executor_sends_both_schema_styles_to_backend() -> None:
    """Native tool calls carry every declared schema, never a silent {}."""
    backend = RecordingBackend()
    executor = InferenceExecutor(
        InferenceExecutorConfig(
            backend=backend,
            registry=_registry(),
            tool_executor=None,
            formatter=NoopFormatter(),
            max_iterations=5,
        )
    )

    executor.execute(ExecuteRequest(system_prompt="sys", user_message="go"))

    assert backend.tools_seen == [
        [
            ToolDefinition(
                name="attr_tool",
                description="attribute-style schema tool",
                input_schema=SCHEMA_LIMIT,
            ),
            ToolDefinition(
                name="method_tool",
                description="method-style schema tool",
                input_schema=SCHEMA_QUERY,
            ),
            ToolDefinition(
                name="no_schema_tool",
                description="schema-less tool",
                input_schema={},
            ),
        ]
    ]
