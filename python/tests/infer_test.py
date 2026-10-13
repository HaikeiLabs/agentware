"""Unit tests for the Markov link network inference engine (pgmpy)."""

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("pgmpy", reason="install the 'inference' extra")

from haikeilabs.agentware.memory.infer import InferenceError, infer  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[2]
SCHEMA_PATH = REPO_ROOT / "go/memory/infer/schema.json"


def reliable(node_id: str) -> dict[str, Any]:
    return {"id": node_id, "kind": "source", "reliability": 0.9}


def claim(node_id: str) -> dict[str, Any]:
    return {"id": node_id, "kind": "claim"}


def edge(etype: str, source: str, target: str) -> dict[str, str]:
    return {"type": etype, "source": source, "target": target}


def assert_matches_response_contract(response: dict[str, Any]) -> None:
    assert set(response) == {"marginals", "contested", "method"}
    assert response["method"] in ("belief_propagation", "gibbs")
    for value in response["marginals"].values():
        assert 0.0 <= value <= 1.0
    for value in response["contested"].values():
        assert isinstance(value, bool)


def test_mutually_supporting_claims_from_reliable_sources() -> None:
    response = infer(
        {
            "nodes": [claim("c1"), claim("c2"), reliable("s1"), reliable("s2")],
            "edges": [
                edge("sourceOf", "s1", "c1"),
                edge("sourceOf", "s2", "c2"),
                edge("supports", "c1", "c2"),
                edge("supports", "c2", "c1"),
            ],
        }
    )
    assert_matches_response_contract(response)
    assert response["marginals"]["c1"] > 0.85
    assert response["marginals"]["c2"] > 0.85
    assert response["contested"] == {"c1": False, "c2": False}


def test_contradicted_claim_from_superseded_source() -> None:
    response = infer(
        {
            "nodes": [
                claim("c1"),
                claim("c2"),
                {"id": "s1", "kind": "source"},
                reliable("s2"),
                {"id": "s3", "kind": "source"},
            ],
            "edges": [
                edge("sourceOf", "s1", "c1"),
                edge("sourceOf", "s2", "c2"),
                edge("contradicts", "c2", "c1"),
                edge("supersedes", "s3", "s1"),
            ],
        }
    )
    assert_matches_response_contract(response)
    assert response["marginals"]["c1"] < 0.5
    assert response["marginals"]["c2"] > 0.6
    assert response["contested"]["c1"] is True
    assert response["contested"]["c2"] is False


def test_support_cycle_converges_or_falls_back() -> None:
    response = infer(
        {
            "nodes": [claim("c1"), claim("c2"), claim("c3"), reliable("s1")],
            "edges": [
                edge("sourceOf", "s1", "c1"),
                edge("supports", "c1", "c2"),
                edge("supports", "c2", "c3"),
                edge("supports", "c3", "c1"),
            ],
        }
    )
    assert_matches_response_contract(response)
    # The cycle must not break inference: all three land on the same side,
    # pulled up by c1's reliable source.
    for claim_id in ("c1", "c2", "c3"):
        assert response["marginals"][claim_id] > 0.5


def test_invalid_requests_rejected() -> None:
    with pytest.raises(InferenceError):
        infer({"nodes": [], "edges": []})
    with pytest.raises(InferenceError):
        infer({"nodes": [claim("c1")], "edges": [edge("supports", "c1", "ghost")]})
    with pytest.raises(InferenceError):
        infer({"nodes": [claim("c1")], "edges": [edge("frobnicates", "c1", "c1")]})


def test_subprocess_contract_roundtrip() -> None:
    request = {
        "nodes": [claim("c1"), reliable("s1")],
        "edges": [edge("sourceOf", "s1", "c1")],
    }
    proc = subprocess.run(
        [sys.executable, "-m", "haikeilabs.agentware.memory.infer"],
        input=json.dumps(request),
        capture_output=True,
        text=True,
        check=True,
    )
    response = json.loads(proc.stdout)
    assert_matches_response_contract(response)
    assert response["marginals"]["c1"] > 0.5

    bad = subprocess.run(
        [sys.executable, "-m", "haikeilabs.agentware.memory.infer"],
        input="{}",
        capture_output=True,
        text=True,
    )
    assert bad.returncode == 1
    assert "error" in json.loads(bad.stdout)


def test_schema_file_declares_the_contract() -> None:
    schema = json.loads(SCHEMA_PATH.read_text())
    defs = schema["$defs"]
    assert set(defs["response"]["properties"]) == {"marginals", "contested", "method"}
    edge_types = defs["request"]["properties"]["edges"]["items"]["properties"]["type"]["enum"]
    assert set(edge_types) == {"supports", "contradicts", "sourceOf", "supersedes"}
