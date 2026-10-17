"""Contract test: the minimal governed harness example against the fake daemon.

Runs ``examples/python/minimal-harness/main.py --fake`` as a subprocess and
asserts it prints a governed permit for ``echo`` and a governed deny for
``delete_everything`` (with ``reason_code`` and ``hint`` as separate fields),
and that the fake daemon wrote exactly two audit lines.
"""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
MAIN = REPO_ROOT / "examples" / "python" / "minimal-harness" / "main.py"
SRC = REPO_ROOT / "python" / "src"


def test_minimal_harness_fake_round_trip() -> None:
    with tempfile.TemporaryDirectory(prefix="fakekei-demo-", dir=REPO_ROOT) as directory:
        audit_path = str(Path(directory) / "audit.jsonl")
        # The child runs the example as a fresh interpreter; make the src-layout
        # package importable regardless of how the test interpreter resolved it.
        env = dict(os.environ)
        env["PYTHONPATH"] = str(SRC) + os.pathsep + env.get("PYTHONPATH", "")
        proc = subprocess.run(
            [sys.executable, str(MAIN), "--fake", "--audit", audit_path],
            capture_output=True,
            text=True,
            timeout=60,
            env=env,
        )
        assert proc.returncode == 0, proc.stderr
        out = proc.stdout.splitlines()

        allow = [line for line in out if line.startswith("ALLOW tool=echo")]
        assert allow, f"no permit line for echo in {out!r}"
        assert "policy_id=fake-allow-echo" in allow[0]

        deny = [line for line in out if line.startswith("DENY tool=delete_everything")]
        assert deny, f"no deny line for delete_everything in {out!r}"
        assert "reason_code=no_matching_policy" in deny[0]
        assert "hint=Add a policy for this tool: kei policies create" in deny[0]

        lines = [
            json.loads(line)
            for line in Path(audit_path).read_text().splitlines()
            if line.strip()
        ]
        assert [line["tool"] for line in lines] == ["echo", "delete_everything"]
        assert [line["decision"] for line in lines] == ["permit", "deny"]
