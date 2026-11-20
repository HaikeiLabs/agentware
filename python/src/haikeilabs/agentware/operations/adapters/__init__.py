"""Framework adapters for the Python OperationRunner (LangChain, Pydantic AI).

Build-ahead draft mirroring the TypeScript runner (agentware#325) with Python
idioms. Each adapter wraps the same runner behind a framework tool: the tool
kwargs become the generated request (``to_request``), every call is authorized
and audited by the runner in the caller's context, and the response is mapped
back with ``from_response``.

The model-facing tool name and description always come from the caller. Tool
errors stay readable and carry reason codes only — never a credential or a
provider payload.

The framework imports are lazy (PEP 562): ``langchain_tool`` needs the
``langchain`` extra, ``register_pydantic_ai_tool`` needs the ``pydantic-ai``
extra; either can be used without the other installed.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol

from google.protobuf.descriptor import FieldDescriptor
from google.protobuf.message import Message
from kei.connection import Op

from ..runner import ConnectRequiredError, EnrollmentRequiredError, OperationDeniedError

if TYPE_CHECKING:
    from haikeilabs.agentware.middleware.types import CallerContext

__all__ = [
    "OperationToolRunner",
    "langchain_tool",
    "register_pydantic_ai_tool",
]

# Generated request fields are protobuf scalars; map them to the Python
# annotations the tool schemas expose to the model. Anything else (message,
# repeated, map) stays ``Any``.
_SCALAR_ANNOTATIONS: dict[int, type] = {
    FieldDescriptor.TYPE_STRING: str,
    FieldDescriptor.TYPE_BYTES: bytes,
    FieldDescriptor.TYPE_BOOL: bool,
    FieldDescriptor.TYPE_FLOAT: float,
    FieldDescriptor.TYPE_DOUBLE: float,
    FieldDescriptor.TYPE_INT32: int,
    FieldDescriptor.TYPE_INT64: int,
    FieldDescriptor.TYPE_UINT32: int,
    FieldDescriptor.TYPE_UINT64: int,
    FieldDescriptor.TYPE_SINT32: int,
    FieldDescriptor.TYPE_SINT64: int,
    FieldDescriptor.TYPE_FIXED32: int,
    FieldDescriptor.TYPE_FIXED64: int,
    FieldDescriptor.TYPE_SFIXED32: int,
    FieldDescriptor.TYPE_SFIXED64: int,
    FieldDescriptor.TYPE_ENUM: int,
}

# The runner errors a tool surfaces as a readable tool error; the payloads of
# the enrollment/connect errors pass back in the exception, reason codes in
# the message, never a credential.
_TOOL_ERROR_CLASSES = (OperationDeniedError, EnrollmentRequiredError, ConnectRequiredError)


class OperationToolRunner(Protocol):
    """Anything with the :class:`~haikeilabs.agentware.operations.OperationRunner` run shape.

    The real runner satisfies it; tests substitute a fake.
    """

    async def run(
        self,
        caller: CallerContext,
        op: Op,
        request: Message,
        *,
        tool: str = "",
        resource_template_override: str | None = None,
    ) -> Any: ...


def _field_annotations(request_type: type[Message]) -> dict[str, Any]:
    """One optional Python annotation per scalar field of ``request_type``."""
    annotations: dict[str, Any] = {}
    for field in request_type.DESCRIPTOR.fields_by_name.values():
        annotation = _SCALAR_ANNOTATIONS.get(field.type, Any)
        annotations[field.name] = annotation | None
    return annotations


def __getattr__(name: str) -> Any:
    """Resolve the adapters lazily so each framework extra stays optional."""
    if name == "langchain_tool":
        from .langchain import langchain_tool

        return langchain_tool
    if name == "register_pydantic_ai_tool":
        from .pydantic_ai import register_pydantic_ai_tool

        return register_pydantic_ai_tool
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
