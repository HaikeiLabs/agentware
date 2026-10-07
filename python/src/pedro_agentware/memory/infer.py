"""Markov link network inference over wiki-memory claims.

Implements the JSON contract in ``go/memory/infer/schema.json``: claim and
source-reliability nodes with binary states, typed edges (supports,
contradicts, sourceOf, supersedes) turned into pairwise potentials, and
per-claim marginals as probabilities. Belief propagation first; Gibbs
sampling fallback if it fails.

Runnable as a subprocess (the Go core's transport)::

    python -m pedro_agentware.memory.infer < request.json > response.json

Requires the ``inference`` extra (pgmpy).
"""

import json
import sys
from dataclasses import dataclass, field
from typing import Any

DEFAULT_WEIGHTS = {
    "supports": 0.8,
    "contradicts": 0.9,
    "sourceOf": 0.85,
    "sourcePrior": 0.7,
    "supersededPrior": 0.4,
    "contestedThreshold": 0.6,
}

EDGE_TYPES = {"supports", "contradicts", "sourceOf", "supersedes"}


class InferenceError(ValueError):
    """Invalid inference request."""


@dataclass
class Network:
    """Validated request: nodes, typed edges, effective weights."""

    claims: list[str]
    sources: list[str]
    reliability: dict[str, float]
    edges: list[tuple[str, str, str]]  # (type, source, target)
    weights: dict[str, float]
    contradicts_pairs: list[tuple[str, str]] = field(default_factory=list)


def _build_network(request: dict[str, Any]) -> Network:
    weights = {**DEFAULT_WEIGHTS, **(request.get("weights") or {})}
    claims: list[str] = []
    sources: list[str] = []
    reliability: dict[str, float] = {}
    seen: set[str] = set()
    for node in request.get("nodes", []):
        node_id, kind = node.get("id"), node.get("kind")
        if not node_id or kind not in ("claim", "source"):
            raise InferenceError(f"bad node: {node}")
        if node_id in seen:
            raise InferenceError(f"duplicate node id: {node_id}")
        seen.add(node_id)
        if kind == "claim":
            claims.append(node_id)
        else:
            sources.append(node_id)
            reliability[node_id] = float(node.get("reliability", weights["sourcePrior"]))
    if not claims:
        raise InferenceError("no claim nodes")

    superseded: set[str] = set()
    edges: list[tuple[str, str, str]] = []
    contradicts_pairs: list[tuple[str, str]] = []
    for edge in request.get("edges", []):
        etype, src, dst = edge.get("type"), edge.get("source"), edge.get("target")
        if etype not in EDGE_TYPES:
            raise InferenceError(f"bad edge type: {etype}")
        if src not in seen or dst not in seen:
            raise InferenceError(f"edge references unknown node: {edge}")
        if etype == "supersedes":
            superseded.add(dst)
            continue
        edges.append((etype, src, dst))
        if etype == "contradicts":
            contradicts_pairs.append((src, dst))
    for source_id in superseded:
        if source_id in reliability and "reliability" not in _node_by_id(request, source_id):
            reliability[source_id] = weights["supersededPrior"]
    return Network(
        claims=claims,
        sources=sources,
        reliability=reliability,
        edges=edges,
        weights=weights,
        contradicts_pairs=contradicts_pairs,
    )


def _node_by_id(request: dict[str, Any], node_id: str) -> dict[str, Any]:
    for node in request.get("nodes", []):
        if node.get("id") == node_id:
            return node
    return {}


def _build_model(net: Network) -> Any:
    from pgmpy.factors.discrete import DiscreteFactor  # type: ignore[import-untyped]

    try:  # pgmpy >= 1.0
        from pgmpy.models import (  # type: ignore[import-untyped]
            DiscreteMarkovNetwork as MarkovNetwork,
        )
    except ImportError:  # pragma: no cover - older pgmpy
        from pgmpy.models import MarkovNetwork  # type: ignore[import-untyped]

    model = MarkovNetwork()
    model.add_nodes_from(net.claims + net.sources)

    def agreement(a: str, b: str, w: float) -> Any:
        return DiscreteFactor([a, b], [2, 2], [w, 1 - w, 1 - w, w])

    def disagreement(a: str, b: str, w: float) -> Any:
        return DiscreteFactor([a, b], [2, 2], [1 - w, w, w, 1 - w])

    connected = {n for _, src, dst in net.edges for n in (src, dst)}
    factors = []
    for source_id in net.sources:
        if source_id not in connected:
            # A source with no remaining edges (e.g. one that only
            # supersedes) carries no information; keeping it disconnects
            # the graph for belief propagation.
            model.remove_node(source_id)
            continue
        r = net.reliability[source_id]
        factors.append(DiscreteFactor([source_id], [2], [1 - r, r]))
    for claim_id in net.claims:
        # Weak uniform prior keeps isolated claims well-defined.
        factors.append(DiscreteFactor([claim_id], [2], [0.5, 0.5]))
    for etype, src, dst in net.edges:
        if src == dst:
            raise InferenceError(f"self edge on {src}")
        model.add_edge(src, dst)
        if etype == "supports":
            factors.append(agreement(src, dst, net.weights["supports"]))
        elif etype == "contradicts":
            factors.append(disagreement(src, dst, net.weights["contradicts"]))
        elif etype == "sourceOf":
            factors.append(agreement(src, dst, net.weights["sourceOf"]))
    model.add_factors(*factors)
    return model


def _marginals_bp(model: Any, claims: list[str]) -> dict[str, float]:
    from pgmpy.inference import BeliefPropagation  # type: ignore[import-untyped]

    bp = BeliefPropagation(model)
    bp.calibrate()
    out: dict[str, float] = {}
    for claim_id in claims:
        marginal = bp.query([claim_id], show_progress=False)
        out[claim_id] = float(marginal.values[1])
    return out


def _marginals_gibbs(model: Any, claims: list[str], samples: int = 4000) -> dict[str, float]:
    from pgmpy.sampling import GibbsSampling  # type: ignore[import-untyped]

    gibbs = GibbsSampling(model)
    frame = gibbs.sample(size=samples, seed=7)
    return {claim_id: float(frame[claim_id].mean()) for claim_id in claims}


def infer(request: dict[str, Any]) -> dict[str, Any]:
    """Run inference per the JSON contract; returns the response object."""
    net = _build_network(request)
    model = _build_model(net)
    try:
        marginals = _marginals_bp(model, net.claims)
        method = "belief_propagation"
    except Exception:  # noqa: BLE001 - any BP failure falls back to sampling
        marginals = _marginals_gibbs(model, net.claims)
        method = "gibbs"

    threshold = net.weights["contestedThreshold"]
    contested = {claim_id: False for claim_id in net.claims}
    for a, b in net.contradicts_pairs:
        if a in contested and marginals.get(b, 0.0) > threshold:
            contested[a] = True
        if b in contested and marginals.get(a, 0.0) > threshold:
            contested[b] = True
    return {
        "marginals": {k: round(v, 6) for k, v in marginals.items()},
        "contested": contested,
        "method": method,
    }


def main() -> int:
    """Subprocess entry point: JSON request on stdin, response on stdout."""
    try:
        request = json.load(sys.stdin)
        response = infer(request)
    except Exception as exc:  # noqa: BLE001 - errors go on the wire
        json.dump({"error": str(exc)}, sys.stdout)
        return 1
    json.dump(response, sys.stdout)
    return 0


if __name__ == "__main__":
    sys.exit(main())
