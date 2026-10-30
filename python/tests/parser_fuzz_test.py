"""Property tests for the parsers, mirroring the Go fuzz targets (#278).

Covers the three targets of ``go/toolformat/fuzz_test.go``,
``go/toolspec/fuzz_test.go`` and ``go/reasoning/fuzz_test.go``
(``typescript/tests/parser-fuzz.test.ts`` is the fast-check twin):

- tool-call parsing never raises, and every returned call carries a
  non-empty name and arguments that marshal to a JSON object
  (FuzzParseToolCalls);
- manifest v3 parsing fails closed with :class:`ManifestV3Error`, and
  accepted specs re-serialize and re-parse to the same specs (modulo
  parameters, which the v3 serializer intentionally does not write)
  (FuzzParseManifestV3);
- the reasoning entry points agree on failing closed, an error produces
  no partial output, and a successful parse yields a well-formed context
  tree (FuzzAdapter).

The Go seed corpora — the shared fixture cases plus the edge inputs — are
pinned with ``@example`` so they run on every invocation, and arbitrary
input comes from Hypothesis. The default profile (max_examples=200) runs
in normal CI; ``HYPOTHESIS_PROFILE=nightly`` (max_examples=20000) drives
the scheduled fuzz job in ``.github/workflows/fuzz.yml``.

Function names avoid the words the conformance workflow's ``pytest -k``
filter selects on (toolformat, reasoning, toolspec, manifest): recording
shared fixture cases is the fixture runners' job, not the property tests'.
"""

import json
from dataclasses import replace
from pathlib import Path

from hypothesis import example, given
from hypothesis import strategies as st

from haikeilabs.agentware.reasoning import ReasoningAdapter, ReasoningError
from haikeilabs.agentware.toolformat import get_formatter
from haikeilabs.agentware.toolspec import (
    ManifestV3Error,
    ToolSpec,
    parse_manifest_v3,
    serialize_tool_specs,
)

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures"

# go/toolformat/fuzz_test.go: fixture family -> a model name whose selector
# routes to that family's formatter, so the property exercises every
# formatter through the selector.
FAMILY_MODELS = {
    "openai": "gpt-4o",
    "glm": "glm-4",
    "deepseek": "deepseek-chat",
    "anthropic": "claude-3",
    "qwen": "qwen-2",
}


def _fixture_tool_call_seeds() -> list[tuple[str, str]]:
    """Every shared toolformat parse_calls case, keyed by selector model."""
    seeds = []
    for path in sorted((FIXTURES / "toolformat").glob("*-cases.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        model = FAMILY_MODELS[data["family"]]
        for case in data.get("parse_calls") or []:
            seeds.append((model, case["input"]))
    return seeds


def _fixture_reasoning_seeds() -> list[str]:
    """Every shared reasoning fixture raw_response, JSON-encoded like Go's
    json.Marshal(ex.RawResponse) seed."""
    seeds = []
    for path in sorted((FIXTURES / "reasoning").glob("*-cases.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        for case in data.get("examples") or []:
            seeds.append(json.dumps(case["raw_response"]))
    return seeds


def _manifest_seeds() -> list[bytes]:
    """Every shared manifest v3 fixture document, as bytes like Go's f.Add."""
    root = FIXTURES / "toolspec" / "manifest-v3"
    return [
        (root / directory / path).read_bytes()
        for directory in ("valid", "invalid")
        for path in sorted((root / directory).glob("*.json"))
    ]


# The edge-shape seeds of go/toolformat/fuzz_test.go, plus the regressions
# these properties found: the generic and Anthropic parsers used to return
# calls with an empty name, and the generic parser a call whose arguments
# were not an object.
TOOL_CALL_SEEDS = [
    *_fixture_tool_call_seeds(),
    ("llama-3", '<|python_tag|>get_weather({"location": "Tokyo"})'),
    ("mistral-7b", '[TOOL_CALLS][tool_call>get_weather: {"location": "Tokyo"}][/TOOL_CALLS]'),
    ("nemotron-70b", '[{"id":"1","name":"get_weather","arguments":{"location":"Tokyo"}}]'),
    ("gpt-4o", '[{"id":"1","name":"get_weather","arguments":"{\\"location\\":\\"Tokyo\\"}"}]'),
    ("gpt-4o", '[{"name":""}]'),
    ("gpt-4o", '[{"name":"x","arguments":null}]'),
    ("qwen-2", ""),
    ("minimax-7b", ""),
    ("some-model", '[{"name":"x","arguments":{"a":1}}]'),
    ("some-model", "not json at all"),
    ("gpt-4o", '[{"name":"","arguments":{}}]'),
    ("gpt-4o", '[{"name":"x","arguments":"null"}]'),
    ("claude-3", '<invoke tool="">{}</invoke>'),
]

# The edge-shape seeds of go/reasoning/fuzz_test.go.
REASONING_EDGE_SEEDS = [
    "",
    "hello world",
    "<thinking>why</thinking>answer",
    "<thinking>unbalanced",
    "closed</thinking>first",
    "[THINK]why[/THINK]answer",
    "[THINK]unbalanced",
    '{"content": "hi", "reasoning_content": "why"}',
    '{"content": "hi", "reasoning_content": 5}',
    '{"content": {"a": 1}}',
    '{"content": null, "reasoning": "why"}',
    '{"content": [{"type": "text", "text": "hi"}, {"type": "thinking", "thinking": "why"}]}',
    '{"content": [{"type": "text"}]}',
    '{"message": {"content": "hi", "reasoning_content": "why"}}',
    '{"choices": [{"message": {"content": "hi"}}]}',
    '{"message": 42}',
    '{"unrelated": true}',
    "{invalid",
]

REASONING_SEEDS = [*_fixture_reasoning_seeds(), *REASONING_EDGE_SEEDS]
# Invalid UTF-8 that still looks like JSON: the bytes entry point used to
# leak a UnicodeDecodeError instead of failing closed.
REASONING_BYTES_SEEDS = [seed.encode("utf-8") for seed in REASONING_SEEDS] + [
    b'{"content": "\xff"}'
]


def _pinned(seeds):
    """Pin every seed as an ``@example`` so the corpus runs on every
    invocation, the way a Go fuzz target's f.Add seed corpus does."""

    def decorate(test):
        for value in seeds:
            test = example(value)(test)
        return test

    return decorate


TOOL_CALL_STRATEGY = st.one_of(
    st.sampled_from(TOOL_CALL_SEEDS),
    st.tuples(st.text(max_size=64), st.text(max_size=4096)),
)
MANIFEST_STRATEGY = st.one_of(
    st.sampled_from(_manifest_seeds()),
    st.binary(max_size=4096),
)
REASONING_STRATEGY = st.one_of(
    st.sampled_from(REASONING_SEEDS),
    st.text(max_size=4096),
)
REASONING_BYTES_STRATEGY = st.one_of(
    st.sampled_from(REASONING_BYTES_SEEDS),
    st.binary(max_size=4096),
)


@_pinned(TOOL_CALL_SEEDS)
@given(TOOL_CALL_STRATEGY)
def test_parse_tool_calls_yield_named_calls_with_object_args(seed):
    """FuzzParseToolCalls: parsing never raises, and every returned call
    carries a non-empty name and arguments that marshal to a JSON object."""
    model, response = seed
    calls = get_formatter(model).parse_tool_calls(response)
    for call in calls:
        assert isinstance(call.name, str) and call.name, f"empty name: {call!r}"
        assert isinstance(call.args, dict), f"arguments are not an object: {call!r}"
        json.dumps(call.args)


@_pinned(_manifest_seeds())
@given(MANIFEST_STRATEGY)
def test_v3_documents_fail_closed_and_round_trip(data):
    """FuzzParseManifestV3: a rejected document is always a
    ManifestV3Error, and accepted specs re-serialize and re-parse to the
    same specs (modulo parameters, which the v3 serializer does not write)."""
    try:
        specs = parse_manifest_v3(data)
    except ManifestV3Error:
        return
    reparsed = parse_manifest_v3(json.dumps(serialize_tool_specs(specs)))
    assert _strip_parameters(reparsed) == _strip_parameters(specs)


def _strip_parameters(specs: list[ToolSpec]) -> list[ToolSpec]:
    """Clear the parameters field, which the v3 serializer intentionally does
    not write, so a spec and its round-tripped copy compare equal."""
    return [replace(spec, parameters={}) for spec in specs]


@_pinned(REASONING_SEEDS)
@given(REASONING_STRATEGY)
def test_adapter_entry_points_agree_and_fail_closed(raw):
    """FuzzAdapter: the entry points agree on failing closed, an error
    produces no partial output, and a successful parse yields a well-formed
    context tree. The bytes entry point must not raise anything but a
    ReasoningError either."""
    adapter = ReasoningAdapter()
    try:
        clean, tree = adapter.parse(raw, "fuzz-model", "fuzz-backend")
        parse_error = None
    except ReasoningError as error:
        parse_error = error
    try:
        stripped = adapter.strip(raw)
        strip_error = None
    except ReasoningError as error:
        strip_error = error
    try:
        adapter.extract(raw, "fuzz-model", "fuzz-backend")
        extract_error = None
    except ReasoningError as error:
        extract_error = error
    try:
        adapter.strip(raw.encode("utf-8"))
    except ReasoningError:
        pass

    assert (parse_error is None) == (strip_error is None)
    assert (parse_error is None) == (extract_error is None)
    if parse_error is not None:
        return
    assert stripped == clean
    assert tree is not None
    if not tree.nodes:
        assert tree.root_id == ""
        return
    assert tree.root_id == tree.nodes[0].id
    for position, node in enumerate(tree.nodes):
        want_parent = "" if position == 0 else tree.nodes[position - 1].id
        assert node.parent_id == want_parent


@_pinned(REASONING_BYTES_SEEDS)
@given(REASONING_BYTES_STRATEGY)
def test_adapter_bytes_entry_point_only_raises_reasoning_errors(data):
    """The []byte entry of go/reasoning/fuzz_test.go: arbitrary bytes never
    raise anything but a ReasoningError."""
    try:
        ReasoningAdapter().strip(data)
    except ReasoningError:
        return
