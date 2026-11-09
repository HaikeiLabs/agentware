import json
from pathlib import Path

from haikeilabs.agentware.llm.openai_backend import format_tool_call, parse_tool_calls


def test_provider_extras_fixtures():
    cases = json.loads(
        (
            Path(__file__).resolve().parents[2] / "fixtures/llm/provider-extras/cases.json"
        ).read_text()
    )
    for case in cases:
        calls = parse_tool_calls(case["response"]["choices"][0]["message"]["tool_calls"])
        assert [format_tool_call(call) for call in calls] == case["expected"]["tool_calls"], case[
            "name"
        ]


def test_backend_echoes_fixture_in_next_request():
    import httpx

    from haikeilabs.agentware.llm import Message, OpenAIBackend, Role

    cases = json.loads(
        (
            Path(__file__).resolve().parents[2] / "fixtures/llm/provider-extras/cases.json"
        ).read_text()
    )
    for case in cases:
        requests = []

        def respond(request):
            requests.append(json.loads(request.content))
            return httpx.Response(200, json=case["response"])

        backend = OpenAIBackend(
            "fixture", client=httpx.Client(transport=httpx.MockTransport(respond))
        )
        response = backend.complete([Message(role=Role.USER, content="go")])
        backend.complete([Message(role=Role.ASSISTANT, tool_calls=response.tool_calls)])
        assert requests[1]["messages"][0]["tool_calls"] == case["expected"]["tool_calls"]
        assert "fixture-signature" not in repr(response.tool_calls)
