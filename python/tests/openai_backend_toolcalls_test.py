"""Native tool-call decoding is fail-closed (HAI-1107 item 3, AW-P2).

Mirrors Go #300's serverBackend: arguments that are not a JSON object (or a
string holding one) and nameless calls are refused, never silently run with
empty arguments.
"""

import json

import httpx
import pytest

from haikeilabs.agentware.llm import Message, OpenAIBackend, Role
from haikeilabs.agentware.llm.openai_backend import parse_tool_calls


def _call(name="echo", arguments="{}"):
    entry = {"id": "call_1", "type": "function", "function": {"name": name}}
    if arguments is not None:
        entry["function"]["arguments"] = arguments
    return entry


def test_parse_tool_calls_decodes_native_shapes():
    """String arguments, object arguments, empty and null arguments decode."""
    calls = parse_tool_calls(
        [
            _call("hn_top_stories", '{"limit":5}'),
            _call("echo", {"text": "hi"}),
            _call("no_args", ""),
            _call("null_args", None),
        ]
    )

    assert [c.name for c in calls] == ["hn_top_stories", "echo", "no_args", "null_args"]
    assert calls[0].arguments == {"limit": 5}
    assert calls[1].arguments == {"text": "hi"}
    assert calls[2].arguments == {}
    assert calls[3].arguments == {}
    assert [c.id for c in calls] == ["call_1"] * 4


def test_parse_tool_calls_refuses_unparseable_arguments():
    """Arguments that are not JSON at all are an error, not {}."""
    with pytest.raises(ValueError, match="echo"):
        parse_tool_calls([_call("echo", "not json")])


def test_parse_tool_calls_refuses_non_object_arguments():
    """A JSON string or array arguments value is refused."""
    with pytest.raises(ValueError, match="not a JSON object"):
        parse_tool_calls([_call("echo", '"just a string"')])
    with pytest.raises(ValueError, match="not a JSON object"):
        parse_tool_calls([_call("echo", "[1, 2]")])
    with pytest.raises(ValueError, match="not a JSON object"):
        parse_tool_calls([_call("echo", 5)])


def test_parse_tool_calls_refuses_nameless_calls():
    """A call without a function name is refused, not skipped."""
    with pytest.raises(ValueError, match="without a name"):
        parse_tool_calls([_call(name=None, arguments="{}")])


def test_backend_complete_fails_on_malformed_arguments():
    """OpenAIBackend.complete fails instead of returning a nameless {} call."""
    body = {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "tool_calls": [_call("echo", "not json")],
                },
                "finish_reason": "tool_calls",
            }
        ]
    }

    def respond(request):
        return httpx.Response(200, json=body)

    backend = OpenAIBackend("m", client=httpx.Client(transport=httpx.MockTransport(respond)))
    with pytest.raises(ValueError, match="echo"):
        backend.complete([Message(role=Role.USER, content="go")])


def test_backend_complete_still_decodes_valid_native_calls():
    """The happy path still returns tool calls with ids and arguments."""
    body = {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [_call("hn_top_stories", '{"limit":5}')],
                },
                "finish_reason": "tool_calls",
            }
        ]
    }

    def respond(request):
        return httpx.Response(200, json=body)

    backend = OpenAIBackend("m", client=httpx.Client(transport=httpx.MockTransport(respond)))
    resp = backend.complete([Message(role=Role.USER, content="go")])

    assert len(resp.tool_calls) == 1
    assert resp.tool_calls[0].id == "call_1"
    assert resp.tool_calls[0].name == "hn_top_stories"
    assert resp.tool_calls[0].arguments == {"limit": 5}
