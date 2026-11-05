#!/usr/bin/env python3
"""Fail when user-facing docs say "governed" or "governance" in prose.

Agentware docs say guardrails, not governance. Code identifiers keep their
names, so a word inside inline backticks or a fenced code block is allowed:
`governed_call` and the `governed-call-audit` job pass, "a governed call"
fails.

    python3 scripts/docs/guardrails_lint.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[2]

# The Task 4 language-pass scope. CHANGELOGs, docs/specs/ and anything else
# outside these globs keep their wording.
DOC_GLOBS = (
    "README.md",
    "docs/**/*.md",
    "examples/**/README.md",
    "python/README.md",
    "typescript/README.md",
    "go/README.md",
    "fixtures/kei/README.md",
)
EXCLUDES = ("docs/specs/",)

_WORD_RE = re.compile(r"\b(governed|governance)\b", re.IGNORECASE)
_INLINE_CODE_RE = re.compile(r"(`+)(?:(?!\1).)+?\1")
_FENCE_RE = re.compile(r"^\s*(```|~~~)")


def doc_paths(root: Path) -> list[Path]:
    paths: set[Path] = set()
    for pattern in DOC_GLOBS:
        for path in root.glob(pattern):
            rel = PurePosixPath(path.relative_to(root).as_posix())
            if path.is_file() and not str(rel).startswith(EXCLUDES) and "node_modules" not in rel.parts:
                paths.add(path)
    return sorted(paths)


def prose_hits(text: str) -> list[tuple[int, str]]:
    """Return (line, word) for each governed/governance outside code."""
    hits = []
    fence: str | None = None
    for number, line in enumerate(text.splitlines(), start=1):
        match = _FENCE_RE.match(line)
        if match:
            marker = match.group(1)
            if fence is None:
                fence = marker
            elif fence == marker:
                fence = None
            continue
        if fence is not None:
            continue
        prose = _INLINE_CODE_RE.sub("", line)
        hits.extend((number, word.group(0)) for word in _WORD_RE.finditer(prose))
    return hits


def check(root: Path = ROOT) -> list[str]:
    errors = []
    for path in doc_paths(root):
        rel = path.relative_to(root).as_posix()
        for line, word in prose_hits(path.read_text(encoding="utf-8")):
            errors.append(
                f'{rel}:{line}: "{word}" in prose; write guardrails, a protected tool call, '
                "or a tool call that Kei checks, or put a code identifier in backticks"
            )
    return errors


def main() -> int:
    errors = check(ROOT)
    for error in errors:
        print(error, file=sys.stderr)
    if errors:
        return 1
    print('docs say guardrails: no "governed" or "governance" in prose')
    return 0


if __name__ == "__main__":
    sys.exit(main())
