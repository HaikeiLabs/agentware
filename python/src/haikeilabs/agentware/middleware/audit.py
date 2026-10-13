"""Audit logging for middleware."""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

from .types import Action, Decision


@dataclass
class AuditRecord:
    """Record of a tool call decision.

    The delegation fields mirror ``middleware.CallerContext``: ``invoking_subject``
    is the human who originated the request, carried unchanged across every
    delegation hop; ``parent_span`` and ``delegation_depth`` record where in the
    chain the call was made; ``framework`` names the adapter/harness that
    originated the call.
    """

    session_id: str
    tool_name: str
    args: dict[str, Any]
    decision: Decision
    timestamp: datetime = field(default_factory=datetime.now)
    invoking_subject: str = ""
    parent_span: str = ""
    delegation_depth: int = 0
    framework: str = ""


class AuditFilter:
    """Filter for querying audit records."""

    def __init__(
        self,
        session_id: str = "",
        tool_name: str = "",
        action: Action | None = None,
        since: datetime | None = None,
        limit: int = 0,
        parent_span: str = "",
        invoking_subject: str = "",
    ) -> None:
        self.session_id = session_id
        self.tool_name = tool_name
        self.action = action
        self.since = since
        self.limit = limit
        self.parent_span = parent_span
        self.invoking_subject = invoking_subject


class Auditor(Protocol):
    """Protocol for auditors."""

    def record(self, record: AuditRecord) -> None:
        """Record an audit entry."""
        ...

    def query(self, filter: AuditFilter) -> list[AuditRecord]:
        """Query audit records."""
        ...


class InMemoryAuditor:
    """In-memory auditor for testing and development."""

    def __init__(self) -> None:
        self._records: list[AuditRecord] = []

    def record(self, record: AuditRecord) -> None:
        """Record an audit entry."""
        self._records.append(record)

    def query(self, filter: AuditFilter) -> list[AuditRecord]:
        """Query audit records."""
        results = []
        for r in self._records:
            if filter.session_id and r.session_id != filter.session_id:
                continue
            if filter.tool_name and r.tool_name != filter.tool_name:
                continue
            if filter.action and r.decision.action != filter.action:
                continue
            if filter.since and r.timestamp < filter.since:
                continue
            if getattr(filter, "parent_span", "") and r.parent_span != filter.parent_span:
                continue
            if (
                getattr(filter, "invoking_subject", "")
                and r.invoking_subject != filter.invoking_subject
            ):
                continue
            results.append(r)
            if filter.limit > 0 and len(results) >= filter.limit:
                break
        return results

    def clear(self) -> None:
        """Clear all records."""
        self._records.clear()
