#!/usr/bin/env python3
"""Generate the linter's table from kei-connector-contracts v0.5.0 source."""
import json
import re
import sys
from pathlib import Path


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("usage: generate-connector-capabilities.py PATH_TO_CONTRACTS_V0.5.0")
    source = Path(sys.argv[1]) / "contract" / "contract.go"
    text = source.read_text(encoding="utf-8")
    providers = dict(re.findall(r"(Provider\w+)\s+Provider\s*=\s*\"([^\"]+)\"", text))
    block = text.split("var definitions = map[Provider][]Capability{", 1)[1].split("\n}", 1)[0]
    result = {}
    for line in block.splitlines():
        match = re.match(r"\s*(Provider\w+):\s*(.*)", line)
        if match:
            key, entries = match.groups()
            result[providers[key]] = sorted(re.findall(r'Name:\s*"([^\"]+)"', entries))
    expected = set(providers.values())
    if set(result) != expected:
        raise SystemExit(f"could not parse all provider capability declarations: missing {sorted(expected-set(result))}")
    out = Path(__file__).resolve().parents[1] / "fixtures/kei/connector-capabilities.v0.5.0.json"
    out.write_text(json.dumps(dict(sorted(result.items())), indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
