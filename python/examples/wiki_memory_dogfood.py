"""Dogfood: an agent maintains one user's wiki memory via the enforced path.

Walks the full loop with the real Go core (memctl serve over MCP stdio) and
the real inference engine (pgmpy):

1. Ingest three overlapping sources on Go concurrency.
2. Write pages through memory_write_page — the first attempt uses a topic
   term the ontology rejects, so the agent reads the DENY diagnostics,
   corrects itself, and retries (the deny→retry cycle is logged).
3. Ingest a fourth source that CONTRADICTS an earlier claim and record the
   contradicting claim.
4. Build the Markov link network from the vault's claims, run inference,
   and write confidence back through the enforced path.

Run: PEDRO_MEMCTL=/path/to/memctl python examples/wiki_memory_dogfood.py

The tool surface used here is exactly what a LangGraph agent gets from
LangGraphMemoryTools; this script plays the agent's tool calls determinis-
tically so the flow is testable without a live LLM.
"""

import json
import tempfile
from pathlib import Path
from typing import Any

from haikeilabs.agentware.memory import (
    LangGraphMemoryTools,
    WikiMemory,
    parse_diagnostics,
)
from haikeilabs.agentware.memory.confidence import (
    build_inference_request,
    claim_node_id,
    merge_confidence,
)
from haikeilabs.agentware.memory.infer import infer

REPO_ROOT = Path(__file__).resolve().parents[2]
TBOX_PATHS = [
    str(REPO_ROOT / "ontologies/education/TBOX_LEARNING_SOFTWARE.ttl"),
    str(REPO_ROOT / "ontologies/social/twitch_topics.ttl"),
]

SOURCES = {
    "src-pprof-talk": "Talk transcript: bounded worker pools cap goroutine counts under load.",
    "src-go-blog-pipelines": "Go blog: pipelines and cancellation; worker pools bound concurrency.",
    "src-team-runbook": "Team runbook: use worker pools; goroutine leaks page the on-call.",
    "src-hn-comment": "Comment: goroutine leaks are harmless, the runtime cleans them up eventually.",
}


def page_markdown(claims: list[dict[str, Any]]) -> str:
    """Render the worker-pools page with the given claims block."""
    lines = [
        "---",
        "id: go-worker-pools",
        "type: sw:Skill",
        'labels: ["Worker Pools"]',
        "topics: [twitch:Go]",
        "claims:",
    ]
    for c in claims:
        entry = {k: v for k, v in c.items() if k not in ("page",) and v not in (None, [], False)}
        entry.setdefault("confidence", c.get("confidence"))
        lines.append("  - " + json.dumps(entry))
    lines += [
        "sources: [src-pprof-talk, src-go-blog-pipelines, src-team-runbook]",
        "---",
        "",
        "Worker pools bound concurrency. They build on [[goroutines]].",
        "",
    ]
    return "\n".join(lines)


INITIAL_CLAIMS: list[dict[str, Any]] = [
    {
        "id": "c1",
        "text": "Bounded worker pools prevent goroutine leaks under load",
        "sources": ["src-pprof-talk", "src-team-runbook"],
        "confidence": None,
    },
    {
        "id": "c2",
        "text": "Worker pools bound concurrency",
        "sources": ["src-go-blog-pipelines"],
        "supports": ["c1"],
        "confidence": None,
    },
    {
        "id": "c3",
        "text": "Goroutine leaks are harmless in practice",
        "sources": ["src-hn-comment"],
        "contradicts": ["c1"],
        "confidence": None,
    },
]


def run(root: str) -> dict[str, Any]:
    """Execute the dogfood scenario; returns a report of what happened."""
    deny_log: list[dict[str, str]] = []
    with WikiMemory(
        user_id="soypete", root=root, tbox_paths=TBOX_PATHS, session_id="dogfood"
    ) as memory:
        tools = {t.__name__: t for t in LangGraphMemoryTools(memory).tools()}

        # 1. Ingest the three overlapping sources (enforced path).
        for source_id in list(SOURCES)[:3]:
            out = tools["memory_ingest"](source_id=source_id, text=SOURCES[source_id])
            assert out.startswith("ingested"), out

        # 2. First write attempt tags the page with twitch:Golang — a label,
        #    not a concept. The ontology denies it with nearest-term
        #    diagnostics; the agent corrects and retries.
        wrong = page_markdown(INITIAL_CLAIMS[:2]).replace(
            "topics: [twitch:Go]", "topics: [twitch:Golang]"
        )
        out = tools["memory_write_page"](content=wrong)
        assert out.startswith("DENIED:"), "expected the ontology to deny twitch:Golang"
        violations = parse_diagnostics(out)
        nearest = violations[0].nearest[0].rsplit("/", 1)[-1]
        deny_log.append(
            {"attempt": "twitch:Golang", "constraint": violations[0].constraint,
             "corrected_to": f"twitch:{nearest}"}
        )
        corrected = wrong.replace("twitch:Golang", f"twitch:{nearest}")
        out = tools["memory_write_page"](content=corrected)
        assert out == "wrote go-worker-pools", out

        # 3. Fourth source contradicts c1; record the contradicting claim.
        out = tools["memory_ingest"](source_id="src-hn-comment", text=SOURCES["src-hn-comment"])
        assert out.startswith("ingested"), out
        out = tools["memory_write_page"](content=page_markdown(INITIAL_CLAIMS))
        assert out == "wrote go-worker-pools", out

        # 4. Inference over the vault's claims; write confidence back.
        claims = memory.get_claims()[0]
        baseline = infer(
            build_inference_request(
                [c for c in claims if c["id"] != "c3"],
                reliability={"src-pprof-talk": 0.9, "src-go-blog-pipelines": 0.9,
                             "src-team-runbook": 0.9},
            )
        )
        response = infer(
            build_inference_request(
                claims,
                reliability={"src-pprof-talk": 0.9, "src-go-blog-pipelines": 0.9,
                             "src-team-runbook": 0.9},
            )
        )
        merged = merge_confidence(claims, response)
        out = tools["memory_write_page"](content=page_markdown(merged))
        assert out == "wrote go-worker-pools", out
        final_claims = memory.get_claims("go-worker-pools")[0]

        # 5. Query the memory: contested claims and low-confidence claims.
        contested_text, ok, err = memory.execute(
            "memory_query", {"kind": "contested_claims"}
        )
        assert ok, err
        contested_query = json.loads(contested_text)
        low_text, ok, err = memory.execute(
            "memory_query", {"kind": "low_confidence", "threshold": 0.6}
        )
        assert ok, err
        low_confidence_query = json.loads(low_text)

    c1 = claim_node_id("go-worker-pools", "c1")
    c3 = claim_node_id("go-worker-pools", "c3")
    return {
        "deny_log": deny_log,
        "baseline_c1": baseline["marginals"][c1],
        "final_c1": response["marginals"][c1],
        "final_c3": response["marginals"][c3],
        "contested_c1": response["contested"][c1],
        "contested_c3": response["contested"][c3],
        "written_back": {
            c["id"]: {"confidence": c["confidence"], "contested": c.get("contested", False)}
            for c in final_claims
        },
        "contested_query": [c["id"] for c in contested_query],
        "low_confidence_query": [c["id"] for c in low_confidence_query],
        "method": response["method"],
    }


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        report = run(str(Path(tmp) / "memory"))
    print(json.dumps(report, indent=2))
    drop = report["baseline_c1"] - report["final_c1"]
    print(f"\nc1 confidence dropped {report['baseline_c1']:.3f} -> "
          f"{report['final_c1']:.3f} (Δ {drop:.3f}); contested={report['contested_c1']}")
    print(f"deny→retry cycles: {len(report['deny_log'])}")


if __name__ == "__main__":
    main()
