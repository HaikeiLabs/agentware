"""KEI harness configuration and manifest loading.

This module provides the canonical configuration layer for KEI harness
integration. It loads and validates versioned kei-harness.json manifests
while enforcing security boundaries:

- The bootstrap secret (KEI_RUNTIME_TOKEN) is NEVER stored in the manifest
  and is never resolved from it. The wrapper always reads it separately
  from the KEI_RUNTIME_TOKEN environment variable or a secret provider.
- The manifest may contain non-secret connector secret reference
  identifiers only. The wrapper never resolves them and they must not be
  able to resolve or reveal credentials to the agent.
- workspace_id is informational only; authority is server-derived.
- Self-reported tool bindings/capabilities are routing metadata only and
  never grant permissions.

Schema version: 1.0.0
"""

import json
from pathlib import Path
from typing import Any

import pydantic
from pydantic import BaseModel, Field

# Canonical name of the bootstrap secret. Manifests that reference it in
# any form are rejected (fail closed).
BOOTSTRAP_SECRET_NAME = "KEI_RUNTIME_TOKEN"

# Legacy bootstrap secret name (HAI-119 / ADR-020). The old name is also
# rejected in manifests; only KEI_RUNTIME_TOKEN is accepted.
LEGACY_BOOTSTRAP_SECRET_NAME = "KEI_HARNESS_TOKEN"

# Tool bindings are self-reported routing metadata. They never grant
# permissions; enforcement remains with the middleware policy layer.
BINDINGS_GRANT_PERMISSIONS = False


class HarnessType(BaseModel):
    """Harness type specification."""

    slug: str = Field(min_length=1, description="Machine-readable harness type identifier")
    version: str = Field(min_length=1, description="Harness version string")


class SecretRef(BaseModel):
    """Non-secret identifier for a connector secret.

    This is an opaque reference identifier only. The wrapper NEVER resolves
    it and it must not be able to resolve or reveal credentials to the
    agent. Resolution, if any, happens out-of-band on the connector side.
    """

    source: str = Field(description="Reference namespace, e.g. 'secret_provider'")
    key: str = Field(min_length=1, description="Opaque reference identifier (not a credential)")


class ToolBinding(BaseModel):
    """Binding between a tool and a KEI connector.

    Self-reported routing metadata only. Never grants permissions.
    """

    tool_name: str = Field(min_length=1, description="Name of the tool")
    connector_id: str = Field(min_length=1, description="KEI connector identifier")
    config: dict[str, Any] = Field(
        default_factory=dict, description="Connector-specific routing config"
    )


class HarnessManifest(BaseModel):
    """KEI harness manifest configuration.

    Versioned, non-secret configuration describing the harness setup.
    The bootstrap secret is deliberately absent and must not be added.
    """

    schema_version: str = Field(min_length=1, description="Manifest schema version")
    kei_api_url: str = Field(min_length=1, description="Base URL for KEI API")
    harness_id: str = Field(min_length=1, description="Unique harness identifier")
    workspace_id: str | None = Field(
        default=None,
        description="Informational only; authority is server-derived",
    )
    harness_type: HarnessType = Field(description="Harness type specification")
    environment: str = Field(
        min_length=1,
        description="Environment name (e.g., 'dev', 'staging', 'production')",
    )
    installation_id: str | None = Field(default=None, description="Installation identifier")
    config_revision: str | None = Field(default=None, description="Configuration revision hash")
    tool_bindings: list[ToolBinding] = Field(
        default_factory=list,
        description="Self-reported tool-to-connector routing; never grants permissions",
    )
    secret_refs: dict[str, SecretRef] = Field(
        default_factory=dict,
        description="Non-secret connector reference identifiers; never resolved by wrapper",
    )

    model_config = {"extra": "forbid"}


def validate_manifest(manifest: HarnessManifest) -> list[str]:
    """Validate a harness manifest. Fails closed on security violations.

    Args:
        manifest: The manifest to validate

    Returns:
        List of validation errors (empty if valid)
    """
    errors: list[str] = []

    # Fail closed: the bootstrap secret must never appear in the manifest,
    # neither as a value-bearing entry nor as a resolvable reference.
    # Both the current name (KEI_RUNTIME_TOKEN) and the legacy name
    # (KEI_HARNESS_TOKEN) are rejected.
    forbidden_names = {BOOTSTRAP_SECRET_NAME, LEGACY_BOOTSTRAP_SECRET_NAME}
    for name in manifest.secret_refs:
        if name.upper() in forbidden_names:
            errors.append(
                f"Manifest must not contain a secret_ref for the bootstrap "
                f"secret '{name}'; it is loaded separately "
                f"from the environment/secret provider"
            )
    for ref in manifest.secret_refs.values():
        if ref.key.upper() in forbidden_names:
            errors.append(
                f"Secret reference key must not reference the bootstrap secret '{ref.key}'"
            )

    if manifest.schema_version != "1.0.0":
        errors.append(
            f"Unsupported schema version: {manifest.schema_version}. Supported: ['1.0.0']"
        )

    return errors


def load_manifest(path: str | Path) -> HarnessManifest:
    """Load and parse a kei-harness.json manifest file.

    Args:
        path: Path to the kei-harness.json file

    Returns:
        Parsed HarnessManifest

    Raises:
        FileNotFoundError: If manifest file doesn't exist
        ValueError: If manifest is invalid or fails validation
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"Manifest not found: {path}")

    try:
        with open(path) as f:
            data = json.load(f)
    except json.JSONDecodeError as e:
        raise ValueError(f"Invalid JSON in manifest: {e}") from e

    try:
        manifest = HarnessManifest(**data)
    except pydantic.ValidationError as e:
        raise ValueError(f"Invalid manifest schema: {e}") from e

    errors = validate_manifest(manifest)
    if errors:
        raise ValueError(f"Manifest validation failed: {'; '.join(errors)}")

    return manifest


class HarnessConfig(BaseModel):
    """Loaded harness configuration.

    Wraps the manifest. Contains no secret values: the bootstrap secret is
    resolved separately by the auth layer from KEI_RUNTIME_TOKEN or a
    secret provider, and connector secret_refs are never resolved here.
    """

    manifest: HarnessManifest

    @property
    def kei_api_url(self) -> str:
        """KEI API base URL."""
        return self.manifest.kei_api_url

    @property
    def harness_id(self) -> str:
        """Harness identifier."""
        return self.manifest.harness_id

    @property
    def config_revision(self) -> str | None:
        """Configuration revision hash."""
        return self.manifest.config_revision


def get_config(path: str | Path) -> HarnessConfig:
    """Load a harness config from a kei-harness.json manifest.

    Resolves no secrets: the bootstrap token is read separately by the
    auth layer from the environment/secret provider.

    Args:
        path: Path to kei-harness.json

    Returns:
        HarnessConfig wrapping the validated manifest
    """
    return HarnessConfig(manifest=load_manifest(path))
