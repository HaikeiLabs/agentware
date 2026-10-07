#!/usr/bin/env python3
"""
Main entry point for running evals.

Table-test suites (EV-C1, canonical):
  python -m evals.main --suite <file-or-dir> --model-profile <name>
      [--profiles evals/model-profiles.yaml] --out <dir>
      [--threshold 0.95] [--jobs N]
  Exit 0 = every suite >= threshold; 1 = a suite below threshold;
  2 = endpoint unavailable or the run is misconfigured (never scored).

Legacy Python case modules:
  python -m evals.main [--file-search | --general | --github | --calendar
                       | --beta-tools | --all]
"""
import argparse
import json
import os
import sys
import threading
from pathlib import Path
from typing import Any

from evals.benchmark import build_benchmark, git_sha, scrub, write_benchmark
from evals.cases.beta_tools import BETA_TOOL_CASES
from evals.cases.calendar import CALENDAR_CASES
from evals.cases.file_search import FILE_SEARCH_CASES
from evals.cases.general import GENERAL_CASES
from evals.cases.github import GITHUB_CASES
from evals.executors import (
    beta_tool_executor,
    dispatch_tool_executor,
    mock_tool_executor,
    suite_tool_executor,
)
from evals.models import ModelBackend
from evals.profiles import ProfileError, resolve_profile
from evals.runner import EndpointUnavailableError, EvalCase, EvalResult, EvalRunner
from evals.suite import Suite, SuiteValidationError, load_suites
from evals.table import EXIT_UNAVAILABLE, ResultHook, exit_code, run_suite, select_cases

__all__ = [
    "beta_tool_executor",
    "dispatch_tool_executor",
    "main",
    "mock_tool_executor",
    "suite_tool_executor",
]

# python/src/evals/main.py -> repo root
_REPO_ROOT = Path(__file__).resolve().parents[3]
_PROFILES_NAME = Path("evals") / "model-profiles.yaml"


def default_profiles_path() -> Path:
    """``./evals/model-profiles.yaml`` if present, else agentware's canonical file."""
    local = Path.cwd() / _PROFILES_NAME
    return local if local.is_file() else _REPO_ROOT / _PROFILES_NAME


def _blocked(message: str) -> int:
    print(f"\nBLOCKED: eval run did not execute.\n  {message}", file=sys.stderr)
    return EXIT_UNAVAILABLE


def transcript_writer(path: Path, secrets: tuple[str, ...]) -> ResultHook:
    """A ``run_suite`` hook appending one scrubbed JSON line per run.

    Transcripts are diagnostics for explaining failures; they are not part
    of the benchmark contract and are not meant to be committed.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("")
    lock = threading.Lock()

    def write(suite: Suite, case: EvalCase, rep: int, result: EvalResult) -> None:
        record: dict[str, Any] = {
            "suite": suite.name,
            "case": case.name,
            "repeat": rep,
            "passed": result.success,
            "errored": result.errored,
            "reason": result.error,
            "turns": result.turns,
            "tool_calls": result.tool_calls,
            "final_text": result.final_text,
        }
        line = scrub(json.dumps(record), secrets)
        with lock, path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")

    return write


def run_suites(args: argparse.Namespace) -> int:
    """Run table-test suites on one model profile; return the exit code."""
    if not args.out:
        return _blocked("--out <dir> is required with --suite")
    try:
        suites: list[Suite] = []
        for path in args.suite:
            suites.extend(load_suites(path))
        if args.case:
            suites = select_cases(suites, args.case)
        profile = resolve_profile(args.profiles or default_profiles_path(), args.model_profile)
        base_url = profile.base_url()
    except (SuiteValidationError, ProfileError, ValueError) as exc:
        return _blocked(str(exc))

    # Pin the runner version now: the code scoring this run is what is loaded
    # at start, even if the checkout moves before the benchmark is written.
    sha = os.environ.get("GITHUB_SHA") or git_sha(_REPO_ROOT)
    jobs = max(1, min(args.jobs, profile.concurrency))
    if jobs < args.jobs:
        print(f"--jobs {args.jobs} capped to profile concurrency {profile.concurrency}")

    runner = EvalRunner(
        base_url=base_url,
        max_turns=args.max_turns,
        backend=profile.backend,
        timeout=args.timeout,
        max_tokens=args.max_tokens,
    )
    try:
        runner.preflight(profile.model)
    except EndpointUnavailableError:
        # The exception text carries the private base URL; report the env name.
        return _blocked(
            f"profile {profile.name!r} ({profile.model}) is not reachable at "
            f"${profile.base_url_env} via the {profile.backend.value} backend"
        )

    print(f"=== {profile.name} ({profile.model}), jobs={jobs} ===")
    on_result = transcript_writer(Path(args.transcripts), (base_url,)) if args.transcripts else None
    results = [
        run_suite(
            runner, suite, profile.model, suite_tool_executor, jobs=jobs, on_result=on_result
        )
        for suite in suites
    ]

    benchmark = build_benchmark(
        results,
        model_profile=profile.name,
        model=profile.model,
        sha=sha,
        secrets=(base_url,),
    )
    out_dir = Path(args.out)
    write_benchmark(out_dir, benchmark, args.threshold)

    print("\n=== Summary ===")
    for r in results:
        print(f"{r.suite}: {r.passed}/{r.total} passed ({r.pass_rate:.1%}), "
              f"{r.failed} failed, {r.errors} errors")
    print(f"Benchmark written to: {out_dir}")
    return exit_code(results, args.threshold)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run evals against models")
    suite_group = parser.add_argument_group("table-test suites (EV-C1)")
    suite_group.add_argument(
        "--suite",
        action="append",
        help="agentware.eval-suite.v1 JSON file or directory of them (repeatable)",
    )
    suite_group.add_argument(
        "--model-profile",
        default=os.environ.get("EVAL_MODEL_PROFILE", ""),
        help="Profile name from --profiles (default: $EVAL_MODEL_PROFILE)",
    )
    suite_group.add_argument(
        "--profiles",
        default=None,
        help="model-profiles.yaml (default: ./evals/model-profiles.yaml, else agentware's)",
    )
    suite_group.add_argument("--out", default="", help="Directory for benchmark.{json,md}")
    suite_group.add_argument(
        "--threshold", type=float, default=0.95, help="Minimum pass rate per suite"
    )
    suite_group.add_argument(
        "--jobs", type=int, default=1, help="Concurrent cases (capped by profile concurrency)"
    )
    suite_group.add_argument(
        "--timeout", type=int, default=600, help="Per-request model timeout in seconds"
    )
    suite_group.add_argument(
        "--case",
        action="append",
        help="Run only this case id (repeatable), e.g. to re-run failures",
    )
    suite_group.add_argument(
        "--transcripts",
        default="",
        help="Write per-run JSONL transcripts (tool calls, final text) here; diagnostics only",
    )
    suite_group.add_argument(
        "--max-tokens",
        type=int,
        default=1024,
        help="Completion token budget per turn; a reply cut off here is failed as truncated",
    )
    parser.add_argument("--file-search", action="store_true", help="Run file search evals only")
    parser.add_argument("--general", action="store_true", help="Run general tool calling evals only")
    parser.add_argument("--github", action="store_true", help="Run GitHub tool evals only")
    parser.add_argument("--calendar", action="store_true", help="Run calendar tool evals only")
    parser.add_argument(
        "--beta-tools", action="store_true", help="Run beta tool surface evals only"
    )
    parser.add_argument("--all", action="store_true", default=True, help="Run all evals (default)")
    parser.add_argument(
        "--models",
        default=os.environ.get("EVAL_MODELS", "llama3.2"),
        help="Comma-separated model list (default: $EVAL_MODELS)",
    )
    parser.add_argument(
        "--base-url",
        default=os.environ.get("EVAL_BASE_URL", "http://localhost:8000"),
        help="API base URL (default: $EVAL_BASE_URL)",
    )
    parser.add_argument("--backend", default="llamacpp", choices=["openai", "anthropic", "ollama", "vllm", "lmstudio", "llamacpp"], help="Model backend")
    parser.add_argument("--max-turns", type=int, default=10, help="Max turns per eval")
    args = parser.parse_args(argv)

    if args.suite:
        if not args.model_profile:
            return _blocked("--model-profile is required with --suite")
        return run_suites(args)

    backend = ModelBackend(args.backend)

    if args.file_search:
        cases = FILE_SEARCH_CASES
        output_file = "file_search_results.json"
    elif args.general:
        cases = GENERAL_CASES
        output_file = "general_results.json"
    elif args.github:
        cases = GITHUB_CASES
        output_file = "github_results.json"
    elif args.calendar:
        cases = CALENDAR_CASES
        output_file = "calendar_results.json"
    elif args.beta_tools:
        cases = BETA_TOOL_CASES
        output_file = "beta_tools_results.json"
    else:
        cases = (
            FILE_SEARCH_CASES + GENERAL_CASES + GITHUB_CASES + CALENDAR_CASES + BETA_TOOL_CASES
        )
        output_file = "results.json"

    models = [m.strip() for m in args.models.split(",") if m.strip()]

    runner = EvalRunner(base_url=args.base_url, max_turns=args.max_turns, backend=backend)
    try:
        report = runner.run_evals(cases, models, dispatch_tool_executor)
    except EndpointUnavailableError as exc:
        # A blocked model run is reported as blocked and exits non-zero. It is
        # never written out as a 0% score, which would read as "the model
        # failed every case".
        print(f"\nBLOCKED: model run did not execute.\n  {exc}", file=sys.stderr)
        print(
            "  Set EVAL_BASE_URL (or --base-url) and --backend to a reachable "
            "OpenAI-compatible endpoint, then re-run.",
            file=sys.stderr,
        )
        return EXIT_UNAVAILABLE

    # Anchored to this module, not the CWD, so results land in the same
    # directory whether the runner is invoked from the repo root or python/.
    output_dir = Path(__file__).resolve().parent / "output"
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / output_file
    runner.save_report(report, output_path)

    print("\n=== Summary ===")
    for model in models:
        pass_rate = report.pass_rate(model) * 100
        print(f"{model}: {pass_rate:.1f}% pass rate")

    print(f"\nResults saved to: {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
