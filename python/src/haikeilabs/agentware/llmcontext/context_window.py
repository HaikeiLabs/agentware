"""Context window management for conversation history."""

import threading
from collections.abc import Callable
from dataclasses import dataclass

from haikeilabs.agentware.llm import Message
from haikeilabs.agentware.llmcontext.strategies import (
    CompactionStrategy,
    TieredCompact,
    TokenCounter,
)


@dataclass(frozen=True)
class CompactEvent:
    """Event emitted when context compaction occurs."""

    step_index: int
    tokens_before: int
    tokens_after: int
    budget_tokens: int
    messages_before: int
    messages_after: int
    phase_reached: int
    strategy_name: str

ThresholdCallback = Callable[[int, int, float], str | None]


def default_context_warning(tokens: int, budget: int, pct: float) -> str | None:
    """Default callback for context threshold warnings."""
    if pct >= 0.80:
        return "[Context is nearly full. Summarize critical findings and complete current task.]"
    if pct >= 0.65:
        return "[Context is filling up. Be concise and front-load important information.]"
    return None


class ContextWindowManager:
    """Manages context window size and compaction for conversation history."""

    def __init__(
        self,
        context_window: int,
        counter: TokenCounter | None = None,
        strategy: CompactionStrategy | None = None,
        context_thresholds: list[float] | None = None,
        on_context_threshold: ThresholdCallback | None = None,
        on_compact: Callable[[CompactEvent], None] | None = None,
    ) -> None:
        self._context_window = context_window
        self._compaction_ratio = 0.75
        self._counter: TokenCounter = counter if counter is not None else default_counter
        self._strategy: CompactionStrategy = (
            strategy if strategy is not None else TieredCompact()
        )
        self._last_known_tokens: int | None = None
        self._lock = threading.RLock()
        self._context_thresholds = (
            sorted(context_thresholds) if context_thresholds else [0.65, 0.80]
        )
        self._on_context_threshold = (
            on_context_threshold if on_context_threshold is not None else default_context_warning
        )
        self._fired_thresholds: set[float] = set()
        self._on_compact = on_compact

    def set_compaction_ratio(self, ratio: float) -> None:
        """Set the ratio of context window at which compaction triggers."""
        with self._lock:
            self._compaction_ratio = ratio

    def update_token_count(self, total_tokens: int) -> None:
        """Record actual token count from backend API response."""
        with self._lock:
            self._last_known_tokens = total_tokens

    def check(self, messages: list[Message]) -> tuple[int, bool]:
        """Check current token count and if threshold is exceeded.

        Returns:
            Tuple of (current_tokens, should_compact)
        """
        with self._lock:
            current_tokens = self._estimate_tokens(messages)
            threshold = int(self._context_window * self._compaction_ratio)
            return current_tokens, current_tokens >= threshold

    def should_compact(self, messages: list[Message]) -> bool:
        """Determine if compaction should be triggered."""
        with self._lock:
            current_tokens = self._estimate_tokens(messages)
            threshold = int(self._context_window * self._compaction_ratio)
            return current_tokens > threshold

    def compact(self, messages: list[Message]) -> list[Message]:
        """Compact messages to fit within target token count.

        Returns compacted messages. Resets last_known_tokens after compaction.
        Emits CompactEvent via on_compact callback if configured.
        """
        with self._lock:
            tokens_before = self._estimate_tokens(messages)
            messages_before = len(messages)
            target_tokens = int(self._context_window * self._compaction_ratio)
            compacted = self._strategy.compact(messages, target_tokens, self._counter)
            tokens_after = self._counter(compacted)
            messages_after = len(compacted)

            phase_reached = 0
            if isinstance(self._strategy, TieredCompact):
                phase_reached = self._strategy.last_phase

            if self._on_compact is not None:
                event = CompactEvent(
                    step_index=0,
                    tokens_before=tokens_before,
                    tokens_after=tokens_after,
                    budget_tokens=self._context_window,
                    messages_before=messages_before,
                    messages_after=messages_after,
                    phase_reached=phase_reached,
                    strategy_name=self._strategy.name(),
                )
                self._on_compact(event)

            self._last_known_tokens = None
            self._fired_thresholds = set()
            return compacted

    def check_thresholds(self, messages: list[Message]) -> str | None:
        """Check if usage has crossed any configured threshold.

        Returns a warning to inject as transient message, or None.
        Each threshold fires at most once per session; resets after compaction.
        """
        with self._lock:
            current_tokens = self._estimate_tokens(messages)
            budget = self._context_window
            if current_tokens == 0 or budget == 0:
                return None

            pct = current_tokens / budget

            for threshold in reversed(self._context_thresholds):
                if pct >= threshold:
                    if threshold not in self._fired_thresholds:
                        self._fired_thresholds.add(threshold)
                        return self._on_context_threshold(current_tokens, budget, pct)
            return None

    def _estimate_tokens(self, messages: list[Message]) -> int:
        """Estimate tokens, using actual count if available."""
        if self._last_known_tokens is not None:
            return self._last_known_tokens
        return self._counter(messages)


def default_counter(messages: list[Message]) -> int:
    """Default token counter using character-based estimation."""
    total = 0
    for m in messages:
        overhead = len(str(m.role)) + 4
        if m.tool_calls:
            for tc in m.tool_calls:
                overhead += len(tc.name) + 1
        total += (len(m.content) // 4) + overhead
    return total
