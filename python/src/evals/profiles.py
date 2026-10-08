"""Model profiles for eval runs (EV-C1 §3).

A profile names an OpenAI-compatible endpoint by the *environment variable*
that holds its base URL, never the URL itself, so profile files can live in
public repos without leaking private hostnames or addresses.
"""
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from evals.models import ModelBackend


class ProfileError(RuntimeError):
    """A profile file or profile is missing, malformed, or unresolvable."""


@dataclass(frozen=True)
class ModelProfile:
    name: str
    backend: ModelBackend
    model: str
    base_url_env: str
    concurrency: int = 1
    opencode_model: str = ""

    def base_url(self, environ: Mapping[str, str] | None = None) -> str:
        """Resolve the base URL from ``base_url_env``.

        Raises ``ProfileError`` when the variable is unset or empty -- a run
        without an endpoint is blocked, not scored.
        """
        env = os.environ if environ is None else environ
        value = env.get(self.base_url_env, "").strip()
        if not value:
            raise ProfileError(
                f"profile {self.name!r}: environment variable {self.base_url_env} is not set"
            )
        return value.rstrip("/")


def _profile(name: str, raw: Any) -> ModelProfile:
    if not isinstance(raw, dict):
        raise ProfileError(f"profile {name!r}: must be a mapping")
    for key in ("backend", "model", "base_url_env"):
        if not isinstance(raw.get(key), str) or not raw[key]:
            raise ProfileError(f"profile {name!r}: {key} must be a non-empty string")
    try:
        backend = ModelBackend(raw["backend"])
    except ValueError as exc:
        choices = [b.value for b in ModelBackend]
        raise ProfileError(f"profile {name!r}: backend must be one of {choices}") from exc
    concurrency = raw.get("concurrency", 1)
    if not isinstance(concurrency, int) or isinstance(concurrency, bool) or concurrency < 1:
        raise ProfileError(f"profile {name!r}: concurrency must be a positive integer")
    opencode_model = raw.get("opencode_model", "")
    if not isinstance(opencode_model, str):
        raise ProfileError(f"profile {name!r}: opencode_model must be a string")
    return ModelProfile(
        name=name,
        backend=backend,
        model=raw["model"],
        base_url_env=raw["base_url_env"],
        concurrency=concurrency,
        opencode_model=opencode_model,
    )


def load_profiles(path: str | Path) -> dict[str, ModelProfile]:
    """Load every profile from a ``model-profiles.yaml`` file.

    Keys other than the ones the runner reads are ignored, so a profile file
    shared with other runners (e.g. the skills runner) can carry their fields.
    """
    profile_path = Path(path)
    try:
        data = yaml.safe_load(profile_path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ProfileError(f"{profile_path}: cannot read profiles: {exc}") from exc
    except yaml.YAMLError as exc:
        raise ProfileError(f"{profile_path}: not valid YAML: {exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("profiles"), dict):
        raise ProfileError(f"{profile_path}: expected a top-level 'profiles' mapping")
    return {str(name): _profile(str(name), raw) for name, raw in data["profiles"].items()}


def resolve_profile(path: str | Path, name: str) -> ModelProfile:
    profiles = load_profiles(path)
    if name not in profiles:
        raise ProfileError(f"unknown model profile {name!r}; known: {sorted(profiles)}")
    return profiles[name]
