#!/usr/bin/env python3
"""Equal-coverage gate for the shared fixture conformance suites.

Every language's fixture tests append one JSON line per case they ran and
passed to the file named by AGENTWARE_CONFORMANCE_LOG:

    {"language": "go", "fixture": "toolformat/glm-cases.json",
     "section": "parse_calls", "case": "single call"}

This script lists every case in the shared fixtures, then checks that each
language ran each case, unless fixtures/EXCLUSIONS.md lists it for that
language. See docs/testing.md.

    gate.py report --language go --log go.jsonl   # one leg: counts only
    gate.py check --logs DIR                      # all legs: DIR/<language>.jsonl

Only the standard library is used, so it runs on any runner with python3.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import sys
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
FIXTURES = REPO / "fixtures"
EXCLUSIONS = FIXTURES / "EXCLUSIONS.md"
LANGUAGES = ("go", "python", "typescript", "rust")

# Fixture families in the conformance suite: a glob under fixtures/ and the
# top-level arrays whose named entries are cases.
FAMILIES = (
    ("toolformat/*-cases.json", ("format_definitions", "parse_calls", "format_result", "validate_format")),
    ("reasoning/*-cases.json", ("examples",)),
    ("toolspec/manifest-v3/index.json", ("valid", "invalid")),
    ("toolspec/cases.v1.json", ("cases",)),
)


def case_id(fixture: str, section: str, name: str) -> str:
    return f"{fixture}#{section}/{name}"


def fixture_of(cid: str) -> str:
    return cid.split("#", 1)[0]


def load_cases() -> dict[str, list[str]]:
    """Return {fixture path relative to fixtures/: [case ids]}."""
    cases: dict[str, list[str]] = {}
    for pattern, sections in FAMILIES:
        for path in sorted(FIXTURES.glob(pattern)):
            fixture = path.relative_to(FIXTURES).as_posix()
            data = json.loads(path.read_text(encoding="utf-8"))
            ids: list[str] = []
            for section in sections:
                for entry in data.get(section) or []:
                    name = entry.get("name") if isinstance(entry, dict) else None
                    if not name:
                        sys.exit(f"{fixture}: an entry in {section} has no name")
                    cid = case_id(fixture, section, name)
                    if cid in ids:
                        sys.exit(f"{fixture}: duplicate case name {section}/{name}")
                    ids.append(cid)
            cases[fixture] = ids
    return cases


@dataclass(frozen=True)
class Exclusion:
    language: str
    pattern: str
    line: int

    def matches(self, cid: str) -> bool:
        return fnmatch.fnmatchcase(cid, self.pattern)


def load_exclusions(path: Path = EXCLUSIONS) -> list[Exclusion]:
    """Parse the rows of the exclusions table: | language | `pattern` | reason | removed by |."""
    rows: list[Exclusion] = []
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if not line.lstrip().startswith("|") or len(cells) < 4:
            continue
        language, pattern = cells[0].lower(), cells[1].strip("`")
        if language in ("language", "") or set(language) <= set("-: "):
            continue
        if language not in LANGUAGES:
            sys.exit(f"{path.name}:{lineno}: unknown language {cells[0]!r}")
        if "#" not in pattern:
            sys.exit(f"{path.name}:{lineno}: case pattern {pattern!r} must be <fixture>#<section>/<case>")
        rows.append(Exclusion(language, pattern, lineno))
    return rows


def load_log(path: Path, language: str) -> set[str]:
    ran: set[str] = set()
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        rec = json.loads(line)
        if rec.get("language") != language:
            sys.exit(f"{path}:{lineno}: record is for {rec.get('language')!r}, expected {language!r}")
        ran.add(case_id(rec["fixture"], rec["section"], rec["case"]))
    return ran


def summarize(language: str, ran: set[str], cases: dict[str, list[str]]) -> list[str]:
    known = {c for ids in cases.values() for c in ids}
    lines = [f"### {language}: ran {len(ran & known)} of {len(known)} fixture cases", ""]
    lines += ["| Fixture | Ran | Cases |", "|---|---:|---:|"]
    for fixture, ids in cases.items():
        lines.append(f"| `{fixture}` | {len(ran & set(ids))} | {len(ids)} |")
    return lines


def check(logs: dict[str, set[str]], cases: dict[str, list[str]], exclusions: list[Exclusion]) -> tuple[list[str], list[str]]:
    """Return (markdown report lines, failure messages)."""
    known = {c for ids in cases.values() for c in ids}
    failures: list[str] = []
    excluded: dict[str, set[str]] = defaultdict(set)

    for language, ran in logs.items():
        for cid in sorted(ran - known):
            failures.append(f"{language}: recorded a case that is not in the fixtures: {cid}")
        rows = [e for e in exclusions if e.language == language]
        for cid in sorted(known - ran):
            if any(e.matches(cid) for e in rows):
                excluded[language].add(cid)
            else:
                failures.append(f"{language}: did not run {cid} and it is not in fixtures/EXCLUSIONS.md")
        for e in rows:
            if not any(e.matches(cid) for cid in known - ran):
                failures.append(
                    f"{language}: EXCLUSIONS.md line {e.line} ({e.pattern}) excludes nothing "
                    "this language skips; remove the row"
                )

    header = "| Fixture | Cases | " + " | ".join(logs) + " |"
    lines = ["### Fixture conformance (ran / excluded per language)", "", header,
             "|---|---:|" + "---:|" * len(logs)]
    for fixture, ids in cases.items():
        cells = []
        for language, ran in logs.items():
            r = len(ran & set(ids))
            x = len(excluded[language] & set(ids))
            cells.append(f"{r}" + (f" (+{x} excl.)" if x else ""))
        lines.append(f"| `{fixture}` | {len(ids)} | " + " | ".join(cells) + " |")
    return lines, failures


def write_summary(lines: list[str]) -> None:
    text = "\n".join(lines) + "\n"
    print(text)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as fh:
            fh.write(text + "\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    rep = sub.add_parser("report", help="print one language's case counts")
    rep.add_argument("--language", required=True, choices=LANGUAGES)
    rep.add_argument("--log", required=True, type=Path)
    chk = sub.add_parser("check", help="compare every language against the fixtures and exclusions")
    chk.add_argument("--logs", required=True, type=Path, help="directory holding <language>.jsonl")
    args = parser.parse_args(argv)

    cases = load_cases()
    if args.command == "report":
        ran = load_log(args.log, args.language) if args.log.exists() else set()
        write_summary(summarize(args.language, ran, cases))
        return 0

    logs: dict[str, set[str]] = {}
    missing = []
    for language in LANGUAGES:
        path = args.logs / f"{language}.jsonl"
        if path.exists():
            logs[language] = load_log(path, language)
        else:
            missing.append(language)
            logs[language] = set()
    lines, failures = check(logs, cases, load_exclusions())
    failures = [f"{language}: no conformance log ({language}.jsonl); did its leg run?" for language in missing] + failures
    if failures:
        lines += ["", "#### Failures", ""] + [f"- {f}" for f in failures]
    write_summary(lines)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
