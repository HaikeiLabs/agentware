"""Model-agnostic reasoning adapter.

The adapter recognizes three shapes of model output:

- native ``reasoning_content`` fields (DeepSeek/Qwen/Kimi/GLM-style JSON)
- generic reasoning tags (every pair in ``REASONING_TAG_PAIRS``: think,
  thinking and bracket THINK; see ``tags.py``)
- model-specific reasoning fields (registered per model name)

Reasoning is stripped from the model output before ordinary tool-call parsing,
and the extracted reasoning text is normalized into a versioned context tree.
Raw reasoning is never exposed in normal replies or default audits: tree nodes
carry only bounded summaries.

The adapter fails closed. Malformed input (a reasoning field that is not a
string, unbalanced thinking tags, malformed JSON) and unbounded input
(reasoning larger than the configured byte cap, more nodes or more depth than
configured) raise errors instead of emitting partial reasoning.

Mirrors ``go/reasoning`` and ``typescript/src/reasoning``.
"""

from .adapter import (
    MalformedContentError,
    MalformedInputError,
    MalformedJSONError,
    MalformedReasoningFieldError,
    ReasoningAdapter,
    ReasoningError,
    ReasoningOptions,
    ReasoningTooLargeError,
    TooDeepError,
    TooManyNodesError,
    UnbalancedThinkingError,
)
from .tags import (
    BRACKET_THINK_CLOSE,
    BRACKET_THINK_OPEN,
    REASONING_TAG_PAIRS,
    THINK_CLOSE,
    THINK_OPEN,
    THINKING_CLOSE,
    THINKING_OPEN,
)
from .types import (
    CONTRACT_VERSION,
    ContextTree,
    Format,
    Node,
    NodeKind,
    NodeStatus,
)

__all__ = [
    "CONTRACT_VERSION",
    "ReasoningAdapter",
    "ReasoningOptions",
    "ReasoningError",
    "ContextTree",
    "Node",
    "NodeKind",
    "NodeStatus",
    "Format",
    "MalformedInputError",
    "MalformedJSONError",
    "MalformedReasoningFieldError",
    "MalformedContentError",
    "UnbalancedThinkingError",
    "ReasoningTooLargeError",
    "TooManyNodesError",
    "TooDeepError",
    "REASONING_TAG_PAIRS",
    "THINK_OPEN",
    "THINK_CLOSE",
    "THINKING_OPEN",
    "THINKING_CLOSE",
    "BRACKET_THINK_OPEN",
    "BRACKET_THINK_CLOSE",
]
