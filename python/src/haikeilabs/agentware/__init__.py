"""Pedro Agentware - Agent middleware and tooling for LLM tool calling."""

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _version

try:
    # pyproject.toml is the single version source.
    __version__ = _version("haikeilabs-agentware")
except PackageNotFoundError:  # running from a source tree without an install
    __version__ = "0+unknown"

from .executor import Executor
from .jobs import Job, JobManager
from .llm import Backend, Message
from .llmcontext import ContextManager
from .middleware import Auditor, Middleware, PolicyEvaluator
from .prompts import PromptGenerator
from .reasoning import ReasoningAdapter
from .toolformat import ToolFormatter
from .tools import Result, Tool, ToolRegistry

__all__ = [
    "Tool",
    "ToolRegistry",
    "Result",
    "Middleware",
    "PolicyEvaluator",
    "Auditor",
    "Executor",
    "Job",
    "JobManager",
    "Message",
    "Backend",
    "ContextManager",
    "PromptGenerator",
    "ToolFormatter",
    "ReasoningAdapter",
]
