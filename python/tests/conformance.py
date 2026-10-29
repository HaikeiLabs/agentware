"""Record which shared fixture cases a Python test ran.

The equal-coverage gate (scripts/conformance/gate.py, see docs/testing.md)
compares these records across languages. Recording is a no-op unless
AGENTWARE_CONFORMANCE_LOG is set. Call record() after a case's assertions,
so only passing cases are written.
"""

import json
import os

LOG_ENV = "AGENTWARE_CONFORMANCE_LOG"


def record(fixture: str, section: str, case: str) -> None:
    """Note that a case of fixture (a path relative to fixtures/) passed."""
    path = os.environ.get(LOG_ENV)
    if not path:
        return
    line = json.dumps({"language": "python", "fixture": fixture, "section": section, "case": case})
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(line + "\n")
