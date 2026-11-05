"""Workflow progress from application-validated observations, not model claims.

This helper never authorizes actions. One instance belongs to one workflow run.
Observations must come from the application's trusted verification boundary.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass


@dataclass(frozen=True)
class ProgressSnapshot:
    """Requirements in declaration order, readiness, and no-progress budget."""

    pending: tuple[str, ...]
    ready: bool
    no_progress: int
    exhausted: bool


class ProgressTracker:
    """Track named independent requirements and a bounded no-progress budget."""

    def __init__(self, required: list[str], max_no_progress: int = 2) -> None:
        if (
            not required
            or any(not key.strip() for key in required)
            or len(set(required)) != len(required)
        ):
            raise ValueError("required must contain distinct, nonempty keys")
        if max_no_progress < 0:
            raise ValueError("max_no_progress must be nonnegative")
        self._required = tuple(required)
        self._satisfied: set[str] = set()
        self._max = max_no_progress
        self._failures = 0

    def observe(self, key: str, present: bool = True) -> ProgressSnapshot:
        """Record a verified fact or invalidate it. Only a new fact resets failures."""
        if key not in self._required:
            raise ValueError("unknown requirement")
        if not isinstance(present, bool):
            raise TypeError("present must be a boolean")
        if present and key not in self._satisfied:
            self._satisfied.add(key)
            self._failures = 0
        elif not present:
            self._satisfied.discard(key)
        return self.snapshot()

    def observe_many(self, observations: Iterable[tuple[str, bool]]) -> ProgressSnapshot:
        """Apply verified facts atomically after checking all keys and values."""
        batch = list(observations)
        if len({key for key, _ in batch}) != len(batch):
            raise ValueError("duplicate requirement in observation batch")
        for key, present in batch:
            if key not in self._required:
                raise ValueError("unknown requirement")
            if not isinstance(present, bool):
                raise TypeError("present must be a boolean")
        for key, present in batch:
            self.observe(key, present)
        return self.snapshot()

    def failure(self) -> ProgressSnapshot:
        """Count one unsuccessful attempt; a separate total budget remains necessary."""
        self._failures += 1
        return self.snapshot()

    def snapshot(self) -> ProgressSnapshot:
        """Read state without mutating it."""
        pending = tuple(key for key in self._required if key not in self._satisfied)
        return ProgressSnapshot(
            pending=pending,
            ready=not pending,
            no_progress=self._failures,
            exhausted=self._failures > self._max,
        )
