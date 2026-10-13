"""Compaction strategies for context management."""

from dataclasses import dataclass
from typing import Protocol

from haikeilabs.agentware.llm import Message
from haikeilabs.agentware.middleware.types import MessageType


class TokenCounter(Protocol):
    """Protocol for token counting functions."""

    def __call__(self, messages: list[Message]) -> int:
        """Count tokens in messages."""
        ...


class CompactionStrategy(Protocol):
    """Protocol for compaction strategies."""

    def compact(
        self, messages: list[Message], target_tokens: int, counter: TokenCounter
    ) -> list[Message]:
        """Compact messages to fit within target token count."""
        ...

    def name(self) -> str:
        """Return the strategy name."""
        ...


def _is_nudge_type(msg_type: MessageType) -> bool:
    return (
        msg_type == MessageType.STEP_NUDGE
        or msg_type == MessageType.RETRY_NUDGE
        or msg_type == MessageType.PREREQUISITE_NUDGE
    )


@dataclass
class TieredCompact:
    """3-phase compaction strategy that prunes messages in semantic priority order.

    Compaction Priority (cut first -> preserve last):
    1. STEP_NUDGE, RETRY_NUDGE, PREREQUISITE_NUDGE
    2. TOOL_RESULT - truncate to 200 chars (Phase 1)
    3. TOOL_RESULT - drop entirely (Phase 2)
    4. REASONING, TEXT_RESPONSE - drop (Phase 3)

    messages[0] and messages[1] are always protected.
    """

    keep_recent: int = 2
    phase_thresholds: tuple[float, float, float] | None = None
    truncate_chars: int = 200
    last_phase: int = 0

    def __post_init__(self) -> None:
        if self.phase_thresholds is None:
            self.phase_thresholds = (0.75, 0.75, 0.75)

    def name(self) -> str:
        return "TieredCompact"

    def compact(
        self, messages: list[Message], target_tokens: int, counter: TokenCounter
    ) -> list[Message]:
        """Compact messages using 3-phase tiered strategy."""
        self.last_phase = 0

        if not messages:
            return []

        result = list(messages)

        eligible_end = _find_eligible_end(result, self.keep_recent)

        current_tokens = counter(result)

        if current_tokens <= target_tokens:
            return result

        result = self._phase1_compact(result, eligible_end, counter)
        current_tokens = counter(result)
        self.last_phase = 1
        if current_tokens <= target_tokens:
            return result

        result = self._phase2_compact(result, eligible_end, counter)
        current_tokens = counter(result)
        self.last_phase = 2
        if current_tokens <= target_tokens:
            return result

        result = self._phase3_compact(result, eligible_end, counter)
        self.last_phase = 3
        return result

    def _protected_steps(self, messages: list[Message]) -> dict[int, bool]:
        """Get set of protected step indices."""
        protected: dict[int, bool] = {}

        if self.keep_recent <= 0:
            return protected

        steps: list[int] = []
        step_set: set[int] = set()
        for m in messages:
            if m.meta.step_index is not None and m.meta.step_index not in step_set:
                steps.append(m.meta.step_index)
                step_set.add(m.meta.step_index)

        if not steps:
            return protected

        if self.keep_recent >= len(steps):
            for s in steps:
                protected[s] = True
            return protected

        start_idx = len(steps) - self.keep_recent
        for i in range(start_idx, len(steps)):
            protected[steps[i]] = True

        return protected

    def _is_protected(self, msg: Message, protected_steps: dict[int, bool]) -> bool:
        """Check if message is in a protected step."""
        if msg.meta.step_index is None:
            return False
        return protected_steps.get(msg.meta.step_index, False)

    def _phase1_compact(
        self, messages: list[Message], eligible_end: int, counter: TokenCounter
    ) -> list[Message]:
        """Phase 1: drop nudges, truncate TOOL_RESULT to truncate_chars."""
        result: list[Message] = []
        protected = self._protected_steps(messages)

        for i, m in enumerate(messages):
            if i == 0 or i == 1:
                result.append(m)
                continue

            if self._is_protected(m, protected):
                result.append(m)
                continue

            if _is_nudge_type(m.meta.type):
                continue

            if m.meta.type == MessageType.TOOL_RESULT:
                if len(m.content) > self.truncate_chars:
                    truncated = Message(
                        role=m.role,
                        content=m.content[: self.truncate_chars],
                        tool_call_id=m.tool_call_id,
                        tool_calls=m.tool_calls,
                        meta=m.meta,
                    )
                    result.append(truncated)
                    continue

            result.append(m)

        return result

    def _phase2_compact(
        self, messages: list[Message], eligible_end: int, counter: TokenCounter
    ) -> list[Message]:
        """Phase 2: phase1 + drop TOOL_RESULT entirely."""
        result: list[Message] = []
        protected = self._protected_steps(messages)

        for i, m in enumerate(messages):
            if i == 0 or i == 1:
                result.append(m)
                continue

            if self._is_protected(m, protected):
                result.append(m)
                continue

            if m.meta.type == MessageType.TOOL_RESULT:
                continue

            result.append(m)

        return result

    def _phase3_compact(
        self, messages: list[Message], eligible_end: int, counter: TokenCounter
    ) -> list[Message]:
        """Phase 3: phase2 + drop REASONING and TEXT_RESPONSE."""
        result: list[Message] = []
        protected = self._protected_steps(messages)

        for i, m in enumerate(messages):
            if i == 0 or i == 1:
                result.append(m)
                continue

            if self._is_protected(m, protected):
                result.append(m)
                continue

            if m.meta.type in (MessageType.REASONING, MessageType.TEXT_RESPONSE):
                continue

            result.append(m)

        return result


def _find_eligible_end(messages: list[Message], keep_recent: int) -> int:
    """Find the last index that is eligible for compaction based on step_index."""
    if keep_recent <= 0:
        return len(messages) - 1

    max_step = -1
    for m in messages:
        if m.meta.step_index is not None and m.meta.step_index > max_step:
            max_step = m.meta.step_index

    if max_step < 0:
        protected_count = keep_recent
        if protected_count >= len(messages):
            protected_count = len(messages)
        return len(messages) - 1 - protected_count

    protected_steps: dict[int, bool] = {}
    current_step = max_step
    for _ in range(keep_recent):
        protected_steps[current_step] = True
        current_step -= 1
        if current_step < 0:
            break

    for i in range(len(messages) - 1, -1, -1):
        step_idx = messages[i].meta.step_index
        if step_idx is None or not protected_steps.get(step_idx, False):
            return i

    return -1
