from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from typing import Any


class ErrorCategory(str, Enum):
    TIMEOUT = "timeout"
    NOT_FOUND = "not_found"
    INVALID_ARGS = "invalid_args"
    PERMISSION = "permission"
    RATE_LIMIT = "rate_limit"
    UNKNOWN = "unknown"


@dataclass
class ToolError:
    timestamp: datetime
    tool: str
    args: dict[str, Any]
    category: ErrorCategory
    message: str
    session_id: str
    retry_count: int


class ErrorTracker:
    def __init__(
        self,
        max_errors_per_tool: int = 5,
        window_duration_minutes: int = 5,
    ):
        self._errors: dict[str, list[ToolError]] = {}
        self._max_errors_per_tool = max_errors_per_tool
        self._window_duration = timedelta(minutes=window_duration_minutes)

    def set_thresholds(self, max_errors: int, window_minutes: int) -> None:
        self._max_errors_per_tool = max_errors
        self._window_duration = timedelta(minutes=window_minutes)

    def record_error(
        self,
        session_id: str,
        tool: str,
        args: dict[str, Any],
        err: Exception,
        category: ErrorCategory,
    ) -> None:
        if session_id not in self._errors:
            self._errors[session_id] = []

        retry_count = self._get_retry_count(session_id, tool)

        tool_err = ToolError(
            timestamp=datetime.now(),
            tool=tool,
            args=args,
            category=category,
            message=str(err),
            session_id=session_id,
            retry_count=retry_count,
        )

        self._errors[session_id].append(tool_err)
        self._prune_old_errors(session_id)

    def _get_retry_count(self, session_id: str, tool: str) -> int:
        return sum(1 for e in self._errors.get(session_id, []) if e.tool == tool)

    def _prune_old_errors(self, session_id: str) -> None:
        if session_id not in self._errors:
            return
        if len(self._errors[session_id]) <= self._max_errors_per_tool:
            return

        cutoff = datetime.now() - self._window_duration
        self._errors[session_id] = [
            e for e in self._errors[session_id] if e.timestamp > cutoff
        ]

    def get_error_count(self, session_id: str, tool: str) -> int:
        return sum(1 for e in self._errors.get(session_id, []) if e.tool == tool)

    def get_recent_errors(self, session_id: str) -> list[ToolError]:
        self._prune_old_errors(session_id)
        return list(self._errors.get(session_id, []))

    def get_errors_by_category(
        self, session_id: str, category: ErrorCategory
    ) -> list[ToolError]:
        return [
            e
            for e in self._errors.get(session_id, [])
            if e.category == category
        ]

    def is_error_rate_exceeded(self, session_id: str, tool: str) -> bool:
        return self.get_error_count(session_id, tool) >= self._max_errors_per_tool

    def reset_session(self, session_id: str) -> None:
        self._errors.pop(session_id, None)

    def should_block_tool(self, session_id: str, tool: str) -> bool:
        return self.is_error_rate_exceeded(session_id, tool)
