#!/usr/bin/env python3
"""Check the README compatibility table against the pinned kei-proxy.

README.md holds, between <!-- compat:begin --> and <!-- compat:end -->, a
table of agentware SDK version -> minimum kei-proxy -> minimum kei-cli.
KEI_PROXY_VERSION at the repository root pins the kei-proxy release this SDK
version is supported against (the same file name and format kei-cli uses for
the kei-proxy it bundles). The check fails unless the table has a row for the
current VERSION whose kei-proxy entry equals the pin.

    python3 scripts/release/compat.py check
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from pathlib import Path

import version

ROOT = version.ROOT
README = "README.md"
PIN_FILE = "KEI_PROXY_VERSION"
BEGIN = "<!-- compat:begin -->"
END = "<!-- compat:end -->"

_PROXY_RE = re.compile(r"v?(\d+\.\d+\.\d+)")
_CLI_RE = re.compile(r"\d+\.\d+\.\d+")


@dataclass(frozen=True)
class Row:
    sdk: str
    kei_proxy: str
    kei_cli: str


def _cells(line: str) -> list[str]:
    return [cell.strip().strip("`") for cell in line.strip().strip("|").split("|")]


def parse_table(text: str) -> list[Row]:
    """Return the table rows, or raise ValueError when it is missing or malformed."""
    start, end = text.find(BEGIN), text.find(END)
    if start < 0 or end < start:
        raise ValueError(f"{README}: compatibility table markers {BEGIN} ... {END} not found")
    lines = [line for line in text[start + len(BEGIN) : end].splitlines() if line.strip()]
    rows = []
    # lines[0] is the header and lines[1] the separator.
    for line in lines[2:]:
        cells = _cells(line)
        if len(cells) != 3:
            raise ValueError(f"{README}: compatibility row needs 3 columns: {line.strip()}")
        sdk, proxy, cli = cells
        version.parse(sdk)
        if not _PROXY_RE.fullmatch(proxy):
            raise ValueError(f"{README}: kei-proxy entry {proxy!r} is not a version")
        if not _CLI_RE.fullmatch(cli):
            raise ValueError(f"{README}: kei-cli entry {cli!r} is not a version")
        rows.append(Row(sdk=sdk, kei_proxy=proxy, kei_cli=cli))
    if not rows:
        raise ValueError(f"{README}: compatibility table has no rows")
    return rows


def _normalize(proxy: str) -> str:
    match = _PROXY_RE.fullmatch(proxy)
    return match.group(1) if match else proxy


def check(root: Path = ROOT) -> list[str]:
    """Return one error per problem; empty means the table matches the pin."""
    try:
        pin = (root / PIN_FILE).read_text().strip()
    except OSError as exc:
        return [f"{PIN_FILE}: {exc}"]
    if not _PROXY_RE.fullmatch(pin):
        return [f"{PIN_FILE}: {pin!r} is not a kei-proxy version (want vX.Y.Z)"]
    try:
        current = version.read_version(root)
        rows = parse_table((root / README).read_text())
    except (OSError, ValueError) as exc:
        return [str(exc)]
    row = next((r for r in rows if r.sdk == current), None)
    if row is None:
        return [f"{README}: compatibility table has no row for agentware {current} (VERSION)"]
    if _normalize(row.kei_proxy) != _normalize(pin):
        return [
            f"{README}: agentware {current} lists kei-proxy {row.kei_proxy}, "
            f"but {PIN_FILE} pins {pin}"
        ]
    return []


def main() -> int:
    if sys.argv[1:] != ["check"]:
        print(__doc__, file=sys.stderr)
        return 2
    errors = check(ROOT)
    for error in errors:
        print(error, file=sys.stderr)
    if errors:
        return 1
    print(f"compatibility table matches {PIN_FILE} {(ROOT / PIN_FILE).read_text().strip()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
