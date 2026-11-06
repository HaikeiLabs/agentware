#!/usr/bin/env python3
"""Check the README compatibility table against the pinned kei-proxy.

README.md holds, between <!-- compat:begin --> and <!-- compat:end -->, a
table of agentware SDK version -> minimum kei-proxy -> minimum kei-cli.
KEI_PROXY_VERSION at the repository root pins the kei-proxy release this SDK
version is supported against (the same file name and format kei-cli uses for
the kei-proxy it bundles). The check fails unless the table has a row for the
current VERSION whose kei-proxy entry equals the pin.

    python3 scripts/release/compat.py check

The same command checks the user-facing Markdown: a doc that names an
Agentware version older than VERSION must say "from X" or "X or later", so a
stale minimum reads as a feature floor rather than the current release.
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

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


# User-facing Markdown checked for stale Agentware versions. CHANGELOGs and
# docs/specs/ are history and contracts; they keep the versions they name.
DOC_GLOBS = (
    "README.md",
    "docs/**/*.md",
    "examples/**/README.md",
    "python/README.md",
    "typescript/README.md",
    "go/README.md",
    "fixtures/kei/README.md",
)
DOC_EXCLUDES = ("docs/specs/",)

_DOC_VERSION_RE = re.compile(r"\bagentware(?:\s+sdk)?\s+v?(\d+)\.(\d+)\.(\d+)\b", re.IGNORECASE)
_FROM_RE = re.compile(r"\bfrom\s+$", re.IGNORECASE)
_OR_LATER_RE = re.compile(r"^\s+or\s+later\b", re.IGNORECASE)


def _doc_paths(root: Path) -> list[Path]:
    paths: set[Path] = set()
    for pattern in DOC_GLOBS:
        for path in root.glob(pattern):
            rel = PurePosixPath(path.relative_to(root).as_posix())
            if path.is_file() and not str(rel).startswith(DOC_EXCLUDES) and "node_modules" not in rel.parts:
                paths.add(path)
    return sorted(paths)


def stale_doc_versions(text: str, current: str) -> list[tuple[int, str]]:
    """Return (line, version) for each unqualified Agentware version older than current."""
    major, minor, patch = (int(part) for part in version.parse(current).groups()[:3])
    found = []
    for match in _DOC_VERSION_RE.finditer(text):
        named = tuple(int(part) for part in match.groups())
        if named >= (major, minor, patch):
            continue
        if _FROM_RE.search(text[: match.start()]) or _OR_LATER_RE.match(text[match.end() :]):
            continue
        line = text.count("\n", 0, match.start()) + 1
        found.append((line, ".".join(match.groups())))
    return found


def check_docs(root: Path = ROOT) -> list[str]:
    """Return one error per doc that names an older Agentware version without "from" or "or later"."""
    try:
        current = version.read_version(root)
    except (OSError, ValueError) as exc:
        return [str(exc)]
    errors = []
    for path in _doc_paths(root):
        rel = path.relative_to(root).as_posix()
        for line, named in stale_doc_versions(path.read_text(encoding="utf-8"), current):
            errors.append(
                f"{rel}:{line}: names Agentware {named}, older than VERSION {current}; "
                f'write "from {named}" or "{named} or later", or update it'
            )
    return errors


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
    errors = check(ROOT) + check_docs(ROOT)
    for error in errors:
        print(error, file=sys.stderr)
    if errors:
        return 1
    print(f"compatibility table matches {PIN_FILE} {(ROOT / PIN_FILE).read_text().strip()}")
    print("docs name no Agentware version older than VERSION without a from/or-later floor")
    return 0


if __name__ == "__main__":
    sys.exit(main())
