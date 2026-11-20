"""Executor - Agent inference loop."""

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Protocol

from ..llm import Backend, Message
from ..llm.backend import accepts_tools
from ..llm.request import Role, ToolDefinition
from ..middleware import CallerContext
from ..toolformat import ToolFormatter
from ..tools import ToolRegistry

logger = logging.getLogger(__name__)


class TerminationReason(str, Enum):
    """Reason for executor termination."""

    COMPLETE = "complete"
    MAX_ITERATIONS = "max_iterations"
    ERROR = "error"
    CANCELED = "canceled"


@dataclass
class ExecuteRequest:
    """Input to a single agent run."""

    system_prompt: str
    user_message: str
    history: list[Message] = field(default_factory=list)
    max_iterations: int = 0
    caller_ctx: CallerContext = field(default_factory=CallerContext)
    job_id: str = ""


@dataclass
class ExecuteResult:
    """Output of a completed agent run."""

    final_response: str
    iterations: int
    tool_calls_made: int
    termination_reason: TerminationReason
    conversation: list[Message]


class Executor(Protocol):
    """Protocol for executors."""

    def execute(self, req: ExecuteRequest) -> ExecuteResult:
        """Run the inference loop for a task."""
        ...


@dataclass
class InferenceExecutorConfig:
    """Configuration for InferenceExecutor."""

    backend: Backend
    registry: ToolRegistry
    tool_executor: Any
    formatter: ToolFormatter
    max_iterations: int = 20
    completion_signal: str = "TASK_COMPLETE"


class InferenceExecutor:
    """Standard executor implementation."""

    def __init__(self, config: InferenceExecutorConfig) -> None:
        self._config = config

    def _llm_tools(self) -> list[ToolDefinition]:
        """Map the registry's tools to llm tool definitions (mirrors #300)."""
        tools: list[ToolDefinition] = []
        for tool in self._config.registry.all():
            schema: dict[str, Any] = {}
            schema_fn = getattr(tool, "input_schema", None)
            if callable(schema_fn):
                schema = schema_fn()
            tools.append(
                ToolDefinition(name=tool.name, description=tool.description, input_schema=schema)
            )
        return tools

    def execute(self, req: ExecuteRequest) -> ExecuteResult:
        """Run the inference loop."""
        # Cross-language contract: the system prompt leads the conversation,
        # ahead of any caller-supplied history (mirrors Go buildConversation).
        conversation = [Message(role=Role.SYSTEM, content=req.system_prompt)]
        conversation.extend(req.history)
        conversation.append(Message(role=Role.USER, content=req.user_message))

        iterations = 0
        tool_calls_made = 0
        final_response = ""

        max_iters = req.max_iterations or self._config.max_iterations

        # Pass tools only to backends that accept them; older third-party
        # backends keep running without native tools (one warning, not per
        # completion), mirroring run_inference.
        tools = self._llm_tools()
        supports_tools = accepts_tools(self._config.backend)
        if tools and not supports_tools:
            logger.warning(
                "backend %s does not accept a tools parameter; "
                "native tool calling is disabled for: %s",
                self._config.backend.model_name(),
                ", ".join(t.name for t in tools),
            )

        while iterations < max_iters:
            response = (
                self._config.backend.complete(conversation, tools=tools)
                if supports_tools
                else self._config.backend.complete(conversation)
            )

            if not response.tool_calls:
                final_response = response.content
                break

            conversation.append(Message(role=Role.ASSISTANT, content=response.content, tool_calls=response.tool_calls))
            tool_calls_made += len(response.tool_calls)

            for tool_call in response.tool_calls:
                tool_name = tool_call.name
                tool_args = tool_call.arguments

                result, success, error = self._config.tool_executor.execute(
                    tool_name, tool_args, req.caller_ctx
                )

                conversation.append(
                    Message(
                        role=Role.TOOL,
                        content=f"Tool {tool_name} result: {result if success else error}",
                        tool_call_id=tool_call.id,
                    )
                )

            iterations += 1

        if iterations >= max_iters:
            termination = TerminationReason.MAX_ITERATIONS
        elif final_response:
            termination = TerminationReason.COMPLETE
        else:
            termination = TerminationReason.ERROR

        return ExecuteResult(
            final_response=final_response,
            iterations=iterations,
            tool_calls_made=tool_calls_made,
            termination_reason=termination,
            conversation=conversation,
        )
