"""Tests for ContextWindowManager."""

import threading

from haikeilabs.agentware.llm import Message, Role
from haikeilabs.agentware.llmcontext.context_window import (
    CompactEvent,
    ContextWindowManager,
    default_counter,
)


def make_messages(count: int) -> list[Message]:
    """Create test messages with increasing content length."""
    return [
        Message(
            role=Role.USER if i % 2 == 0 else Role.ASSISTANT,
            content=f"Message {i}: " + ("x" * (i * 100)),
        )
        for i in range(count)
    ]


class TestContextWindowManagerUpdateTokenCount:  # noqa: N801
    def test_update_token_count_stores_value(self):
        mgr = ContextWindowManager(1000, default_counter)
        mgr.update_token_count(500)

        tokens, _ = mgr.check([])
        assert tokens == 500

    def test_token_count_overrides_estimate(self):
        mgr = ContextWindowManager(1000, default_counter)
        messages = make_messages(3)

        _, needs_compact_before = mgr.check(messages)
        mgr.update_token_count(100)
        tokens, _ = mgr.check(messages)

        assert tokens == 100
        assert not needs_compact_before


class TestContextWindowManagerCheckUsesActualCount:  # noqa: N801
    def test_check_returns_false_when_under_threshold(self):
        mgr = ContextWindowManager(1000, default_counter)
        mgr.update_token_count(500)

        tokens, should_compact = mgr.check([])

        assert tokens == 500
        assert not should_compact

    def test_check_returns_true_when_at_threshold(self):
        mgr = ContextWindowManager(1000, default_counter)
        mgr.set_compaction_ratio(0.5)
        mgr.update_token_count(500)

        tokens, should_compact = mgr.check([])

        assert tokens == 500
        assert should_compact


class TestContextWindowManagerShouldCompactUsesActualCount:  # noqa: N801
    def test_should_compact_false_when_under(self):
        mgr = ContextWindowManager(1000, default_counter)
        mgr.update_token_count(500)

        assert not mgr.should_compact([])

    def test_should_compact_true_when_over(self):
        mgr = ContextWindowManager(1000, default_counter)
        mgr.set_compaction_ratio(0.5)
        mgr.update_token_count(600)

        assert mgr.should_compact([])


class TestContextWindowManagerCompactResetsTokenCount:  # noqa: N801
    def test_compact_resets_last_known_tokens(self):
        mgr = ContextWindowManager(1000, default_counter)
        mgr.update_token_count(800)
        messages = make_messages(10)

        compacted = mgr.compact(messages)

        assert isinstance(compacted, list)
        tokens, _ = mgr.check([])
        assert tokens is not None
        assert tokens != 800


class TestContextWindowManagerThreadSafety:  # noqa: N801
    def test_concurrent_access(self):
        mgr = ContextWindowManager(1000, default_counter)
        errors: list[Exception] = []

        def worker():
            try:
                for i in range(100):
                    mgr.update_token_count(i)
                    mgr.check(make_messages(5))
                    mgr.should_compact(make_messages(5))
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0


class TestDefaultCounter:
    def test_counts_messages_correctly(self):
        messages = [
            Message(role=Role.USER, content="hello world test content here"),
            Message(role=Role.ASSISTANT, content="response with more content"),
        ]

        count = default_counter(messages)

        assert count > 0

    def test_empty_messages_returns_zero(self):
        count = default_counter([])
        assert count == 0


class TestContextWindowManagerCheckThresholds:  # noqa: N801
    def test_fires_once_per_threshold(self):
        def counter(messages):
            return 850

        mgr = ContextWindowManager(1000, counter, context_thresholds=[0.80])
        messages = [Message(role=Role.USER, content="test")]

        warning = mgr.check_thresholds(messages)
        assert warning is not None

        warning = mgr.check_thresholds(messages)
        assert warning is None

    def test_resets_after_compact(self):
        def counter(messages):
            return 850

        mgr = ContextWindowManager(1000, counter, context_thresholds=[0.80])
        messages = [Message(role=Role.USER, content="test")]

        mgr.check_thresholds(messages)

        mgr.compact(messages)

        mgr.update_token_count(850)
        warning = mgr.check_thresholds(messages)
        assert warning is not None

    def test_highest_threshold_fires_first(self):
        def counter(messages):
            return 900

        mgr = ContextWindowManager(1000, counter, context_thresholds=[0.50, 0.80, 0.65])
        messages = [Message(role=Role.USER, content="test")]

        warning = mgr.check_thresholds(messages)
        assert warning is not None
        assert "nearly full" in warning

    def test_default_thresholds(self):
        def counter(messages):
            return 700

        mgr = ContextWindowManager(1000, counter)
        messages = [Message(role=Role.USER, content="test")]

        warning = mgr.check_thresholds(messages)
        assert warning is not None
        assert "filling up" in warning

    def test_custom_callback(self):
        def counter(messages):
            return 850

        called = [False]

        def custom_cb(tokens, budget, pct):
            called[0] = True
            return "custom warning"

        mgr = ContextWindowManager(
            1000, counter, context_thresholds=[0.80], on_context_threshold=custom_cb
        )
        messages = [Message(role=Role.USER, content="test")]

        warning = mgr.check_thresholds(messages)
        assert called[0]
        assert warning == "custom warning"

    def test_zero_tokens_returns_none(self):
        mgr = ContextWindowManager(1000, None)
        messages = [Message(role=Role.USER, content="")]

        warning = mgr.check_thresholds(messages)
        assert warning is None


class TestContextWindowManagerThreadSafetyCheckThresholds:  # noqa: N801
    def test_concurrent_check_thresholds(self):
        mgr = ContextWindowManager(
            1000, default_counter, context_thresholds=[0.50]
        )
        errors = []

        def worker():
            try:
                for i in range(100):
                    mgr.check_thresholds(make_messages(5))
                    mgr.update_token_count(i)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0


class TestCompactEvent:
    def test_compact_event_fired_on_compact(self):
        received_events: list[CompactEvent] = []

        def on_compact(event: CompactEvent):
            received_events.append(event)

        mgr = ContextWindowManager(1000, default_counter, on_compact=on_compact)
        mgr.set_compaction_ratio(0.5)
        messages = make_messages(10)

        mgr.compact(messages)

        assert len(received_events) == 1
        event = received_events[0]
        assert event.messages_before == 10
        assert event.messages_after <= 10
        assert event.tokens_before >= event.tokens_after
        assert event.budget_tokens == 1000
        assert event.strategy_name == "TieredCompact"

    def test_compact_event_not_fired_without_callback(self):
        mgr = ContextWindowManager(1000, default_counter)
        mgr.set_compaction_ratio(0.5)
        messages = make_messages(10)

        compacted = mgr.compact(messages)

        assert isinstance(compacted, list)

    def test_compact_event_phase_reached(self):
        received_events: list[CompactEvent] = []

        def on_compact(event: CompactEvent):
            received_events.append(event)

        mgr = ContextWindowManager(1000, default_counter, on_compact=on_compact)
        mgr.set_compaction_ratio(0.1)
        messages = make_messages(20)

        mgr.compact(messages)

        assert len(received_events) == 1
        assert received_events[0].phase_reached > 0

    def test_compact_event_zero_phase_when_no_compaction_needed(self):
        received_events: list[CompactEvent] = []

        def on_compact(event: CompactEvent):
            received_events.append(event)

        def counter(messages: list[Message]) -> int:
            return 100

        mgr = ContextWindowManager(1000, counter, on_compact=on_compact)
        mgr.set_compaction_ratio(0.5)
        messages = make_messages(3)

        mgr.compact(messages)

        assert len(received_events) == 1
        assert received_events[0].phase_reached == 0
