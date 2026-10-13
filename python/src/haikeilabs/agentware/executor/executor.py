"""Executor - Agent inference loop."""

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Protocol

from ..llm import Backend, Message
from ..llm.request import Role
from ..middleware import CallerContext
from ..toolformat import ToolFormatter
from ..tools import ToolRegistry


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

        while iterations < max_iters:
            response = self._config.backend.complete(conversation)

            if not response.tool_calls:
                final_response = response.content
                break

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
