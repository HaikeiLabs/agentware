"""Inference loop wiring guardrails together."""

import asyncio
from dataclasses import dataclass, field
from typing import Any

from haikeilabs.agentware.llm import Message, Role
from haikeilabs.agentware.llm.backend import Backend
from haikeilabs.agentware.llm.request import ToolDefinition
from haikeilabs.agentware.llm.response import Response
from haikeilabs.agentware.llm.response import ToolCall as LLMToolCall
from haikeilabs.agentware.llmcontext.context_window import ContextWindowManager
from haikeilabs.agentware.middleware.guardrails.error_tracker import ErrorCategory, ErrorTracker
from haikeilabs.agentware.middleware.guardrails.response_validator import (
    ResponseValidator,
    ValidationResult,
)
from haikeilabs.agentware.middleware.guardrails.response_validator import (
    ToolCall as GuardrailsToolCall,
)
from haikeilabs.agentware.middleware.guardrails.step_enforcer import StepEnforcer
from haikeilabs.agentware.middleware.types import MessageMeta, MessageType
from haikeilabs.agentware.reasoning import ContextTree, ReasoningAdapter, ReasoningError


class RetriesExhaustedError(Exception):
    """Raised when inference attempts are exhausted."""

    pass


# default_adapter strips and normalizes reasoning in the inference loop.
# Callers can supply a tuned adapter (limits, registered model fields) via
# InferenceConfig.reasoning.
default_adapter = ReasoningAdapter()


@dataclass
class InferenceResult:
    """Result of an inference call."""

    response: Response
    new_messages: list[Message]
    tool_call_counter: int
    attempts: int
    reasoning_tree: ContextTree | None = None


@dataclass
class InferenceConfig:
    """Configuration for the inference loop."""

    client: Backend
    context_manager: ContextWindowManager | None = None
    validator: ResponseValidator | None = None
    error_tracker: ErrorTracker | None = None
    step_enforcer: StepEnforcer | None = None
    tool_specs: list[ToolDefinition] = field(default_factory=list)
    max_attempts: int = 10
    step_index: int = 0
    reasoning: ReasoningAdapter | None = None


def _get_tool_names(specs: list[ToolDefinition]) -> list[str]:
    """Extract tool names from tool specifications."""
    return [spec.name for spec in specs]


async def run_inference(
    messages: list[Message],
    cfg: InferenceConfig,
    session_id: str = "",
) -> InferenceResult | None:
    """Run inference loop with guardrails.

    Args:
        messages: Current conversation messages.
        cfg: Inference configuration.
        session_id: Session identifier for step enforcer and error tracker.

    Returns:
        InferenceResult on success, None if all attempts exhausted without valid response.

    Raises:
        RetriesExhaustedError: When max attempts are reached without a valid response.
    """
    if cfg.max_attempts <= 0:
        cfg.max_attempts = 3

    current_messages = list(messages)

    last_response: Response | None = None
    tool_call_counter = 0
    attempts = 0

    while attempts < cfg.max_attempts:
        attempts += 1

        if cfg.context_manager is not None:
            if cfg.context_manager.should_compact(current_messages):
                compacted = cfg.context_manager.compact(current_messages)
                current_messages = compacted

            warning = cfg.context_manager.check_thresholds(current_messages)
            if warning:
                warning_msg = Message(
                    role=Role.USER,
                    content=warning,
                    meta=MessageMeta(type=MessageType.CONTEXT_WARNING),
                )
                current_messages.append(warning_msg)

        tool_specs_serialized = []
        for spec in cfg.tool_specs:
            tool_specs_serialized.append(
                {
                    "type": "function",
                    "function": {
                        "name": spec.name,
                        "description": spec.description,
                        "parameters": spec.input_schema,
                    },
                }
            )

        try:
            resp = await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: cfg.client.complete(current_messages),
            )
        except Exception as e:
            if cfg.error_tracker is not None:
                cfg.error_tracker.record_error(
                    session_id,
                    "",
                    {},
                    e,
                    ErrorCategory.UNKNOWN,
                )
            raise

        if cfg.context_manager is not None and resp.usage_tokens.total_tokens > 0:
            cfg.context_manager.update_token_count(resp.usage_tokens.total_tokens)
        validation_result: ValidationResult | None = None

        # AR-1: normalize reasoning and strip it before ordinary tool-call
        # parsing. Reasoning can arrive as a native reasoning_content field or
        # as embedded thinking tags in content; both are combined into a
        # synthetic structured input so the adapter sees the full picture.
        # Malformed or unbounded reasoning fails closed: this turn is treated
        # as invalid and retried rather than parsed partially.
        reasoning_tree: ContextTree | None = None
        clean_content = resp.content
        reasoning_failed = False
        if resp.content or resp.reasoning:
            reasoning_input: dict[str, Any] = {"content": resp.content}
            if resp.reasoning:
                reasoning_input["reasoning_content"] = resp.reasoning
            r_adapter = cfg.reasoning or default_adapter
            try:
                reasoning_tree = r_adapter.extract(
                    reasoning_input,
                    cfg.client.model_name(),
                    "llm-backend",
                )
                clean_content = r_adapter.strip(reasoning_input)
            except ReasoningError:
                reasoning_failed = True
                # Fail closed: never echo content that may carry reasoning we
                # could not parse.
                clean_content = ""
                validation_result = ValidationResult(
                    tool_calls=[],
                    nudge=None,
                    needs_retry=True,
                )

        if not reasoning_failed:
            if resp.tool_calls:
                guardrails_tool_calls = [
                    GuardrailsToolCall(tool=tc.name, args=tc.arguments) for tc in resp.tool_calls
                ]
                if cfg.validator:
                    validation_result = cfg.validator.validate_tool_calls(guardrails_tool_calls)
                else:
                    validation_result = ValidationResult(
                        tool_calls=guardrails_tool_calls, nudge=None, needs_retry=False
                    )
            elif clean_content:
                if cfg.validator:
                    validation_result = cfg.validator.validate_text_response(clean_content)
                else:
                    validation_result = ValidationResult(
                        tool_calls=[], nudge=None, needs_retry=False
                    )

                if not validation_result.needs_retry and validation_result.tool_calls:
                    resp.tool_calls = [
                        LLMToolCall(id="", name=tc.tool, arguments=tc.args)
                        for tc in validation_result.tool_calls
                    ]
            else:
                if cfg.validator:
                    validation_result = cfg.validator.validate_text_response("")
                else:
                    validation_result = ValidationResult(
                        tool_calls=[],
                        nudge=None,
                        needs_retry=True,
                    )

        last_response = resp

        if validation_result and not validation_result.needs_retry:
            if cfg.error_tracker is not None:
                cfg.error_tracker.reset_session(session_id)

            if cfg.step_enforcer is not None and resp.tool_calls:
                for tc in resp.tool_calls:
                    allowed, missing = cfg.step_enforcer.can_execute(session_id, tc.name)
                    if not allowed:
                        from haikeilabs.agentware.middleware.guardrails.nudge import step_nudge

                        nudge = step_nudge(tc.name, missing, 1)
                        nudge_msg = Message(
                            role=Role.USER,
                            content=nudge.content,
                            meta=MessageMeta(type=MessageType.STEP_NUDGE),
                        )
                        current_messages.append(nudge_msg)
                        continue

            tool_call_counter += len(resp.tool_calls) if resp.tool_calls else 0
            return InferenceResult(
                response=last_response,
                new_messages=current_messages,
                tool_call_counter=tool_call_counter,
                attempts=attempts,
                reasoning_tree=reasoning_tree,
            )

        if cfg.error_tracker is not None:
            cfg.error_tracker.record_error(
                session_id,
                "",
                {},
                Exception("validation failed"),
                ErrorCategory.UNKNOWN,
            )

        if attempts >= cfg.max_attempts:
            raise RetriesExhaustedError(f"retries exhausted after {attempts} attempts")

        if validation_result and validation_result.nudge:
            nudge_msg = Message(
                role=Role.USER,
                content=validation_result.nudge.content,
                meta=MessageMeta(type=MessageType.RETRY_NUDGE),
            )
            current_messages.append(nudge_msg)

        failed_msg = Message(
            role=Role.ASSISTANT,
            content=clean_content,
            meta=MessageMeta(type=MessageType.TEXT_RESPONSE),
        )
        current_messages.append(failed_msg)

    raise RetriesExhaustedError(f"retries exhausted after {attempts} attempts")
