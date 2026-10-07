"""Deterministic content checks on a case's final reply text.

Every check is case-insensitive and applied to the model's final text (the
reply after the tool loop ends). There is no LLM grader: a check either
matches or it does not.
"""
import re
from dataclasses import dataclass, field


@dataclass(frozen=True)
class ContentChecks:
    """Content expectations from an ``agentware.eval-suite.v1`` case.

    ``contains_all`` -- every phrase must appear.
    ``contains_any`` -- when non-empty, at least one phrase must appear.
    ``regex`` -- every pattern must match somewhere (``re.search``).
    ``not_contains`` -- no phrase may appear (e.g. a claim of success after a
    policy deny).
    """

    contains_all: tuple[str, ...] = field(default_factory=tuple)
    contains_any: tuple[str, ...] = field(default_factory=tuple)
    regex: tuple[str, ...] = field(default_factory=tuple)
    not_contains: tuple[str, ...] = field(default_factory=tuple)

    def is_empty(self) -> bool:
        return not (self.contains_all or self.contains_any or self.regex or self.not_contains)


def check_content(checks: ContentChecks, text: str) -> tuple[bool, str]:
    """Return ``(ok, reason)`` for ``text`` against ``checks``."""
    lowered = text.lower()

    missing = [p for p in checks.contains_all if p.lower() not in lowered]
    if missing:
        return False, f"final text is missing required phrases: {missing}"

    if checks.contains_any and not any(p.lower() in lowered for p in checks.contains_any):
        return False, f"final text contains none of: {list(checks.contains_any)}"

    for pattern in checks.regex:
        if not re.search(pattern, text, re.IGNORECASE):
            return False, f"final text does not match regex {pattern!r}"

    present = [p for p in checks.not_contains if p.lower() in lowered]
    if present:
        return False, f"final text contains forbidden phrases: {present}"

    return True, ""
