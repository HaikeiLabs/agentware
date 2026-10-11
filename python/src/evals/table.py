"""Batch table-test execution: suites x repeats on one model, with a job cap."""
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from typing import Any

from evals.benchmark import CaseOutcome, SuiteResult
from evals.runner import EvalCase, EvalResult, EvalRunner
from evals.suite import Suite

EXIT_OK = 0
EXIT_BELOW_THRESHOLD = 1
EXIT_UNAVAILABLE = 2

ToolFn = Callable[[str, dict[str, Any]], str]
ResultHook = Callable[[Suite, EvalCase, int, EvalResult], None]


def combine_repeats(case_id: str, runs: list[EvalResult]) -> CaseOutcome:
    """One case's repeats. Each repeat is a run and counts on its own toward
    the suite's passed runs / total runs (EV-C1, owner decision 2026-10-10).

    The case is ``passed`` only when every run passed, and ``errored`` when no
    run failed on its merits but one errored. The reason lists every run that
    did not pass, failures before errors.
    """
    total = len(runs)
    duration_ms = sum(run.duration_ms for run in runs)

    def prefix(i: int) -> str:
        return f"repeat {i}/{total}: " if total > 1 else ""

    failed = [(i, run) for i, run in enumerate(runs, 1) if not run.success and not run.errored]
    errored = [(i, run) for i, run in enumerate(runs, 1) if run.errored]
    reasons = [prefix(i) + run.error for i, run in failed]
    reasons += [f"{prefix(i)}error: {run.error}" for i, run in errored]
    passed_runs = sum(1 for run in runs if run.success)
    return CaseOutcome(
        id=case_id,
        passed=passed_runs == total,
        reason="; ".join(reasons),
        errored=bool(errored) and not failed,
        duration_ms=duration_ms,
        runs=total,
        passed_runs=passed_runs,
        errored_runs=len(errored),
    )


def run_suite(
    runner: EvalRunner,
    suite: Suite,
    model: str,
    tool_executor: ToolFn,
    jobs: int = 1,
    log: Callable[[str], None] = lambda line: print(line, flush=True),
    on_result: ResultHook | None = None,
) -> SuiteResult:
    """Run every case ``suite.repeats`` times with at most ``jobs`` in flight.

    ``on_result`` sees every individual run (e.g. to write transcripts).
    """
    units: list[tuple[EvalCase, int]] = [
        (case, rep) for case in suite.cases for rep in range(suite.repeats)
    ]

    def run(unit: tuple[EvalCase, int]) -> EvalResult:
        case, rep = unit
        result = runner.run_table_case(
            case, model, tool_executor, session_id=f"{suite.name}:{case.name}:{rep}"
        )
        status = "pass" if result.success else ("error" if result.errored else "fail")
        rep_label = f" (repeat {rep + 1}/{suite.repeats})" if suite.repeats > 1 else ""
        log(f"  ... {case.name}{rep_label}: {status} in {result.duration_ms}ms")
        if on_result is not None:
            on_result(suite, case, rep, result)
        return result

    with ThreadPoolExecutor(max_workers=max(1, jobs)) as pool:
        results = list(pool.map(run, units))

    by_case: dict[str, list[EvalResult]] = {}
    for (case, _), result in zip(units, results):
        by_case.setdefault(case.name, []).append(result)

    outcome = SuiteResult(suite=suite.name, kind=suite.kind)
    for case in suite.cases:
        case_outcome = combine_repeats(case.name, by_case[case.name])
        outcome.cases.append(case_outcome)
        status = "PASS" if case_outcome.passed else ("ERROR" if case_outcome.errored else "FAIL")
        log(f"  [{status}] {suite.name} / {case.name}")
        if not case_outcome.passed:
            log(f"      {case_outcome.reason}")
    return outcome


def select_cases(suites: list[Suite], case_ids: list[str]) -> list[Suite]:
    """Keep only the named cases (for re-running failures); drop empty suites.

    Raises ``ValueError`` for an id no suite contains.
    """
    wanted = set(case_ids)
    known = {c.name for s in suites for c in s.cases}
    unknown = sorted(wanted - known)
    if unknown:
        raise ValueError(f"unknown case id(s): {unknown}")
    selected = []
    for suite in suites:
        cases = [c for c in suite.cases if c.name in wanted]
        if cases:
            selected.append(replace(suite, cases=cases))
    return selected


def exit_code(results: list[SuiteResult], threshold: float) -> int:
    """0 when every suite meets ``threshold``, else 1 (EV-C1 §2)."""
    return EXIT_OK if all(r.pass_rate >= threshold for r in results) else EXIT_BELOW_THRESHOLD
