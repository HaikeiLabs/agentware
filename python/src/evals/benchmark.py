"""``haikei.eval-benchmark.v1`` output: benchmark.json + benchmark.md (EV-C1 §5)."""
import json
import re
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

BENCHMARK_SCHEMA = "haikei.eval-benchmark.v1"
HARNESS = "agentware"

_IPV4 = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}(?::\d+)?\b")
_TAILNET_HOST = re.compile(r"\b[\w-]+(?:\.[\w-]+)*\.ts\.net\b", re.IGNORECASE)
_URL = re.compile(r"\bhttps?://[^\s'\"<>]+", re.IGNORECASE)
REDACTED = "<redacted>"


def scrub(text: str, secrets: tuple[str, ...] = ()) -> str:
    """Remove endpoint URLs, IPv4 addresses, and tailnet hostnames.

    Benchmarks are committed to public repos; a transport error message must
    not carry the private endpoint it failed to reach.
    """
    for secret in secrets:
        if secret:
            text = text.replace(secret, REDACTED)
    text = _URL.sub(REDACTED, text)
    text = _TAILNET_HOST.sub(REDACTED, text)
    return _IPV4.sub(REDACTED, text)


@dataclass(frozen=True)
class CaseOutcome:
    id: str
    passed: bool
    """True only when every run of the case passed."""
    reason: str = ""
    errored: bool = False
    duration_ms: int = 0
    """Wall time across all repeats of the case."""
    runs: int = 1
    passed_runs: int = -1
    """Runs that passed; defaults to all runs when ``passed``, else none."""
    errored_runs: int = -1
    """Runs that errored; defaults to all runs when ``errored``, else none."""

    def __post_init__(self) -> None:
        if self.passed_runs < 0:
            object.__setattr__(self, "passed_runs", self.runs if self.passed else 0)
        if self.errored_runs < 0:
            object.__setattr__(self, "errored_runs", self.runs if self.errored else 0)


@dataclass
class SuiteResult:
    """One suite on one model. Counts are runs (each repeat of a case is a run),
    so ``pass_rate`` is passed runs / total runs: the average over repeats,
    as for tool-call evals. With one repeat a run is a case."""

    suite: str
    kind: str
    cases: list[CaseOutcome] = field(default_factory=list)

    @property
    def passed(self) -> int:
        return sum(c.passed_runs for c in self.cases)

    @property
    def errors(self) -> int:
        return sum(c.errored_runs for c in self.cases)

    @property
    def failed(self) -> int:
        return self.total - self.passed - self.errors

    @property
    def total(self) -> int:
        return sum(c.runs for c in self.cases)

    @property
    def pass_rate(self) -> float:
        return self.passed / self.total if self.total else 0.0


def _git(cwd: Path | None, *args: str) -> str:
    out = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, timeout=10, check=True
    )
    return out.stdout.strip()


def git_sha(cwd: Path | None = None) -> str:
    """HEAD of the runner checkout, suffixed ``-dirty`` when tracked files differ.

    Call it when a run starts: the code that scores a run is the code that
    was loaded then, not whatever HEAD is when the benchmark is written.
    Untracked files (e.g. fresh results) do not make the tree dirty.
    """
    try:
        sha = _git(cwd, "rev-parse", "HEAD")
        dirty = _git(cwd, "status", "--porcelain", "--untracked-files=no")
    except (OSError, subprocess.SubprocessError):
        return ""
    return f"{sha}-dirty" if dirty else sha


def build_benchmark(
    results: list[SuiteResult],
    model_profile: str,
    model: str,
    sha: str = "",
    created_at: str | None = None,
    secrets: tuple[str, ...] = (),
) -> dict[str, Any]:
    """The benchmark document in the v1 shape. Reasons are scrubbed.

    ``cases[].duration_ms`` is an additive v1 field (EV-C1 contract change,
    2026-10-08); consumers ignore fields they do not know. So are
    ``cases[].runs`` and ``cases[].passed_runs`` (EV-C1 contract change,
    2026-10-10): suite counts are runs, and ``pass_rate`` is passed runs /
    total runs.
    """
    return {
        "schema": BENCHMARK_SCHEMA,
        "harness": HARNESS,
        "model_profile": model_profile,
        "model": model,
        "created_at": created_at or datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "git_sha": sha,
        "results": [
            {
                "suite": r.suite,
                "kind": r.kind,
                "passed": r.passed,
                "failed": r.failed,
                "errors": r.errors,
                "total": r.total,
                "pass_rate": round(r.pass_rate, 4),
                "cases": [
                    {
                        "id": c.id,
                        "passed": c.passed,
                        "reason": scrub(c.reason, secrets),
                        "duration_ms": c.duration_ms,
                        "runs": c.runs,
                        "passed_runs": c.passed_runs,
                    }
                    for c in r.cases
                ],
            }
            for r in results
        ],
    }


def _cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


def render_markdown(benchmark: dict[str, Any], threshold: float) -> str:
    lines = [
        f"# Eval benchmark: {benchmark['model_profile']}",
        "",
        f"- Model: `{benchmark['model']}`",
        f"- Harness: {benchmark['harness']}",
        f"- Created: {benchmark['created_at']}",
        f"- Git SHA: `{benchmark['git_sha'] or 'unknown'}`",
        f"- Threshold: {threshold:.0%}",
        "- Counts are runs (cases x repeats); the pass rate is passed runs / total runs.",
        "",
        "| Suite | Kind | Passed | Failed | Errors | Total | Pass rate | Mean case time | Status |",
        "|---|---|---:|---:|---:|---:|---:|---:|---|",
    ]
    for r in benchmark["results"]:
        status = "PASS" if r["pass_rate"] >= threshold else "BELOW THRESHOLD"
        times = [c.get("duration_ms", 0) for c in r["cases"]]
        mean_s = sum(times) / len(times) / 1000 if times else 0.0
        lines.append(
            f"| {r['suite']} | {r['kind']} | {r['passed']} | {r['failed']} | {r['errors']} "
            f"| {r['total']} | {r['pass_rate']:.1%} | {mean_s:.1f}s | {status} |"
        )
    for r in benchmark["results"]:
        failing = [c for c in r["cases"] if not c["passed"]]
        if not failing:
            continue
        lines += ["", f"## Failing cases: {r['suite']}", "", "| Case | Reason |", "|---|---|"]
        lines += [f"| {c['id']} | {_cell(c['reason'])} |" for c in failing]
    return "\n".join(lines) + "\n"


def write_benchmark(out_dir: Path, benchmark: dict[str, Any], threshold: float) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "benchmark.json").write_text(json.dumps(benchmark, indent=2) + "\n")
    (out_dir / "benchmark.md").write_text(render_markdown(benchmark, threshold))
