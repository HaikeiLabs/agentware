"""Tests for MessageType, MessageMeta, and Message types."""

import pytest

from haikeilabs.agentware.llm.request import Message, Role
from haikeilabs.agentware.middleware.types import MessageMeta, MessageType


class TestMessageType:
    """Tests for MessageType enum."""

    def test_all_types_defined(self):
        """Verify all 11 MessageType variants are defined."""
        expected_types = [
            "system_prompt",
            "user_input",
            "tool_call",
            "tool_result",
            "reasoning",
            "text_response",
            "step_nudge",
            "prerequisite_nudge",
            "retry_nudge",
            "context_warning",
            "summary",
        ]
        actual_types = [mt.value for mt in MessageType]
        assert sorted(actual_types) == sorted(expected_types)

    def test_message_type_is_str_enum(self):
        """Verify MessageType inherits from str for easy serialization."""
        assert isinstance(MessageType.USER_INPUT, str)
        assert MessageType.USER_INPUT == "user_input"


class TestMessageMeta:
    """Tests for MessageMeta dataclass."""

    def test_default_values(self):
        """Verify default values for MessageMeta."""
        meta = MessageMeta()
        assert meta.type == MessageType.USER_INPUT
        assert meta.step_index is None
        assert meta.original_type is None
        assert meta.token_estimate is None

    def test_custom_values(self):
        """Verify MessageMeta accepts custom values."""
        meta = MessageMeta(
            type=MessageType.TOOL_RESULT,
            step_index=5,
            original_type=MessageType.TOOL_CALL,
            token_estimate=150,
        )
        assert meta.type == MessageType.TOOL_RESULT
        assert meta.step_index == 5
        assert meta.original_type == MessageType.TOOL_CALL
        assert meta.token_estimate == 150

    def test_frozen(self):
        """Verify MessageMeta is frozen (immutable)."""
        from dataclasses import FrozenInstanceError

        meta = MessageMeta(type=MessageType.REASONING)
        with pytest.raises((AttributeError, FrozenInstanceError)):
            meta.type = MessageType.TOOL_CALL


class TestMessage:
    """Tests for Message dataclass with meta field."""

    def test_default_meta(self):
        """Verify Message has default meta field."""
        msg = Message(role=Role.USER, content="Hello")
        assert msg.meta == MessageMeta()
        assert msg.meta.type == MessageType.USER_INPUT

    def test_custom_meta(self):
        """Verify Message accepts custom meta."""
        meta = MessageMeta(type=MessageType.SYSTEM_PROMPT, step_index=0)
        msg = Message(role=Role.SYSTEM, content="You are helpful", meta=meta)
        assert msg.meta.type == MessageType.SYSTEM_PROMPT
        assert msg.meta.step_index == 0

    def test_backward_compatibility(self):
        """Verify Message can be created without meta (backward compatible)."""
        msg = Message(role=Role.ASSISTANT, content="Response", tool_call_id="call_123")
        assert msg.role == Role.ASSISTANT
        assert msg.content == "Response"
        assert msg.tool_call_id == "call_123"
        assert msg.meta == MessageMeta()
