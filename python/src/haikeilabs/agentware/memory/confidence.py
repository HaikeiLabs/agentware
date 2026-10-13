"""Glue between vault claims and the Markov link network contract.

``build_inference_request`` turns ``memory_get_claims`` output into the
JSON contract (``go/memory/infer/schema.json``); ``merge_confidence``
folds an inference response back into claim dicts so the agent can rewrite
pages — through the enforced write path — with updated ``confidence`` and
``contested`` values.
"""

from typing import Any


def claim_node_id(page: str, claim_id: str) -> str:
    """Global node id for a claim: '<page>#<claim-id>'."""
    return f"{page}#{claim_id}"


def _resolve_ref(page: str, ref: str) -> str:
    return ref if "#" in ref else claim_node_id(page, ref)


def build_inference_request(
    claims: list[dict[str, Any]],
    supersedes: list[tuple[str, str]] | None = None,
    reliability: dict[str, float] | None = None,
    weights: dict[str, float] | None = None,
) -> dict[str, Any]:
    """Build the inference request for a vault's claims.

    claims is ``memory_get_claims`` output. supersedes lists
    (newer_source_id, superseded_source_id) pairs. reliability overrides
    per-source priors.
    """
    reliability = reliability or {}
    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, str]] = []
    source_ids: set[str] = set()

    for claim in claims:
        node = claim_node_id(claim["page"], claim["id"])
        nodes.append({"id": node, "kind": "claim"})
        for source_id in claim.get("sources") or []:
            source_ids.add(source_id)
            edges.append({"type": "sourceOf", "source": source_id, "target": node})
        for ref in claim.get("supports") or []:
            edges.append(
                {"type": "supports", "source": node, "target": _resolve_ref(claim["page"], ref)}
            )
        for ref in claim.get("contradicts") or []:
            edges.append(
                {
                    "type": "contradicts",
                    "source": node,
                    "target": _resolve_ref(claim["page"], ref),
                }
            )

    for newer, older in supersedes or []:
        source_ids.update((newer, older))
        edges.append({"type": "supersedes", "source": newer, "target": older})

    for source_id in sorted(source_ids):
        source_node: dict[str, Any] = {"id": source_id, "kind": "source"}
        if source_id in reliability:
            source_node["reliability"] = reliability[source_id]
        nodes.append(source_node)

    request: dict[str, Any] = {"nodes": nodes, "edges": edges}
    if weights:
        request["weights"] = weights
    return request


def merge_confidence(
    claims: list[dict[str, Any]], response: dict[str, Any]
) -> list[dict[str, Any]]:
    """Return claim dicts with confidence/contested from an inference response."""
    marginals = response.get("marginals", {})
    contested = response.get("contested", {})
    merged = []
    for claim in claims:
        node = claim_node_id(claim["page"], claim["id"])
        updated = dict(claim)
        if node in marginals:
            updated["confidence"] = marginals[node]
            updated["contested"] = bool(contested.get(node, False))
        merged.append(updated)
    return merged
