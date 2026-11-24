"""Reasoning-tag constants shared by the reasoning adapter and the response
validator (``middleware.guardrails``), so both recognize the same tags.

Every tag is built from parts and is never typed as one literal. The tags are
a blind spot for reasoning-model workers: a model that emits these tags itself
cannot reliably read or type them (it writes a different tag, or the tag comes
out garbled), so a literal tag in source is an edit waiting to be corrupted.
Keep it that way: never write a reasoning tag literally in source, tests or
docs; build it from parts or use these constants.

Mirrors ``go/reasoning/tags.go`` and ``typescript/src/reasoning/tags.ts``.
"""

from typing import Final

_LT: Final = "<"
_GT: Final = ">"
_LB: Final = "["
_RB: Final = "]"
_SLASH: Final = "/"

_THINK: Final = "think"
_THINKING: Final = _THINK + "ing"
_BRACKET_THINK: Final = _THINK.upper()

#: Opens the plain think block that Qwen3, QwQ, DeepSeek-R1 and GLM emit when
#: served without a reasoning parser.
THINK_OPEN: Final = _LT + _THINK + _GT
#: Closes a :data:`THINK_OPEN` block.
THINK_CLOSE: Final = _LT + _SLASH + _THINK + _GT

#: Opens the generic thinking block.
THINKING_OPEN: Final = _LT + _THINKING + _GT
#: Closes a :data:`THINKING_OPEN` block.
THINKING_CLOSE: Final = _LT + _SLASH + _THINKING + _GT

#: Opens the bracketed THINK block (Mistral/Magistral).
BRACKET_THINK_OPEN: Final = _LB + _BRACKET_THINK + _RB
#: Closes a :data:`BRACKET_THINK_OPEN` block.
BRACKET_THINK_CLOSE: Final = _LB + _SLASH + _BRACKET_THINK + _RB

#: Every ``(open, close)`` reasoning-tag pair the adapter and the validator
#: recognize, in a fixed order: think, thinking, bracket THINK. Matching is
#: case-insensitive.
REASONING_TAG_PAIRS: Final[tuple[tuple[str, str], ...]] = (
    (THINK_OPEN, THINK_CLOSE),
    (THINKING_OPEN, THINKING_CLOSE),
    (BRACKET_THINK_OPEN, BRACKET_THINK_CLOSE),
)

__all__ = [
    "BRACKET_THINK_CLOSE",
    "BRACKET_THINK_OPEN",
    "REASONING_TAG_PAIRS",
    "THINKING_CLOSE",
    "THINKING_OPEN",
    "THINK_CLOSE",
    "THINK_OPEN",
]
