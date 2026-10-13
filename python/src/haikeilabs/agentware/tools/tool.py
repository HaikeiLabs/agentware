"""Tool definitions and result types."""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol, TypedDict, runtime_checkable


@runtime_checkable
class Tool(Protocol):
    """Protocol for tool implementations."""

    @property
    def name(self) -> str:
        """Tool name."""
        ...

    @property
    def description(self) -> str:
        """Tool description."""
        ...

    def execute(self, args: dict[str, Any]) -> "Result":
        """Execute the tool with given arguments."""
        ...


@dataclass
class ToolExample:
    """Example usage of a tool."""

    input: dict[str, Any]
    output: str
    explanation: str | None = None


class BaseTool(ABC):
    """Base class for tools."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Tool name."""
        pass

    @property
    @abstractmethod
    def description(self) -> str:
        """Tool description."""
        pass

    def execute(self, args: dict[str, Any]) -> "Result":
        """Execute the tool. Override in subclass for custom logic."""
        raise NotImplementedError("Subclass must implement execute")


@dataclass
class KeiScope:
    """Governance scope for a tool in the Kei policy catalog.

    Declares the source, required operations, and resource types for a governed tool.
    """

    source: str
    required_capabilities: list[str]
    resource_types: list["KeiResourceType"] = field(default_factory=list)
    operation_class: str = "read"
    service: str | None = None


class _KeiResourceTypeWithOptionalParent(TypedDict, total=False):
    parent_type: str


class KeiResourceType(_KeiResourceTypeWithOptionalParent):
    """Plain resource type identifier with an optional parent type."""

    type: str


@runtime_checkable
class GovernedTool(Protocol):
    """A tool that declares its Kei governance scope.

    Only tools implementing GovernedTool are included in the exported manifest.
    """

    @property
    def name(self) -> str: ...

    @property
    def description(self) -> str: ...

    def execute(self, args: dict[str, Any]) -> "Result": ...

    def kei_scope(self) -> KeiScope:
        """Return the Kei governance scope for this tool."""
        ...


@dataclass
class Result:
    """Result of tool execution."""

    success: bool
    data: Any = None
    error: str | None = None
    timestamp: datetime = field(default_factory=datetime.now)

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary."""
        return {
            "success": self.success,
            "data": self.data,
            "error": self.error,
            "timestamp": self.timestamp.isoformat(),
        }
