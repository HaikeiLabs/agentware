"""Reasoning adapter implementation (mirrors go/reasoning)."""

import hashlib
import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from .types import CONTRACT_VERSION, ContextTree, Format, Node, NodeKind, NodeStatus


class ReasoningError(Exception):
    """Base class for reasoning adapter failures. Fail closed."""


class MalformedInputError(ReasoningError):
    """The raw input has no usable shape."""


class MalformedJSONError(ReasoningError):
    """Input that looks like JSON cannot be decoded."""


class MalformedReasoningFieldError(ReasoningError):
    """A reasoning field is not a string (e.g. reasoning_content is a list)."""


class MalformedContentError(ReasoningError):
    """A structured content field is neither a string nor null."""


class UnbalancedThinkingError(ReasoningError):
    """A thinking tag was opened but never closed."""


class ReasoningTooLargeError(ReasoningError):
    """The extracted reasoning exceeds the configured byte cap."""


class TooManyNodesError(ReasoningError):
    """The reasoning would produce more nodes than configured."""


class TooDeepError(ReasoningError):
    """The reasoning would produce a deeper chain than configured."""


@dataclass
class ReasoningOptions:
    """Bounds for the reasoning adapter.

    All limits fail closed: an input that exceeds a bound raises an error
    rather than being truncated into a partial tree (summaries are the one
    bounded field by design; they are truncated, never the parse aborted).
    """

    max_reasoning_bytes: int = 64 * 1024
    max_nodes: int = 64
    max_depth: int = 32
    max_summary_chars: int = 512
    clock: Callable[[], datetime] | None = None


def _now() -> datetime:
    """Default clock."""
    return datetime.now()


KNOWN_REASONING_FIELDS = ["reasoning_content", "thinking", "thinking_content", "reasoning"]

RECOGNIZED_KEYS = {
    "message",
    "content",
    "reasoning_content",
    "thinking",
    "thinking_content",
    "reasoning",
}

_THINKING_OPEN = re.compile(r"<thinking>|\[THINK\]", re.IGNORECASE)
_THINKING_CLOSE = re.compile(r"</thinking>|\[/THINK\]", re.IGNORECASE)

_PARAGRAPH_SEP = re.compile(r"\n[ \t]*\n")
_LIST_ORNAMENT = re.compile(r"^\s*(?:[-*+•]|\d+[.)])\s+")
_KIND_PREFIX = re.compile(
    r"(?i)^\s*(goal|observation|decision|tool-call|tool_call|tool-result|tool_result|tool|conclusion|blocker)\s*[:=]\s*"
)
_STATUS_MARKER = re.compile(r"(?i)\s*\[(blocked|pending|active|superseded|complete)\]\s*$")
_EVIDENCE_PATTERNS = [
    re.compile(r"\[E\d+\]"),
    re.compile(r"\bref\s*[=:]\s*(\S+)"),
    re.compile(r"\bevidence\s*[=:]\s*(\S+)"),
]
_TOOL_REF = re.compile(r"\b(?:tool|call)\s*[=:]\s*([a-zA-Z_][a-zA-Z0-9_.-]*)")
_LEADING_IDENTIFIER = re.compile(r"^([a-zA-Z_][a-zA-Z0-9_.-]*)\s*[:=]")
_BARE_IDENTIFIER = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_.-]*$")

_KIND_BY_LABEL = {
    "goal": NodeKind.GOAL,
    "observation": NodeKind.OBSERVATION,
    "decision": NodeKind.DECISION,
    "tool-call": NodeKind.TOOL_CALL,
    "tool_call": NodeKind.TOOL_CALL,
    "tool": NodeKind.TOOL_CALL,
    "tool-result": NodeKind.TOOL_RESULT,
    "tool_result": NodeKind.TOOL_RESULT,
    "conclusion": NodeKind.CONCLUSION,
    "blocker": NodeKind.BLOCKER,
}

_STATUS_BY_LABEL = {
    "blocked": NodeStatus.BLOCKED,
    "pending": NodeStatus.ACTIVE,
    "active": NodeStatus.ACTIVE,
    "superseded": NodeStatus.SUPERSEDED,
    "complete": NodeStatus.COMPLETE,
}


class ReasoningAdapter:
    """Model-agnostic reasoning adapter (mirrors ``go/reasoning.Adapter``)."""

    def __init__(self, options: ReasoningOptions | None = None) -> None:
        self._opts = options or ReasoningOptions()
        self._model_fields: dict[str, str] = {}

    @property
    def options(self) -> ReasoningOptions:
        return self._opts

    def register_model_field(self, model: str, field_name: str) -> None:
        """Map a model name to a model-specific reasoning field name.

        Fields registered here are consulted before the known field set, so
        callers can teach the adapter names like Spark's thinking payload
        field.
        """
        if model and field_name:
            self._model_fields[model.lower()] = field_name

    def strip(self, raw: Any) -> str:
        """Remove reasoning from ``raw`` and return the clean content that
        ordinary tool-call parsers should see.

        Fails closed: malformed or unbounded input raises instead of returning
        partially-stripped output.
        """
        analysis = self._analyze(raw, "")
        return analysis.clean

    def extract(self, raw: Any, model: str = "", backend: str = "") -> ContextTree:
        """Normalize reasoning found in ``raw`` into a versioned context tree.

        Raw reasoning is never stored on the tree: nodes carry bounded
        summaries. The tree has no nodes (``root_id`` empty) when ``raw``
        contains no reasoning.
        """
        analysis = self._analyze(raw, model)

        now = (self._opts.clock or _now)()
        tree = ContextTree(
            version=CONTRACT_VERSION,
            format=analysis.format,
            model=model,
            backend=backend,
            created_at=now,
            nodes=[],
        )

        if not analysis.reasoning.strip():
            return tree

        nodes = self._parse_nodes(analysis.reasoning, now)
        tree.nodes = nodes
        if nodes:
            tree.root_id = nodes[0].id
        return tree

    def parse(self, raw: Any, model: str = "", backend: str = "") -> tuple[str, ContextTree]:
        """Strip and extract in one call: return the clean content and the
        normalized context tree for ``raw``."""
        analysis = self._analyze(raw, model)
        return analysis.clean, self.extract(raw, model, backend)

    # -- analysis -----------------------------------------------------------

    def _analyze(self, raw: Any, model: str) -> "_Analysis":
        if isinstance(raw, str):
            return self._analyze_string(raw, model)
        if isinstance(raw, bytes):
            return self._analyze_bytes(raw, model)
        if isinstance(raw, dict):
            return self._analyze_structured(raw, model)
        raise MalformedInputError(f"unsupported raw type {type(raw).__name__}")

    def _analyze_string(self, raw: str, model: str) -> "_Analysis":
        trimmed = raw.strip()
        if trimmed.startswith("{"):
            try:
                parsed = json.loads(trimmed)
            except json.JSONDecodeError as e:
                raise MalformedJSONError(str(e)) from e
            if isinstance(parsed, dict) and self._has_recognized_key(parsed):
                return self._analyze_structured(parsed, model)
        return self._analyze_text(raw)

    def _analyze_bytes(self, raw: bytes, model: str) -> "_Analysis":
        trimmed = raw.strip()
        if trimmed and trimmed[0:1] in (b"{", b"["):
            try:
                parsed = json.loads(trimmed)
            except json.JSONDecodeError as e:
                raise MalformedJSONError(str(e)) from e
            if isinstance(parsed, dict) and self._has_recognized_key(parsed):
                return self._analyze_structured(parsed, model)
        return self._analyze_text(raw.decode("utf-8", errors="replace"))

    def _analyze_structured(self, m: dict[str, Any], model: str) -> "_Analysis":
        message = m.get("message")
        if isinstance(message, dict):
            return self._analyze_structured(message, model)

        content = ""
        if "content" in m:
            value = m["content"]
            if value is None:
                content = ""
            elif isinstance(value, str):
                content = value
            else:
                raise MalformedContentError(
                    f"content field is {type(value).__name__}, not a string"
                )

        reasoning, fmt = self._extract_reasoning_field(m, model)

        if reasoning:
            clean, _, err = split_thinking_tags(content)
            if err is not None:
                raise err
            return _Analysis(reasoning=reasoning, clean=clean, format=fmt)

        clean, tags, err = split_thinking_tags(content)
        if err is not None:
            raise err
        if tags:
            return _Analysis(reasoning=tags, clean=clean, format=Format.THINKING)
        return _Analysis(reasoning="", clean=content, format=Format.NONE)

    def _extract_reasoning_field(self, m: dict[str, Any], model: str) -> tuple[str, Format]:
        if model:
            field_name = self._model_fields.get(model.lower())
            if field_name:
                value = m.get(field_name)
                if value is not None:
                    if not isinstance(value, str):
                        raise MalformedReasoningFieldError(
                            f"field {field_name!r} is {type(value).__name__}, not a string"
                        )
                    if value:
                        return value, Format.MODEL_SPECIFIC
        for field_name in KNOWN_REASONING_FIELDS:
            value = m.get(field_name)
            if value is None:
                continue
            if not isinstance(value, str):
                raise MalformedReasoningFieldError(
                    f"field {field_name!r} is {type(value).__name__}, not a string"
                )
            if value:
                return value, Format.NATIVE
        return "", Format.NONE

    def _analyze_text(self, raw: str) -> "_Analysis":
        clean, tags, err = split_thinking_tags(raw)
        if err is not None:
            raise err
        if tags:
            return _Analysis(reasoning=tags, clean=clean, format=Format.THINKING)
        return _Analysis(reasoning="", clean=raw, format=Format.NONE)

    @staticmethod
    def _has_recognized_key(m: dict[str, Any]) -> bool:
        return any(k in RECOGNIZED_KEYS for k in m)

    # -- tree building ------------------------------------------------------

    def _parse_nodes(self, reasoning: str, now: datetime) -> list[Node]:
        if len(reasoning.encode()) > self._opts.max_reasoning_bytes:
            raise ReasoningTooLargeError("reasoning exceeds maximum size")

        paragraphs = split_paragraphs(reasoning)
        if len(paragraphs) > self._opts.max_nodes:
            raise TooManyNodesError("too many reasoning nodes")

        nodes: list[Node] = []
        parent_id = ""
        depth = 0

        for para in paragraphs:
            depth += 1
            if depth > self._opts.max_depth:
                raise TooDeepError("reasoning tree too deep")

            kind, summary = classify_paragraph(para)
            status, summary = status_from_summary(summary, kind)
            summary = summary[: self._opts.max_summary_chars]
            if not summary:
                depth -= 1
                continue

            node_id = _node_id(kind, parent_id, summary)
            nodes.append(
                Node(
                    id=node_id,
                    parent_id=parent_id,
                    kind=kind,
                    status=status,
                    summary=summary,
                    evidence_refs=extract_evidence(summary),
                    tool_call_refs=extract_tool_refs(kind, summary),
                    created_at=now,
                    updated_at=now,
                )
            )
            parent_id = node_id
        return nodes


@dataclass(frozen=True)
class _Analysis:
    """Result of inspecting a raw model output."""

    reasoning: str
    clean: str
    format: Format


def split_thinking_tags(text: str) -> tuple[str, str, ReasoningError | None]:
    """Extract embedded thinking blocks from ``text``.

    Returns ``(clean, reasoning, err)``. Fails closed on unbalanced tags: an
    open tag without a matching close is an error, not partial output.
    """
    if not _THINKING_OPEN.search(text) and not _THINKING_CLOSE.search(text):
        return text, "", None

    parts: list[str] = []
    reasons: list[str] = []
    rest = text
    while True:
        open_match = _THINKING_OPEN.search(rest)
        if open_match is None:
            if _THINKING_CLOSE.search(rest):
                return "", "", UnbalancedThinkingError()
            parts.append(rest)
            break
        parts.append(rest[: open_match.start()])
        inner = rest[open_match.end() :]
        close_match = _THINKING_CLOSE.search(inner)
        if close_match is None:
            return "", "", UnbalancedThinkingError()
        reasons.append(inner[: close_match.start()])
        rest = inner[close_match.end() :]

    return "".join(parts), "\n".join(reasons).strip(), None


def split_paragraphs(text: str) -> list[str]:
    """Split reasoning text on blank lines and drop empty paragraphs."""
    return [p.strip() for p in _PARAGRAPH_SEP.split(text) if p.strip()]


def classify_paragraph(para: str) -> tuple[NodeKind, str]:
    """Strip list ornaments and an optional kind label; return kind + summary."""
    summary = _LIST_ORNAMENT.sub("", para)
    match = _KIND_PREFIX.match(summary)
    if match:
        label = match.group(1)
        kind = _KIND_BY_LABEL.get(label.lower(), NodeKind.OBSERVATION)
        return kind, summary[match.end() :].strip()
    return NodeKind.OBSERVATION, summary


def status_from_summary(summary: str, kind: NodeKind) -> tuple[NodeStatus, str]:
    """Apply a trailing status marker and infer blocker status."""
    match = _STATUS_MARKER.search(summary)
    if match:
        label = match.group(1).lower()
        return _STATUS_BY_LABEL.get(label, NodeStatus.COMPLETE), summary[: match.start()].strip()
    if kind == NodeKind.BLOCKER:
        return NodeStatus.BLOCKED, summary
    return NodeStatus.COMPLETE, summary


def extract_evidence(summary: str) -> list[str]:
    """Collect evidence references from a summary, in order, deduplicated."""
    out: list[str] = []
    seen: set[str] = set()
    for pattern in _EVIDENCE_PATTERNS:
        for match in pattern.findall(summary):
            ref = match if isinstance(match, str) else match[0]
            ref = ref.strip("[]")
            if ref and ref not in seen:
                seen.add(ref)
                out.append(ref)
    return out


def extract_tool_refs(kind: NodeKind, summary: str) -> list[str]:
    """Collect tool identifiers referenced by a tool-call or tool-result node."""
    if kind not in (NodeKind.TOOL_CALL, NodeKind.TOOL_RESULT):
        return []
    match = _LEADING_IDENTIFIER.match(summary)
    if match:
        return [match.group(1)]
    match = _TOOL_REF.search(summary)
    if match:
        return [match.group(1)]
    if _BARE_IDENTIFIER.match(summary):
        return [summary]
    return []


def _node_id(kind: NodeKind, parent_id: str, summary: str) -> str:
    """Stable, deterministic node ID (mirrors go/reasoning.nodeID)."""
    digest = hashlib.sha256(f"{kind.value}\0{parent_id}\0{summary}".encode())
    return digest.hexdigest()[:16]
