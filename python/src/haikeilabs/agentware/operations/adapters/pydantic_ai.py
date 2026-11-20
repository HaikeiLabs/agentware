"""Pydantic AI adapter: register one generated operation as an agent tool.

``register_pydantic_ai_tool`` registers a tool function on a
``pydantic_ai.Agent`` that runs ``op`` through the OperationRunner. The args
schema is built from the generated request message type; the model-facing
name and description always come from the caller.

A policy deny (or an enrollment/connect requirement) propagates as the
runner's own error — pydantic-ai surfaces tool exceptions to the caller as
they are, and the message carries the reason code, never a credential or a
provider payload.
"""

from __future__ import annotations

import inspect
from typing import TYPE_CHECKING, Any

from google.protobuf.message import Message
from kei.connection import Op

from . import _field_annotations

if TYPE_CHECKING:
    from collections.abc import Callable

    from haikeilabs.agentware.middleware.types import CallerContext

    RequestFactory = Callable[..., Message]
    ResponseMapper = Callable[[Any], Any]
    CallerFactory = Callable[[], CallerContext]


def register_pydantic_ai_tool(
    agent: Any,
    op: Op,
    request_type: type[Message],
    *,
    name: str,
    description: str,
    to_request: RequestFactory,
    from_response: ResponseMapper,
    runner: Any,
    caller_fn: CallerFactory,
) -> None:
    """Register ``op`` as a tool on ``agent``, executed through ``runner``.

    ``runner`` is anything with the OperationRunner.run shape; ``caller_fn``
    yields the :class:`CallerContext` for each invocation. The tool's
    model-facing ``name`` and ``description`` come from the caller verbatim;
    the args schema is derived from ``request_type``.
    """
    annotations = _field_annotations(request_type)
    parameters = [
        inspect.Parameter(
            field_name,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            default=None,
            annotation=annotation,
        )
        for field_name, annotation in annotations.items()
    ]

    async def _tool(**kwargs: Any) -> Any:
        request = to_request(**kwargs)
        response = await runner.run(caller_fn(), op, request, tool=name)
        return from_response(response)

    _tool.__name__ = name
    _tool.__qualname__ = name
    _tool.__doc__ = description
    _tool.__annotations__ = {**annotations, "return": Any}
    _tool.__signature__ = inspect.Signature(parameters)
    # tool_plain: the runner carries the caller's identity via caller_fn; the
    # tool function itself takes no RunContext.
    agent.tool_plain(_tool)
