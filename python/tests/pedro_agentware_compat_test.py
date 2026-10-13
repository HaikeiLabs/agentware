"""The deprecated ``pedro_agentware`` alias resolves to the same objects as ``haikeilabs.agentware``.

Each check runs in a fresh interpreter: the alias warns once per process, and
the order of first imports (old name first, new name first) matters.
"""

import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"


def run(code: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-W", "error::DeprecationWarning", "-c", textwrap.dedent(code)],
        capture_output=True,
        text=True,
        env={"PYTHONPATH": str(SRC)},
        cwd=SRC.parent,
        check=False,
    )


def test_importing_the_old_name_warns() -> None:
    result = run("import pedro_agentware")
    assert result.returncode != 0
    assert "DeprecationWarning" in result.stderr
    assert "import 'haikeilabs.agentware' instead" in result.stderr


def test_the_new_name_does_not_warn() -> None:
    result = run("import haikeilabs.agentware, haikeilabs.agentware.kei, haikeilabs.agentware.middleware")
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("new_first", [False, True])
def test_old_imports_resolve_to_the_same_objects(new_first: bool) -> None:
    preamble = "import haikeilabs.agentware.kei.evaluator\n" if new_first else ""
    result = run(
        preamble
        + textwrap.dedent("""
        import sys, warnings
        warnings.simplefilter("ignore", DeprecationWarning)

        import pedro_agentware
        import pedro_agentware.kei.evaluator as old_evaluator
        import pedro_agentware.middleware.types as old_types
        from pedro_agentware.kei.authorize_client import KeiProxyAuthorizeClient
        from pedro_agentware.kei.evaluator import KeiProxyEvaluator, resources_touched
        from pedro_agentware.middleware.types import Action, CallerContext, Decision
        from pedro_agentware.kei import KeiProxySocketAuthorizeClient, sync_tool_manifest
        import pedro_agentware.kei.runtime_socket as old_socket
        import pedro_agentware.kei.tool_sync as old_tool_sync

        import haikeilabs.agentware as new
        import haikeilabs.agentware.kei.authorize_client as new_client
        import haikeilabs.agentware.kei.evaluator as new_evaluator
        import haikeilabs.agentware.middleware.types as new_types
        import haikeilabs.agentware.kei.runtime_socket as new_socket
        import haikeilabs.agentware.kei.tool_sync as new_tool_sync

        assert old_evaluator is new_evaluator
        assert old_types is new_types
        assert KeiProxyAuthorizeClient is new_client.KeiProxyAuthorizeClient
        assert KeiProxyEvaluator is new_evaluator.KeiProxyEvaluator
        assert resources_touched is new_evaluator.resources_touched
        assert (Action, CallerContext, Decision) == (
            new_types.Action, new_types.CallerContext, new_types.Decision
        )
        assert old_socket is new_socket
        assert old_tool_sync is new_tool_sync
        assert KeiProxySocketAuthorizeClient is new_socket.KeiProxySocketAuthorizeClient
        assert sync_tool_manifest is new_tool_sync.sync_tool_manifest
        assert pedro_agentware.kei.evaluator is new_evaluator
        assert pedro_agentware.Middleware is new.Middleware
        assert pedro_agentware.__version__ == new.__version__
        assert sys.modules["pedro_agentware.kei"] is sys.modules["haikeilabs.agentware.kei"]
        # The real modules keep their own identity.
        assert new_evaluator.__name__ == "haikeilabs.agentware.kei.evaluator"
        assert new_evaluator.__spec__.name == "haikeilabs.agentware.kei.evaluator"
        """)
    )
    assert result.returncode == 0, result.stderr


def test_a_monkeypatch_through_the_old_name_reaches_the_new_module() -> None:
    result = run(
        """
        import warnings
        warnings.simplefilter("ignore", DeprecationWarning)
        import pedro_agentware.kei.evaluator as old
        import haikeilabs.agentware.kei.evaluator as new
        old.AFFIRMATIVE_DECISIONS = frozenset({"ok"})
        assert new.AFFIRMATIVE_DECISIONS == frozenset({"ok"})
        """
    )
    assert result.returncode == 0, result.stderr


def test_an_unknown_old_submodule_is_still_an_import_error() -> None:
    result = run(
        """
        import warnings
        warnings.simplefilter("ignore", DeprecationWarning)
        try:
            import pedro_agentware.no_such_module
        except ModuleNotFoundError:
            pass
        else:
            raise SystemExit("imported a module that does not exist")
        """
    )
    assert result.returncode == 0, result.stderr


def test_haikeilabs_is_a_namespace_package() -> None:
    assert not (SRC / "haikeilabs" / "__init__.py").exists()
    result = run(
        """
        import haikeilabs
        assert getattr(haikeilabs, "__file__", None) is None, haikeilabs.__file__
        assert len(list(haikeilabs.__path__)) >= 1
        """
    )
    assert result.returncode == 0, result.stderr
