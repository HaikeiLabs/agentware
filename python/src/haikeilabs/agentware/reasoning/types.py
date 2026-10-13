"""Reasoning context-tree contract types (mirrors go/reasoning/types.go)."""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any

CONTRACT_VERSION = "1"


class Format(str, Enum):
    """Shape of the input that produced a :class:`ContextTree`."""

    NATIVE = "native"
    THINKING = "thinking"
    MODEL_SPECIFIC = "model_specific"
    NONE = "none"


class NodeKind(str, Enum):
    """Closed vocabulary of context-tree node kinds."""

    GOAL = "goal"
    OBSERVATION = "observation"
    DECISION = "decision"
    TOOL_CALL = "tool-call"
    TOOL_RESULT = "tool-result"
    CONCLUSION = "conclusion"
    BLOCKER = "blocker"


class NodeStatus(str, Enum):
    """Closed vocabulary of context-tree node statuses."""

    ACTIVE = "active"
    COMPLETE = "complete"
    BLOCKED = "blocked"
    SUPERSEDED = "superseded"


@dataclass(frozen=True)
class Node:
    """A single node in the reasoning context tree.

    Nodes carry bounded summaries only; raw reasoning text never appears on a
    node.
    """

    id: str
    parent_id: str
    kind: NodeKind
    status: NodeStatus
    summary: str
    created_at: datetime
    updated_at: datetime
    evidence_refs: list[str] = field(default_factory=list)
    tool_call_refs: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "parent_id": self.parent_id,
            "kind": self.kind.value,
            "status": self.status.value,
            "summary": self.summary,
            "evidence_refs": list(self.evidence_refs),
            "tool_call_refs": list(self.tool_call_refs),
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }


@dataclass
class ContextTree:
    """The versioned, normalized representation of a model's reasoning.

    ``root_id`` points at the root node (the first node when the parser
    produced the tree); it is empty when no reasoning was detected.
    """

    version: str = CONTRACT_VERSION
    format: Format = Format.NONE
    model: str = ""
    backend: str = ""
    created_at: datetime = field(default_factory=datetime.now)
    root_id: str = ""
    nodes: list[Node] = field(default_factory=list)

    def empty(self) -> bool:
        """Return True when the tree holds no reasoning nodes."""
        return not self.nodes

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "format": self.format.value,
            "model": self.model,
            "backend": self.backend,
            "created_at": self.created_at.isoformat(),
            "root_id": self.root_id,
            "nodes": [n.to_dict() for n in self.nodes],
        }
