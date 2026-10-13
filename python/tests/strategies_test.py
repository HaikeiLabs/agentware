"""Tests for TieredCompact compaction strategy."""

from haikeilabs.agentware.llm import Message, Role
from haikeilabs.agentware.llmcontext.strategies import TieredCompact, _find_eligible_end
from haikeilabs.agentware.middleware.types import MessageMeta, MessageType


def simple_token_counter(messages: list[Message]) -> int:
    """Simple token counter that counts characters / 4."""
    return sum(len(m.content) for m in messages) // 4


class TestTieredCompactName:
    def test_name_returns_tiered_compact(self):
        compact = TieredCompact()
        assert compact.name() == "TieredCompact"


class TestTieredCompactEmptyMessages:
    def test_compact_empty_returns_empty(self):
        compact = TieredCompact()
        result = compact.compact([], 100, simple_token_counter)
        assert result == []


class TestTieredCompactPhase1:
    def test_phase1_drops_nudges(self):
        compact = TieredCompact(keep_recent=0)

        messages = [
            Message(
                role=Role.SYSTEM,
                content="system",
                meta=MessageMeta(type=MessageType.SYSTEM_PROMPT),
            ),
            Message(
                role=Role.USER,
                content="user",
                meta=MessageMeta(type=MessageType.USER_INPUT),
            ),
            Message(
                role=Role.ASSISTANT,
                content="",
                meta=MessageMeta(type=MessageType.STEP_NUDGE, step_index=0),
            ),
            Message(
                role=Role.ASSISTANT,
                content="text",
                meta=MessageMeta(type=MessageType.TEXT_RESPONSE, step_index=0),
            ),
        ]

        result = compact.compact(messages, 2, simple_token_counter)

        nudge_found = any(m.meta.type == MessageType.STEP_NUDGE for m in result)
        assert not nudge_found, "expected nudge to be dropped in phase 1"

    def test_phase1_truncates_tool_results(self):
        compact = TieredCompact(keep_recent=0, truncate_chars=200)

        long_content = "x" * 500

        messages = [
            Message(
                role=Role.SYSTEM,
                content="system",
                meta=MessageMeta(type=MessageType.SYSTEM_PROMPT, step_index=0),
            ),
            Message(
                role=Role.USER,
                content="user",
                meta=MessageMeta(type=MessageType.USER_INPUT, step_index=0),
            ),
            Message(
                role=Role.TOOL,
                content=long_content,
                meta=MessageMeta(type=MessageType.TOOL_RESULT, step_index=0),
            ),
            Message(
                role=Role.TOOL,
                content=long_content,
                meta=MessageMeta(type=MessageType.TOOL_RESULT, step_index=1),
            ),
        ]

        result = compact.compact(messages, 150, simple_token_counter)

        assert len(result) == 4
        for m in result:
            if m.meta.type == MessageType.TOOL_RESULT:
                assert len(m.content) <= 200, f"expected truncated to 200, got {len(m.content)}"


class TestTieredCompactPhase2:
    def test_phase2_drops_tool_results(self):
        compact = TieredCompact(keep_recent=0)

        messages = [
            Message(
                role=Role.SYSTEM,
                content="system",
                meta=MessageMeta(type=MessageType.SYSTEM_PROMPT),
            ),
            Message(
                role=Role.USER,
                content="user",
                meta=MessageMeta(type=MessageType.USER_INPUT),
            ),
            Message(
                role=Role.TOOL,
                content="tool result",
                meta=MessageMeta(type=MessageType.TOOL_RESULT, step_index=0),
            ),
            Message(
                role=Role.ASSISTANT,
                content="text",
                meta=MessageMeta(type=MessageType.TEXT_RESPONSE, step_index=0),
            ),
        ]

        result = compact.compact(messages, 5, simple_token_counter)

        tool_result_found = any(m.meta.type == MessageType.TOOL_RESULT for m in result)
        assert not tool_result_found, "expected tool_result to be dropped in phase 2"


class TestTieredCompactPhase3:
    def test_phase3_drops_reasoning_and_text_response(self):
        compact = TieredCompact(keep_recent=0)

        messages = [
            Message(
                role=Role.SYSTEM,
                content="system",
                meta=MessageMeta(type=MessageType.SYSTEM_PROMPT),
            ),
            Message(
                role=Role.USER,
                content="user",
                meta=MessageMeta(type=MessageType.USER_INPUT),
            ),
            Message(
                role=Role.ASSISTANT,
                content="reasoning text",
                meta=MessageMeta(type=MessageType.REASONING, step_index=0),
            ),
            Message(
                role=Role.ASSISTANT,
                content="text response",
                meta=MessageMeta(type=MessageType.TEXT_RESPONSE, step_index=0),
            ),
        ]

        result = compact.compact(messages, 5, simple_token_counter)

        reasoning_found = any(m.meta.type == MessageType.REASONING for m in result)
        text_response_found = any(
            m.meta.type == MessageType.TEXT_RESPONSE for m in result
        )
        assert not reasoning_found, "expected reasoning to be dropped in phase 3"
        assert not text_response_found, "expected text_response to be dropped in phase 3"


class TestTieredCompactProtectedMessages:
    def test_messages_0_and_1_always_protected(self):
        compact = TieredCompact(keep_recent=0)

        messages = [
            Message(
                role=Role.SYSTEM,
                content="system",
                meta=MessageMeta(type=MessageType.SYSTEM_PROMPT),
            ),
            Message(
                role=Role.USER,
                content="user",
                meta=MessageMeta(type=MessageType.USER_INPUT),
            ),
            Message(
                role=Role.ASSISTANT,
                content="reasoning",
                meta=MessageMeta(type=MessageType.REASONING, step_index=0),
            ),
        ]

        result = compact.compact(messages, 1, simple_token_counter)

        assert len(result) >= 2
        assert result[0].meta.type == MessageType.SYSTEM_PROMPT
        assert result[1].meta.type == MessageType.USER_INPUT


class TestTieredCompactKeepRecent:
    def test_keep_recent_preserves_steps(self):
        compact = TieredCompact(keep_recent=2)

        messages = [
            Message(
                role=Role.SYSTEM,
                content="system",
                meta=MessageMeta(type=MessageType.SYSTEM_PROMPT, step_index=0),
            ),
            Message(
                role=Role.USER,
                content="user",
                meta=MessageMeta(type=MessageType.USER_INPUT, step_index=0),
            ),
            Message(
                role=Role.TOOL,
                content="tool0",
                meta=MessageMeta(type=MessageType.TOOL_RESULT, step_index=0),
            ),
            Message(
                role=Role.TOOL,
                content="tool1",
                meta=MessageMeta(type=MessageType.TOOL_RESULT, step_index=1),
            ),
            Message(
                role=Role.TOOL,
                content="tool2",
                meta=MessageMeta(type=MessageType.TOOL_RESULT, step_index=2),
            ),
            Message(
                role=Role.TOOL,
                content="tool3",
                meta=MessageMeta(type=MessageType.TOOL_RESULT, step_index=3),
            ),
            Message(
                role=Role.TOOL,
                content="tool4",
                meta=MessageMeta(type=MessageType.TOOL_RESULT, step_index=4),
            ),
        ]

        result = compact.compact(messages, 1, simple_token_counter)

        step3_preserved = any(
            m.meta.step_index == 3 for m in result if m.meta.step_index is not None
        )
        step4_preserved = any(
            m.meta.step_index == 4 for m in result if m.meta.step_index is not None
        )
        assert step3_preserved, "step 3 should be preserved (keep_recent=2)"
        assert step4_preserved, "step 4 should be preserved (keep_recent=2)"


class TestFindEligibleEnd:
    def test_no_steps_returns_adjusted_index(self):
        messages = [
            Message(
                role=Role.SYSTEM,
                content="system",
                meta=MessageMeta(type=MessageType.SYSTEM_PROMPT),
            ),
            Message(
                role=Role.USER,
                content="user",
                meta=MessageMeta(type=MessageType.USER_INPUT),
            ),
            Message(
                role=Role.ASSISTANT,
                content="msg1",
                meta=MessageMeta(type=MessageType.TEXT_RESPONSE),
            ),
            Message(
                role=Role.ASSISTANT,
                content="msg2",
                meta=MessageMeta(type=MessageType.TEXT_RESPONSE),
            ),
            Message(
                role=Role.ASSISTANT,
                content="msg3",
                meta=MessageMeta(type=MessageType.TEXT_RESPONSE),
            ),
        ]

        result = _find_eligible_end(messages, 1)
        expected = len(messages) - 1 - 1
        assert result == expected, f"expected {expected}, got {result}"

    def test_with_steps_finds_boundary(self):
        messages = [
            Message(
                role=Role.SYSTEM,
                content="system",
                meta=MessageMeta(type=MessageType.SYSTEM_PROMPT, step_index=0),
            ),
            Message(
                role=Role.USER,
                content="user",
                meta=MessageMeta(type=MessageType.USER_INPUT, step_index=0),
            ),
            Message(
                role=Role.ASSISTANT,
                content="step1",
                meta=MessageMeta(type=MessageType.REASONING, step_index=1),
            ),
            Message(
                role=Role.ASSISTANT,
                content="step2",
                meta=MessageMeta(type=MessageType.REASONING, step_index=2),
            ),
            Message(
                role=Role.ASSISTANT,
                content="step3",
                meta=MessageMeta(type=MessageType.REASONING, step_index=3),
            ),
        ]

        result = _find_eligible_end(messages, 2)
        assert result == 2


class TestTieredCompactNoCompactionNeeded:
    def test_under_target_unchanged(self):
        compact = TieredCompact()

        messages = [
            Message(
                role=Role.SYSTEM,
                content="system prompt",
                meta=MessageMeta(type=MessageType.SYSTEM_PROMPT),
            ),
            Message(
                role=Role.USER,
                content="user input",
                meta=MessageMeta(type=MessageType.USER_INPUT),
            ),
        ]

        result = compact.compact(messages, 100, simple_token_counter)

        assert len(result) == 2
        assert result[0].content == "system prompt"
        assert result[1].content == "user input"
