"""LangChain adapter: expose one generated operation as a LangChain tool.

``langchain_tool`` wraps an OperationRunner behind a
``langchain_core.tools.BaseTool``. Every invocation converts the tool kwargs
to the generated request with ``to_request``, runs the operation through the
runner in the caller's context, and maps the response with ``from_response``.

The model-facing name and description always come from the caller; the args
schema is derived from the generated request message type. A policy deny (or
an enrollment/connect requirement) becomes a ``ToolException`` whose message
carries the reason code — never a credential or a provider payload.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from google.protobuf.message import Message
from kei.connection import Op
from pydantic import BaseModel

from . import _TOOL_ERROR_CLASSES, _field_annotations

if TYPE_CHECKING:
    from collections.abc import Callable

    from langchain_core.tools import BaseTool
    from pydantic import BaseModel

    from haikeilabs.agentware.middleware.types import CallerContext

    RequestFactory = Callable[..., Message]
    ResponseMapper = Callable[[Any], Any]
    CallerFactory = Callable[[], CallerContext]


def _args_model(request_type: type[Message]) -> type[BaseModel]:
    """A pydantic args model with one optional field per request scalar field."""
    from pydantic import create_model

    fields = {
        name: (annotation, None) for name, annotation in _field_annotations(request_type).items()
    }
    return create_model(f"{request_type.__name__}Args", **fields)


def langchain_tool(
    runner: Any,
    caller_fn: CallerFactory,
    op: Op,
    request_type: type[Message],
    *,
    name: str,
    description: str,
    to_request: RequestFactory,
    from_response: ResponseMapper,
) -> BaseTool:
    """Expose ``op`` as a LangChain tool behind ``runner``.

    ``runner`` is anything with the OperationRunner.run shape; ``caller_fn``
    yields the :class:`CallerContext` for each invocation. The tool's
    model-facing ``name`` and ``description`` come from the caller verbatim;
    the args schema is derived from ``request_type``.
    """
    from langchain_core.tools import BaseTool, ToolException

    schema = _args_model(request_type)
    tool_name = name
    tool_description = description

    class _OperationTool(BaseTool):
        name: str = tool_name
        description: str = tool_description
        args_schema: type[BaseModel] = schema

        def _run(self, **kwargs: Any) -> Any:
            raise NotImplementedError(
                "The OperationRunner is async only; use the async interface (ainvoke)."
            )

        async def _arun(self, **kwargs: Any) -> Any:
            request = to_request(**kwargs)
            try:
                response = await runner.run(caller_fn(), op, request, tool=name)
            except _TOOL_ERROR_CLASSES as exc:
                raise ToolException(str(exc)) from exc
            return from_response(response)

    return _OperationTool(name=name, description=description)
