"""Tests for the reasoning adapter (mirrors go/reasoning/adapter_test.go)."""

from datetime import datetime, timezone

import pytest

from haikeilabs.agentware.reasoning import (
    CONTRACT_VERSION,
    ContextTree,
    Format,
    MalformedContentError,
    MalformedInputError,
    MalformedJSONError,
    MalformedReasoningFieldError,
    Node,
    NodeKind,
    NodeStatus,
    ReasoningAdapter,
    ReasoningOptions,
    ReasoningTooLargeError,
    TooDeepError,
    TooManyNodesError,
    UnbalancedThinkingError,
)

FIXED = datetime(2026, 9, 10, 12, 0, 0, tzinfo=timezone.utc)


def fixed_clock() -> datetime:
    return FIXED


class TestStrip:
    def test_native_reasoning_content(self):
        adapter = ReasoningAdapter()
        raw = {
            "message": {
                "role": "assistant",
                "content": '{"tool": "search", "args": {"q": "x"}}',
                "reasoning_content": "I need to search first.",
            }
        }
        assert adapter.strip(raw) == '{"tool": "search", "args": {"q": "x"}}'

    def test_thinking_tags(self):
        adapter = ReasoningAdapter()
        raw = '<thinking>First, check the index.</thinking>\n[TOOL_CALLS] tool1: {"a": 1} [/TOOL_CALLS]'
        assert adapter.strip(raw) == '\n[TOOL_CALLS] tool1: {"a": 1} [/TOOL_CALLS]'

    def test_no_reasoning_passthrough(self):
        adapter = ReasoningAdapter()
        cases = [
            '{"tool": "search", "args": {}}',
            '[{"id": "1", "name": "search", "arguments": {}}]',
            "plain text response",
            "",
        ]
        for raw in cases:
            assert adapter.strip(raw) == raw

    def test_model_specific_field(self):
        adapter = ReasoningAdapter()
        adapter.register_model_field("spark", "thinking_payload")
        raw = {"content": "hello", "thinking_payload": "spark reasoning"}
        assert adapter.strip(raw) == "hello"

    def test_json_string_input_is_opaque(self):
        adapter = ReasoningAdapter()
        raw = '[{"id": "1", "name": "search", "arguments": {"q": "x"}}]'
        assert adapter.strip(raw) == raw


class TestFailClosed:
    def test_non_string_reasoning_field(self):
        adapter = ReasoningAdapter()
        with pytest.raises(MalformedReasoningFieldError):
            adapter.strip({"reasoning_content": ["not", "a", "string"]})

    def test_non_string_content(self):
        adapter = ReasoningAdapter()
        with pytest.raises(MalformedContentError):
            adapter.strip({"content": ["parts"]})

    def test_unbalanced_thinking_tag(self):
        adapter = ReasoningAdapter()
        with pytest.raises(UnbalancedThinkingError):
            adapter.strip("<thinking>never closed")

    def test_dangling_close_tag(self):
        adapter = ReasoningAdapter()
        with pytest.raises(UnbalancedThinkingError):
            adapter.strip("no open tag [/THINK]")

    def test_malformed_json(self):
        adapter = ReasoningAdapter()
        with pytest.raises(MalformedJSONError):
            adapter.strip('{"message": {"content": "x"}')

    def test_unsupported_type(self):
        adapter = ReasoningAdapter()
        with pytest.raises(MalformedInputError):
            adapter.strip(42)


class TestBounds:
    def test_reasoning_too_large(self):
        adapter = ReasoningAdapter(ReasoningOptions(max_reasoning_bytes=16))
        raw = {"content": "", "reasoning_content": "x" * 64}
        with pytest.raises(ReasoningTooLargeError):
            adapter.extract(raw, "deepseek", "test")

    def test_too_many_nodes(self):
        adapter = ReasoningAdapter(ReasoningOptions(max_nodes=2, clock=fixed_clock))
        with pytest.raises(TooManyNodesError):
            adapter.extract("<thinking>p1\n\np2\n\np3\n\np4</thinking>", "m", "b")

    def test_too_deep(self):
        adapter = ReasoningAdapter(ReasoningOptions(max_depth=2, clock=fixed_clock))
        with pytest.raises(TooDeepError):
            adapter.extract("<thinking>a\n\nb\n\nc</thinking>", "m", "b")


class TestExtract:
    def test_context_tree(self):
        adapter = ReasoningAdapter(ReasoningOptions(clock=fixed_clock))
        raw = (
            "<thinking>"
            "goal: verify the payment gateway\n\n"
            "observation: gateway returned 200 [E1]\n\n"
            "decision: call refund tool\n\n"
            'tool-call: refund_tool: {"id": "txn1"}\n\n'
            "tool-result: refund_tool: ok [SUPERSEDED]\n\n"
            "conclusion: refund issued\n\n"
            "blocker: regulatory approval pending"
            "</thinking>"
        )

        tree = adapter.extract(raw, "deepseek-reasoner", "test-backend")

        assert tree.version == CONTRACT_VERSION
        assert tree.format == Format.THINKING
        assert tree.model == "deepseek-reasoner"
        assert tree.backend == "test-backend"
        assert tree.root_id != ""

        assert len(tree.nodes) == 7
        expected_kinds = [
            NodeKind.GOAL,
            NodeKind.OBSERVATION,
            NodeKind.DECISION,
            NodeKind.TOOL_CALL,
            NodeKind.TOOL_RESULT,
            NodeKind.CONCLUSION,
            NodeKind.BLOCKER,
        ]
        assert [n.kind for n in tree.nodes] == expected_kinds

        # Chain: root has empty parent, every other node's parent is its predecessor.
        assert tree.nodes[0].parent_id == ""
        for prev, node in zip(tree.nodes, tree.nodes[1:]):
            assert node.parent_id == prev.id

        assert tree.nodes[0].status == NodeStatus.COMPLETE
        assert tree.nodes[4].status == NodeStatus.SUPERSEDED
        assert tree.nodes[6].status == NodeStatus.BLOCKED

        assert tree.nodes[1].evidence_refs == ["E1"]
        assert tree.nodes[3].tool_call_refs == ["refund_tool"]

        assert all(n.created_at == FIXED for n in tree.nodes)
        assert all(n.updated_at == FIXED for n in tree.nodes)

    def test_empty_reasoning(self):
        adapter = ReasoningAdapter(ReasoningOptions(clock=fixed_clock))
        tree = adapter.extract({"content": "hello"}, "m", "b")
        assert tree.empty()
        assert tree.root_id == ""
        assert tree.format == Format.NONE

    def test_deterministic_node_ids(self):
        adapter = ReasoningAdapter(ReasoningOptions(clock=fixed_clock))
        raw = "<thinking>observation: x\n\ndecision: y</thinking>"
        t1 = adapter.extract(raw, "m", "b")
        t2 = adapter.extract(raw, "m", "b")
        assert [n.id for n in t1.nodes] == [n.id for n in t2.nodes]
        assert t1.root_id == t2.root_id

    def test_summary_bounded(self):
        adapter = ReasoningAdapter(ReasoningOptions(max_summary_chars=10, clock=fixed_clock))
        raw = "<thinking>observation: " + "abcdefg" * 20 + "</thinking>"
        tree = adapter.extract(raw, "m", "b")
        assert len(tree.nodes) == 1
        assert len(tree.nodes[0].summary) <= 10
        assert "abcdefg" * 20 not in tree.nodes[0].summary

    def test_unicode_summary_truncation(self):
        adapter = ReasoningAdapter(ReasoningOptions(max_summary_chars=3, clock=fixed_clock))
        raw = "<thinking>observation: héllo wörld 🚀 end</thinking>"
        tree = adapter.extract(raw, "m", "b")
        assert tree.nodes[0].summary == "hél"


class TestParse:
    def test_returns_clean_and_tree(self):
        adapter = ReasoningAdapter(ReasoningOptions(clock=fixed_clock))
        raw = "<thinking>observation: x</thinking>\n" + '{"tool": "search", "args": {"q": "1"}}'
        clean, tree = adapter.parse(raw, "m", "b")
        assert clean == '\n{"tool": "search", "args": {"q": "1"}}'
        assert not tree.empty()


class TestContract:
    def test_vocabulary(self):
        assert [k.value for k in NodeKind] == [
            "goal",
            "observation",
            "decision",
            "tool-call",
            "tool-result",
            "conclusion",
            "blocker",
        ]
        assert [s.value for s in NodeStatus] == [
            "active",
            "complete",
            "blocked",
            "superseded",
        ]
        assert [f.value for f in Format] == [
            "native",
            "thinking",
            "model_specific",
            "none",
        ]

    def test_to_dict_never_contains_raw_reasoning(self):
        adapter = ReasoningAdapter(ReasoningOptions(clock=fixed_clock))
        # Raw reasoning longer than the summary bound must never appear whole.
        raw = "<thinking>observation: " + "z" * 1000 + "</thinking>"
        tree = adapter.extract(raw, "m", "b")
        serialized = tree.to_dict()
        assert "z" * 1000 not in str(serialized)
        assert len(tree.nodes[0].summary) <= 512


class TestContextTreeEmpty:
    def test_empty(self):
        assert ContextTree().empty()
        assert ContextTree(nodes=[]).empty()
        assert not ContextTree(
            nodes=[_node()],
        ).empty()


def _node() -> Node:
    return Node(
        id="abc",
        parent_id="",
        kind=NodeKind.GOAL,
        status=NodeStatus.COMPLETE,
        summary="goal",
        created_at=FIXED,
        updated_at=FIXED,
    )
