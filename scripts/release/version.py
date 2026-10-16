#!/usr/bin/env python3
"""The shared agentware release version.

VERSION at the repository root is the one version source. npm, PyPI and the
Go module are released together at that version, from one release tag
(agentware-v<VERSION>). Every file that has to carry the version literally is
listed in TARGETS; `check` fails when one of them disagrees, and `set` rewrites
them all.

    python3 scripts/release/version.py check [--tag agentware-vX.Y.Z]
    python3 scripts/release/version.py set X.Y.Z
    python3 scripts/release/version.py tags [--format github-output]

VERSION is a semantic version: X.Y.Z or X.Y.Z-(alpha|beta|rc).N. Python gets
the PEP 440 spelling of the same version (1.2.3-rc.1 -> 1.2.3rc1).
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
VERSION_FILE = "VERSION"
RELEASE_TAG_PREFIX = "agentware-v"

_SEMVER_RE = re.compile(
    r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-(alpha|beta|rc)\.(0|[1-9]\d*))?"
)
_PEP440_PRE = {"alpha": "a", "beta": "b", "rc": "rc"}


@dataclass(frozen=True)
class Target:
    """A file that carries the version: pattern's one group is the version."""

    path: str
    pattern: str
    count: int = 1
    pep440: bool = False

    def regex(self) -> re.Pattern[str]:
        return re.compile(self.pattern, re.MULTILINE)


TARGETS: tuple[Target, ...] = (
    Target("typescript/package.json", r'^  "version": "([^"]*)"'),
    # The root entry and packages[""] both carry the package version.
    Target("typescript/package-lock.json", r'^(?:  |      )"version": "([^"]*)"(?=,\n(?:  "lockfileVersion"|      "license"))', count=2),
    Target("typescript/src/kei/runtimeLinkRuntime.ts", r'^const SDK_VERSION = "([^"]*)";'),
    Target("python/pyproject.toml", r'^version = "([^"]*)"', pep440=True),
    Target("python/src/haikeilabs/agentware/kei/runtime_link.py", r'^SDK_VERSION = "([^"]*)"', pep440=True),
    Target("go/kei/runtimelink/link.go", r'^var sdkVersion = "([^"]*)"'),
)


def parse(value: str) -> re.Match[str]:
    """Return the match for a valid shared version, else raise ValueError."""
    match = _SEMVER_RE.fullmatch(value)
    if match is None:
        raise ValueError(
            f"invalid version {value!r}: want X.Y.Z or X.Y.Z-(alpha|beta|rc).N"
        )
    return match


def to_pep440(value: str) -> str:
    major, minor, patch, pre, num = parse(value).groups()
    base = f"{major}.{minor}.{patch}"
    return base if pre is None else f"{base}{_PEP440_PRE[pre]}{num}"


def release_tags(value: str) -> dict[str, str]:
    """The tags one release creates: the trigger, the Go module, and PyPI's."""
    parse(value)
    return {
        "release": f"{RELEASE_TAG_PREFIX}{value}",
        "go": f"go/v{value}",
        "python": f"agentware-py-v{to_pep440(value)}",
    }


def read_version(root: Path) -> str:
    value = (root / VERSION_FILE).read_text().strip()
    parse(value)
    return value


def _expected(target: Target, value: str) -> str:
    return to_pep440(value) if target.pep440 else value


def check(root: Path = ROOT, tag: str | None = None) -> list[str]:
    """Return one error per disagreement with VERSION; empty means they agree."""
    try:
        value = read_version(root)
    except (OSError, ValueError) as exc:
        return [f"{VERSION_FILE}: {exc}"]
    errors = []
    if tag is not None and tag != f"{RELEASE_TAG_PREFIX}{value}":
        errors.append(f"release tag {tag} does not match {VERSION_FILE} {value}")
    for target in TARGETS:
        want = _expected(target, value)
        found = target.regex().findall((root / target.path).read_text())
        if len(found) != target.count:
            errors.append(
                f"{target.path}: found {len(found)} version line(s), want {target.count}"
            )
        elif any(got != want for got in found):
            errors.append(f"{target.path}: version {', '.join(found)}, want {want}")
    return errors


def set_version(root: Path, value: str) -> None:
    """Write value to VERSION and every target."""
    parse(value)
    updates = {}
    for target in TARGETS:
        path = root / target.path
        text = path.read_text()
        want = _expected(target, value)
        new, n = target.regex().subn(
            lambda m: m.group(0)[: m.start(1) - m.start(0)] + want + m.group(0)[m.end(1) - m.start(0) :],
            text,
        )
        if n != target.count:
            raise ValueError(f"{target.path}: found {n} version line(s), want {target.count}")
        updates[path] = new
    for path, text in updates.items():
        path.write_text(text)
    (root / VERSION_FILE).write_text(value + "\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    check_cmd = sub.add_parser("check", help="fail when a file disagrees with VERSION")
    check_cmd.add_argument("--tag", help="release tag that must match VERSION")
    set_cmd = sub.add_parser("set", help="write a new version everywhere")
    set_cmd.add_argument("version")
    tags_cmd = sub.add_parser("tags", help="print the tags a release of VERSION creates")
    tags_cmd.add_argument("--format", choices=["text", "github-output"], default="text")
    args = parser.parse_args(argv)

    if args.command == "check":
        errors = check(ROOT, tag=args.tag)
        for error in errors:
            print(error, file=sys.stderr)
        if errors:
            return 1
        print(f"versions agree: {read_version(ROOT)}")
        return 0
    if args.command == "set":
        set_version(ROOT, args.version)
        print(f"set version {args.version}")
        return 0
    value = read_version(ROOT)
    tags = release_tags(value)
    if args.format == "github-output":
        print(f"version={value}")
        print(f"pep440={to_pep440(value)}")
        for name, tag in tags.items():
            print(f"{name}_tag={tag}")
    else:
        for name, tag in tags.items():
            print(f"{name}: {tag}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
