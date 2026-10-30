#!/usr/bin/env python3
"""Assert that governed calls reached kei-proxy's audit trail.

Used by real-proxy-integration.yml (governed-call-audit job). Each --expect
TOOL=DECISION must match a decision record in every JSONL file given:
the same tool_name and decision and a non-empty invoking_subject (the catalog
rejects a record without one, HAI-928). Files are polled until every
expectation matches or --wait seconds pass, since kei-proxy's collector
flushes on an interval.

Usage:
    python3 fixtures/kei/assert-audit-records.py \\
        --expect echo=allow --expect delete_everything=deny \\
        --wait 180 SPOOL.jsonl FLUSHED.jsonl

Python 3 stdlib only.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any


def read_records(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    records = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict):
            records.append(record)
    return records


def find(records: list[dict[str, Any]], tool: str, decision: str) -> dict[str, Any] | None:
    """The first record for tool and decision that names who invoked it."""
    for record in records:
        if (
            record.get("tool_name") == tool
            and record.get("decision") == decision
            and str(record.get("invoking_subject") or "").strip()
        ):
            return record
    return None


def missing(paths: list[Path], expect: list[tuple[str, str]]) -> list[str]:
    gaps = []
    for path in paths:
        records = read_records(path)
        for tool, decision in expect:
            if find(records, tool, decision) is None:
                gaps.append(f"{path.name}: no {decision} record for {tool} with an invoking_subject")
    return gaps


def parse_expect(value: str) -> tuple[str, str]:
    tool, sep, decision = value.partition("=")
    if not sep or not tool or decision not in ("allow", "deny"):
        raise argparse.ArgumentTypeError(f"expected TOOL=allow|deny, got {value!r}")
    return tool, decision


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("files", nargs="+", type=Path, help="audit JSONL files (spool, flushed records)")
    parser.add_argument("--expect", action="append", type=parse_expect, required=True, metavar="TOOL=DECISION")
    parser.add_argument("--wait", type=float, default=0.0, help="seconds to poll before failing")
    args = parser.parse_args(argv)

    deadline = time.monotonic() + args.wait
    while True:
        gaps = missing(args.files, args.expect)
        if not gaps or time.monotonic() >= deadline:
            break
        time.sleep(2)

    for path in args.files:
        records = read_records(path)
        for tool, decision in args.expect:
            record = find(records, tool, decision)
            if record is not None:
                print(
                    f"{path.name}: {decision} {tool} invoking_subject={record['invoking_subject']}"
                    f" policy_id={record.get('policy_id', '')} span_id={record.get('span_id', '')}"
                )
    if gaps:
        for gap in gaps:
            print(f"::error::{gap}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
