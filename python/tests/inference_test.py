"""Integration tests for run_inference."""

from dataclasses import dataclass

import pytest

from haikeilabs.agentware.llm import Message, Role
from haikeilabs.agentware.llm.request import ToolDefinition
from haikeilabs.agentware.llm.response import Response, TokenUsage
from haikeilabs.agentware.llm.response import ToolCall as LLMToolCall
from haikeilabs.agentware.llmcontext.context_window import ContextWindowManager
from haikeilabs.agentware.middleware.guardrails.error_tracker import ErrorTracker
from haikeilabs.agentware.middleware.guardrails.response_validator import ResponseValidator
from haikeilabs.agentware.middleware.guardrails.step_enforcer import StepEnforcer
from haikeilabs.agentware.middleware.inference import (
    InferenceConfig,
    RetriesExhaustedError,
    run_inference,
)


@dataclass
class MockBackend:
    """Mock backend for testing."""

    responses: list[Response]
    call_count: int = 0

    def complete(self, messages: list[Message]) -> Response:
        """Return sequential mock responses."""
        if self.call_count >= len(self.responses):
            return self.responses[-1]
        resp = self.responses[self.call_count]
        self.call_count += 1
        return resp

    def supports_native_tool_calling(self) -> bool:
        return True

    def model_name(self) -> str:
        return "mock"

    def context_window_size(self) -> int:
        return 8192


class TestRunInference:
    """Tests for run_inference function."""

    @pytest.mark.asyncio
    async def test_successful_inference_with_tool_call(self):
        """Test successful inference returning tool calls."""
        messages = [Message(role=Role.USER, content="test")]

        tool_def = ToolDefinition(
            name="test_tool",
            description="A test tool",
            input_schema={"type": "object", "properties": {}},
        )

        mock_resp = Response(
            content="",
            tool_calls=[LLMToolCall(id="1", name="test_tool", arguments={})],
            finish_reason="tool_calls",
            usage_tokens=TokenUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
        )

        backend = MockBackend(responses=[mock_resp])
        validator = ResponseValidator(tool_names=["test_tool"], rescue_enabled=False)

        cfg = InferenceConfig(
            client=backend,
            tool_specs=[tool_def],
            validator=validator,
            max_attempts=3,
        )

        result = await run_inference(messages, cfg)

        assert result is not None
        assert len(result.response.tool_calls) == 1
        assert result.response.tool_calls[0].name == "test_tool"
        assert result.attempts == 1

    @pytest.mark.asyncio
    async def test_successful_text_response_with_rescue(self):
        """Test text response successfully rescued to tool calls."""
        messages = [Message(role=Role.USER, content="test")]

        tool_def = ToolDefinition(
            name="test_tool",
            description="A test tool",
            input_schema={"type": "object", "properties": {}},
        )

        mock_resp = Response(
            content='{"tool": "test_tool", "args": {"key": "value"}}',
            tool_calls=[],
            finish_reason="stop",
            usage_tokens=TokenUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
        )

        backend = MockBackend(responses=[mock_resp])
        validator = ResponseValidator(tool_names=["test_tool"], rescue_enabled=True)

        cfg = InferenceConfig(
            client=backend,
            tool_specs=[tool_def],
            validator=validator,
            max_attempts=3,
        )

        result = await run_inference(messages, cfg)

        assert result is not None
        assert len(result.response.tool_calls) == 1

    @pytest.mark.asyncio
    async def test_retry_on_invalid_response(self):
        """Test retry logic on invalid response."""
        messages = [Message(role=Role.USER, content="test")]

        tool_def = ToolDefinition(
            name="test_tool",
            description="A test tool",
            input_schema={"type": "object", "properties": {}},
        )

        invalid_resp = Response(
            content="This is just text, not a tool call",
            tool_calls=[],
            finish_reason="stop",
            usage_tokens=TokenUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
        )

        valid_resp = Response(
            content="",
            tool_calls=[LLMToolCall(id="1", name="test_tool", arguments={})],
            finish_reason="tool_calls",
            usage_tokens=TokenUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
        )

        backend = MockBackend(responses=[invalid_resp, valid_resp])
        validator = ResponseValidator(tool_names=["test_tool"], rescue_enabled=False)

        cfg = InferenceConfig(
            client=backend,
            tool_specs=[tool_def],
            validator=validator,
            max_attempts=3,
        )

        result = await run_inference(messages, cfg)

        assert result is not None
        assert result.attempts == 2

    @pytest.mark.asyncio
    async def test_retries_exhausted_error(self):
        """Test RetriesExhaustedError is raised when max attempts exceeded."""
        messages = [Message(role=Role.USER, content="test")]

        tool_def = ToolDefinition(
            name="test_tool",
            description="A test tool",
            input_schema={"type": "object", "properties": {}},
        )

        invalid_resp = Response(
            content="This is just text, not a tool call",
            tool_calls=[],
            finish_reason="stop",
            usage_tokens=TokenUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
        )

        backend = MockBackend(responses=[invalid_resp])
        validator = ResponseValidator(tool_names=["test_tool"], rescue_enabled=False)

        cfg = InferenceConfig(
            client=backend,
            tool_specs=[tool_def],
            validator=validator,
            max_attempts=2,
        )

        with pytest.raises(RetriesExhaustedError):
            await run_inference(messages, cfg)

    @pytest.mark.asyncio
    async def test_context_compaction_triggered(self):
        """Test context compaction is triggered when needed."""
        messages = [Message(role=Role.USER, content="test")]

        tool_def = ToolDefinition(
            name="test_tool",
            description="A test tool",
            input_schema={"type": "object", "properties": {}},
        )

        mock_resp = Response(
            content="",
            tool_calls=[LLMToolCall(id="1", name="test_tool", arguments={})],
            finish_reason="tool_calls",
            usage_tokens=TokenUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
        )

        backend = MockBackend(responses=[mock_resp])
        validator = ResponseValidator(tool_names=["test_tool"], rescue_enabled=False)

        def custom_should_compact(messages: list[Message]) -> bool:
            return True

        ctx_manager = ContextWindowManager(context_window=1000)
        ctx_manager.should_compact = custom_should_compact
        ctx_manager.compact = lambda messages: messages[:1]

        cfg = InferenceConfig(
            client=backend,
            context_manager=ctx_manager,
            tool_specs=[tool_def],
            validator=validator,
            max_attempts=3,
        )

        result = await run_inference(messages, cfg)
        assert result is not None

    @pytest.mark.asyncio
    async def test_context_threshold_warning_injection(self):
        """Test context threshold warnings are injected."""
        messages = [Message(role=Role.USER, content="test")]

        tool_def = ToolDefinition(
            name="test_tool",
            description="A test tool",
            input_schema={"type": "object", "properties": {}},
        )

        mock_resp = Response(
            content="",
            tool_calls=[LLMToolCall(id="1", name="test_tool", arguments={})],
            finish_reason="tool_calls",
            usage_tokens=TokenUsage(prompt_tokens=10, completion_tokens=5, total_tokens=8000),
        )

        backend = MockBackend(responses=[mock_resp])
        validator = ResponseValidator(tool_names=["test_tool"], rescue_enabled=False)

        ctx_manager = ContextWindowManager(context_window=10000)

        cfg = InferenceConfig(
            client=backend,
            context_manager=ctx_manager,
            tool_specs=[tool_def],
            validator=validator,
            max_attempts=3,
        )

        result = await run_inference(messages, cfg)
        assert result is not None

    @pytest.mark.asyncio
    async def test_error_tracker_records_failures(self):
        """Test error tracker is updated on validation failures."""
        messages = [Message(role=Role.USER, content="test")]

        tool_def = ToolDefinition(
            name="test_tool",
            description="A test tool",
            input_schema={"type": "object", "properties": {}},
        )

        invalid_resp1 = Response(
            content="This is just text",
            tool_calls=[],
            finish_reason="stop",
            usage_tokens=TokenUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
        )

        invalid_resp2 = Response(
            content="Still not a tool call",
            tool_calls=[],
            finish_reason="stop",
            usage_tokens=TokenUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
        )

        valid_resp = Response(
            content="",
            tool_calls=[LLMToolCall(id="1", name="test_tool", arguments={})],
            finish_reason="tool_calls",
            usage_tokens=TokenUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
        )

        backend = MockBackend(responses=[invalid_resp1, invalid_resp2, valid_resp])
        validator = ResponseValidator(tool_names=["test_tool"], rescue_enabled=False)
        error_tracker = ErrorTracker(max_errors_per_tool=5)

        cfg = InferenceConfig(
            client=backend,
            tool_specs=[tool_def],
            validator=validator,
            error_tracker=error_tracker,
            max_attempts=5,
        )

        session_id = "test-session"

        result = await run_inference(messages, cfg, session_id=session_id)

        assert result is not None

    @pytest.mark.asyncio
    async def test_step_enforcer_blocks_premature_tools(self):
        """Test step enforcer blocks tools without prerequisites."""
        messages = [Message(role=Role.USER, content="test")]

        tool_def = ToolDefinition(
            name="final_tool",
            description="A final tool",
            input_schema={"type": "object", "properties": {}},
        )

        mock_resp = Response(
            content="",
            tool_calls=[LLMToolCall(id="1", name="final_tool", arguments={})],
            finish_reason="tool_calls",
            usage_tokens=TokenUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
        )

        backend = MockBackend(responses=[mock_resp])
        validator = ResponseValidator(tool_names=["final_tool", "step1"], rescue_enabled=False)
        step_enforcer = StepEnforcer()
        step_enforcer.add_step("final_tool", prerequisites=["step1"])

        cfg = InferenceConfig(
            client=backend,
            tool_specs=[tool_def],
            validator=validator,
            step_enforcer=step_enforcer,
            max_attempts=3,
        )

        result = await run_inference(messages, cfg, session_id="test-session")
        assert result is not None

    @pytest.mark.asyncio
    async def test_empty_response_triggers_retry(self):
        """Test empty response triggers retry."""
        messages = [Message(role=Role.USER, content="test")]

        tool_def = ToolDefinition(
            name="test_tool",
            description="A test tool",
            input_schema={"type": "object", "properties": {}},
        )

        empty_resp = Response(
            content="",
            tool_calls=[],
            finish_reason="stop",
            usage_tokens=TokenUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
        )

        valid_resp = Response(
            content="",
            tool_calls=[LLMToolCall(id="1", name="test_tool", arguments={})],
            finish_reason="tool_calls",
            usage_tokens=TokenUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
        )

        backend = MockBackend(responses=[empty_resp, valid_resp])
        validator = ResponseValidator(tool_names=["test_tool"], rescue_enabled=False)

        cfg = InferenceConfig(
            client=backend,
            tool_specs=[tool_def],
            validator=validator,
            max_attempts=3,
        )

        result = await run_inference(messages, cfg)

        assert result is not None
        assert result.attempts == 2

    @pytest.mark.asyncio
    async def test_max_attempts_respects_config(self):
        """Test max attempts is respected from config."""
        messages = [Message(role=Role.USER, content="test")]

        tool_def = ToolDefinition(
            name="test_tool",
            description="A test tool",
            input_schema={"type": "object", "properties": {}},
        )

        invalid_resp = Response(
            content="This is just text",
            tool_calls=[],
            finish_reason="stop",
            usage_tokens=TokenUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
        )

        backend = MockBackend(responses=[invalid_resp])
        validator = ResponseValidator(tool_names=["test_tool"], rescue_enabled=False)

        cfg = InferenceConfig(
            client=backend,
            tool_specs=[tool_def],
            validator=validator,
            max_attempts=5,
        )

        with pytest.raises(RetriesExhaustedError) as exc_info:
            await run_inference(messages, cfg)

        assert "5 attempts" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_default_max_attempts(self):
        """Test default max attempts is used when not specified."""
        messages = [Message(role=Role.USER, content="test")]

        tool_def = ToolDefinition(
            name="test_tool",
            description="A test tool",
            input_schema={"type": "object", "properties": {}},
        )

        invalid_resp = Response(
            content="This is just text",
            tool_calls=[],
            finish_reason="stop",
            usage_tokens=TokenUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
        )

        backend = MockBackend(responses=[invalid_resp])
        validator = ResponseValidator(tool_names=["test_tool"], rescue_enabled=False)

        cfg = InferenceConfig(
            client=backend,
            tool_specs=[tool_def],
            validator=validator,
            max_attempts=0,
        )

        with pytest.raises(RetriesExhaustedError):
            await run_inference(messages, cfg)

    @pytest.mark.asyncio
    async def test_optional_guardrails_components(self):
        """Test inference works without optional guardrail components."""
        messages = [Message(role=Role.USER, content="test")]

        tool_def = ToolDefinition(
            name="test_tool",
            description="A test tool",
            input_schema={"type": "object", "properties": {}},
        )

        mock_resp = Response(
            content="",
            tool_calls=[LLMToolCall(id="1", name="test_tool", arguments={})],
            finish_reason="tool_calls",
            usage_tokens=TokenUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
        )

        backend = MockBackend(responses=[mock_resp])

        cfg = InferenceConfig(
            client=backend,
            tool_specs=[tool_def],
            max_attempts=3,
        )

        result = await run_inference(messages, cfg)

        assert result is not None
        assert result.attempts == 1


class TestRunInferenceReasoning:
    """Tests for AR-1 reasoning stripping and normalization in the loop."""

    def _tool_def(self) -> ToolDefinition:
        return ToolDefinition(
            name="test_tool",
            description="A test tool",
            input_schema={"type": "object", "properties": {}},
        )

    @pytest.mark.asyncio
    async def test_strips_thinking_tags_before_tool_call_parsing(self):
        """Reasoning embedded as thinking tags must be stripped before parsing."""
        messages = [Message(role=Role.USER, content="test")]

        mock_resp = Response(
            content="<thinking>goal: answer\n\nobservation: the index is fresh</thinking>\n"
            '{"tool": "test_tool", "args": {"key": "value"}}',
            tool_calls=[],
            finish_reason="stop",
            usage_tokens=TokenUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
        )

        backend = MockBackend(responses=[mock_resp])
        validator = ResponseValidator(tool_names=["test_tool"], rescue_enabled=True)

        cfg = InferenceConfig(
            client=backend,
            tool_specs=[self._tool_def()],
            validator=validator,
            max_attempts=3,
        )

        result = await run_inference(messages, cfg)

        assert result is not None
        assert len(result.response.tool_calls) == 1
        assert result.response.tool_calls[0].name == "test_tool"

        assert result.reasoning_tree is not None
        assert len(result.reasoning_tree.nodes) == 2
        for node in result.reasoning_tree.nodes:
            assert len(node.summary) <= 512
            assert node.summary != "goal: answer\n\nobservation: the index is fresh"

    @pytest.mark.asyncio
    async def test_native_reasoning_field(self):
        """Native reasoning_content produces a native-format tree."""
        messages = [Message(role=Role.USER, content="test")]

        mock_resp = Response(
            content='{"tool": "test_tool", "args": {"key": "value"}}',
            reasoning="observation: the index is fresh\n\ndecision: call test_tool",
            tool_calls=[],
            finish_reason="stop",
            usage_tokens=TokenUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
        )

        backend = MockBackend(responses=[mock_resp])
        validator = ResponseValidator(tool_names=["test_tool"], rescue_enabled=True)

        cfg = InferenceConfig(
            client=backend,
            tool_specs=[self._tool_def()],
            validator=validator,
            max_attempts=3,
        )

        result = await run_inference(messages, cfg)

        assert result is not None
        assert result.reasoning_tree is not None
        assert result.reasoning_tree.format.value == "native"
        assert len(result.response.tool_calls) == 1

    @pytest.mark.asyncio
    async def test_fails_closed_on_unbalanced_reasoning(self):
        """Malformed reasoning never produces a tool call from a partial parse."""
        messages = [Message(role=Role.USER, content="test")]

        mock_resp = Response(
            content='<thinking>never closed\n{"tool": "test_tool", "args": {}}',
            tool_calls=[],
            finish_reason="stop",
            usage_tokens=TokenUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
        )

        backend = MockBackend(responses=[mock_resp])
        validator = ResponseValidator(tool_names=["test_tool"], rescue_enabled=True)

        cfg = InferenceConfig(
            client=backend,
            tool_specs=[self._tool_def()],
            validator=validator,
            max_attempts=3,
        )

        with pytest.raises(RetriesExhaustedError):
            await run_inference(messages, cfg)
