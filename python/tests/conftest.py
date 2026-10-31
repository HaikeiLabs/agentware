"""Test configuration."""

import os
import sys
from datetime import timedelta
from pathlib import Path

from hypothesis import settings

# Add project root to path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))
print(f"[conftest] Added {project_root} to sys.path")
print(f"[conftest] sys.path[0] = {sys.path[0]}")

# Hypothesis profiles: the default profile keeps normal CI fast (the PR
# workflow runs the whole suite); HYPOTHESIS_PROFILE=nightly drives the
# scheduled parser-fuzz job in .github/workflows/fuzz.yml. print_blob makes
# failures carry the @reproduce_failure blob needed to replay them.
settings.register_profile(
    "default",
    max_examples=200,
    deadline=timedelta(seconds=2),
)
settings.register_profile(
    "nightly",
    max_examples=20000,
    deadline=None,
    print_blob=True,
)
settings.load_profile(os.environ.get("HYPOTHESIS_PROFILE", "default"))
