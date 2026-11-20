"""Tests for the Python OperationRunner need the connector contracts package.

The package (``kei.connection``, ``kei.connectors.github.v1``) is still
published only as a local build; see the OperationRunner PR body. Tests skip
with a clear reason when it is not installed instead of erroring the suite.
"""

import pytest

pytest.importorskip(
    "kei.connectors.github.v1.ops",
    reason="haikeilabs-connector-contracts is local-only until registry publishing",
)
