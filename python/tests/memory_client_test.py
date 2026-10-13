"""Tests for the WikiMemory MCP client against the real Go core."""

import shutil
import subprocess
from pathlib import Path

import pytest

from haikeilabs.agentware.memory import (
    LangGraphMemoryTools,
    WikiMemory,
    parse_diagnostics,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
TBOX_PATHS = [
    str(REPO_ROOT / "ontologies/education/TBOX_LEARNING_SOFTWARE.ttl"),
    str(REPO_ROOT / "ontologies/social/twitch_topics.ttl"),
]

VALID_PAGE = """---
id: go-worker-pools
type: sw:Skill
labels: ["Worker Pools"]
topics: [twitch:Go]
claims:
  - {id: c1, text: "Bounded pools prevent goroutine leaks", sources: [src-talk]}
---
Pools bound concurrency.
"""


@pytest.fixture(scope="session")
def memctl(tmp_path_factory: pytest.TempPathFactory) -> str:
    if shutil.which("go") is None:
        pytest.skip("go toolchain unavailable")
    if not Path(TBOX_PATHS[0]).exists():
        pytest.skip("ontologies submodule missing (git submodule update --init)")
    binary = tmp_path_factory.mktemp("bin") / "memctl"
    subprocess.run(
        ["go", "build", "-o", str(binary), "./cmd/memctl"],
        cwd=REPO_ROOT / "go",
        check=True,
        capture_output=True,
    )
    return str(binary)


@pytest.fixture()
def vault_root(tmp_path: Path) -> str:
    return str(tmp_path / "memory")


def make_memory(memctl: str, root: str, user: str) -> WikiMemory:
    return WikiMemory(
        user_id=user, root=root, tbox_paths=TBOX_PATHS, memctl_path=memctl
    )


def test_tools_listed(memctl: str, vault_root: str) -> None:
    with make_memory(memctl, vault_root, "alice") as memory:
        names = {t["name"] for t in memory.tools()}
    assert names == {
        "memory_ingest",
        "memory_write_page",
        "memory_query",
        "memory_get_claims",
        "memory_lint",
    }


def test_write_query_claims_roundtrip(memctl: str, vault_root: str) -> None:
    with make_memory(memctl, vault_root, "alice") as memory:
        _, ok, err = memory.write_page(VALID_PAGE)
        assert ok, err
        hits, ok, err = memory.query("worker pools")
        assert ok and any(h["id"] == "go-worker-pools" for h in hits)
        claims, ok, err = memory.get_claims("go-worker-pools")
        assert ok and claims[0]["text"].startswith("Bounded pools")
        assert claims[0]["confidence"] is None


def test_deny_carries_parseable_diagnostics(memctl: str, vault_root: str) -> None:
    with make_memory(memctl, vault_root, "alice") as memory:
        _, ok, err = memory.write_page("---\nid: bad-page\ntype: sw:Skil\n---\n")
    assert not ok
    violations = parse_diagnostics(err)
    assert violations and violations[0].constraint == "unknown-class"
    assert violations[0].nearest and violations[0].nearest[0].endswith("Skill")


def test_user_isolation_across_clients(memctl: str, vault_root: str) -> None:
    with make_memory(memctl, vault_root, "alice") as alice:
        _, ok, err = alice.write_page(VALID_PAGE)
        assert ok, err
    with make_memory(memctl, vault_root, "bob") as bob:
        hits, ok, _ = bob.query("worker pools")
        assert ok and hits is not None and not hits
        # In-band scope override is denied by the server's policy tier.
        _, ok, err = bob.execute("memory_query", {"question": "x", "user_id": "alice"})
        assert not ok and "denied by policy" in err


def test_langgraph_tools_render_denials(memctl: str, vault_root: str) -> None:
    with make_memory(memctl, vault_root, "alice") as memory:
        tools = LangGraphMemoryTools(memory).tools()
        by_name = {t.__name__: t for t in tools}
        assert set(by_name) == {
            "memory_ingest",
            "memory_write_page",
            "memory_query",
            "memory_get_claims",
            "memory_lint",
        }
        out = by_name["memory_write_page"](content="---\nid: p\ntype: nope\n---\n")
        assert out.startswith("DENIED:")
        assert by_name["memory_ingest"](source_id="src-a", text="notes") == "ingested src-a"
