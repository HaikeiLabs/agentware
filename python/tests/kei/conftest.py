"""Test configuration for KEI contract tests.

Ensures the package source (python/src) and this test directory are on
sys.path so imports resolve regardless of the working directory.

This worktree shares site-packages with the original checkout via an
editable install of `haikeilabs.agentware` that points at the original
checkout (which lacks the `kei` submodule). To guarantee the tests import
the worktree's package, we purge any already-imported `haikeilabs.agentware`
that does not expose `kei` and re-import from this worktree's src.
"""

import importlib
import sys
from pathlib import Path

_here = Path(__file__).resolve().parent
_src = _here.parent.parent / "src"

for p in (str(_src), str(_here)):
    if p not in sys.path:
        sys.path.insert(0, p)

# Force the worktree's haikeilabs.agentware (which contains `kei`) to be the
# imported package, even if an editable install resolved it elsewhere.
_existing = sys.modules.get("haikeilabs.agentware")
if _existing is not None:
    try:
        importlib.import_module("haikeilabs.agentware.kei")
    except ImportError:
        # Loaded from a location without `kei` (e.g. original checkout).
        for name in [
            n for n in sys.modules if n == "haikeilabs.agentware" or n.startswith("haikeilabs.agentware.")
        ]:
            del sys.modules[name]
