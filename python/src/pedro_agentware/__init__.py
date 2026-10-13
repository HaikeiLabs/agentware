"""Deprecated alias for :mod:`haikeilabs.agentware`.

The SDK's import package is ``haikeilabs.agentware`` (distribution
``haikeilabs-agentware``). This package keeps code written against the old
name working: ``pedro_agentware`` and every ``pedro_agentware.<submodule>``
resolve to the *same module objects* as their ``haikeilabs.agentware``
counterparts, so classes, isinstance checks and monkeypatches are shared.

Importing it emits a :class:`DeprecationWarning`. Switch imports to
``haikeilabs.agentware``; this alias will be removed in a future release.
"""

import importlib
import importlib.abc
import importlib.machinery
import importlib.util
import sys
import warnings
from collections.abc import Sequence
from types import ModuleType
from typing import Any

import haikeilabs.agentware as _target
from haikeilabs.agentware import *  # noqa: F403
from haikeilabs.agentware import __all__ as __all__  # noqa: F401
from haikeilabs.agentware import __version__ as __version__  # noqa: F401

_OLD = __name__
_NEW = _target.__name__

warnings.warn(
    f"'{_OLD}' is deprecated; import '{_NEW}' instead (pip install haikeilabs-agentware)",
    DeprecationWarning,
    stacklevel=2,
)


class _AliasLoader(importlib.abc.Loader):
    """Hands back the already-imported ``haikeilabs.agentware`` module."""

    def __init__(self, target: str) -> None:
        self._target = target
        self._spec: importlib.machinery.ModuleSpec | None = None

    def create_module(self, spec: importlib.machinery.ModuleSpec) -> ModuleType:
        module = importlib.import_module(self._target)
        # module_from_spec overwrites __spec__; exec_module puts it back.
        self._spec = module.__spec__
        return module

    def exec_module(self, module: ModuleType) -> None:
        module.__spec__ = self._spec


class _AliasFinder(importlib.abc.MetaPathFinder):
    """Resolves ``pedro_agentware.<x>`` to ``haikeilabs.agentware.<x>``."""

    def find_spec(
        self,
        fullname: str,
        path: Sequence[str] | None,
        target: ModuleType | None = None,
    ) -> importlib.machinery.ModuleSpec | None:
        if not fullname.startswith(_OLD + "."):
            return None
        new_name = _NEW + fullname[len(_OLD) :]
        if importlib.util.find_spec(new_name) is None:
            return None
        return importlib.machinery.ModuleSpec(fullname, _AliasLoader(new_name))


if not any(isinstance(finder, _AliasFinder) for finder in sys.meta_path):
    sys.meta_path.insert(0, _AliasFinder())

# Submodules already imported under the new name are aliased right away.
for _name, _module in list(sys.modules.items()):
    if _name.startswith(_NEW + "."):
        sys.modules.setdefault(_OLD + _name[len(_NEW) :], _module)


def __getattr__(name: str) -> Any:
    """``pedro_agentware.<x>`` reads ``haikeilabs.agentware.<x>`` (e.g. a loaded submodule)."""
    try:
        return getattr(_target, name)
    except AttributeError:
        raise AttributeError(f"module {_OLD!r} has no attribute {name!r}") from None
