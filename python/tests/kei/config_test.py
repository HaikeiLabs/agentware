"""Tests for KEI config module."""

import json
import os
import tempfile
from pathlib import Path

import pytest

from pedro_agentware.kei.config import (
    BINDINGS_GRANT_PERMISSIONS,
    BOOTSTRAP_SECRET_NAME,
    LEGACY_BOOTSTRAP_SECRET_NAME,
    HarnessConfig,
    HarnessManifest,
    get_config,
    load_manifest,
    validate_manifest,
)

FIXTURES = Path(__file__).parent.parent / "fixtures" / "kei"
VALID_MANIFEST = FIXTURES / "harness-v1.json"
BOOTSTRAP_TOKEN_MANIFEST = FIXTURES / "harness-invalid-bootstrap-token.json"
UNSUPPORTED_VERSION_MANIFEST = FIXTURES / "harness-unsupported-version.json"


class TestHarnessManifest:
    """Tests for HarnessManifest model."""

    def test_valid_manifest(self):
        """Test loading valid manifest."""
        manifest = load_manifest(VALID_MANIFEST)
        assert manifest.schema_version == "1.0.0"
        assert manifest.kei_api_url == "https://kei.example.com/api/v1"
        assert manifest.harness_id == "test-harness-001"
        assert manifest.workspace_id == "ws-test-workspace"
        assert manifest.harness_type.slug == "agent-tool-proxy"
        assert len(manifest.tool_bindings) == 2

    def test_manifest_missing_file(self):
        """Test loading non-existent manifest."""
        with pytest.raises(FileNotFoundError):
            load_manifest("/nonexistent/manifest.json")

    def test_manifest_invalid_json(self):
        """Test loading invalid JSON."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            f.write("{ invalid json }")
            f.flush()
            with pytest.raises(ValueError, match="Invalid JSON"):
                load_manifest(f.name)
            os.unlink(f.name)

    def test_manifest_unknown_field_rejected(self):
        """Extra fields are forbidden (fail closed on schema drift)."""
        data = json.loads(VALID_MANIFEST.read_text())
        data["unexpected_field"] = "x"
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(data, f)
            f.flush()
            with pytest.raises(ValueError, match="Invalid manifest schema"):
                load_manifest(f.name)
            os.unlink(f.name)

    def test_optional_fields(self):
        """Test manifest with optional fields as None."""
        data = {
            "schema_version": "1.0.0",
            "kei_api_url": "https://example.com",
            "harness_id": "test",
            "harness_type": {"slug": "test", "version": "1.0"},
            "environment": "dev",
        }
        manifest = HarnessManifest(**data)
        assert manifest.workspace_id is None
        assert manifest.installation_id is None
        assert manifest.config_revision is None
        assert manifest.tool_bindings == []
        assert manifest.secret_refs == {}


class TestSecurityContract:
    """Tests enforcing the bootstrap-secret security contract."""

    def test_bootstrap_token_secret_ref_rejected(self):
        """Manifest containing a bootstrap token secret_ref must be rejected."""
        with pytest.raises(ValueError, match="bootstrap"):
            load_manifest(BOOTSTRAP_TOKEN_MANIFEST)

    def test_bootstrap_token_ref_key_rejected(self):
        """A secret_ref whose key is the bootstrap token name must be rejected."""
        data = json.loads(VALID_MANIFEST.read_text())
        data["secret_refs"]["connector_bad"] = {
            "source": "env",
            "key": "kei_runtime_token",
        }
        manifest = HarnessManifest(**data)
        errors = validate_manifest(manifest)
        assert any("bootstrap" in e for e in errors)

    def test_manifest_contains_no_secret_values(self):
        """The manifest file itself must not contain the bootstrap secret."""
        raw = VALID_MANIFEST.read_text()
        assert "KEI_RUNTIME_TOKEN" not in raw
        assert "KEI_HARNESS_TOKEN" not in raw
        assert "secret_value" not in raw

    def test_legacy_bootstrap_token_name_rejected(self):
        """A secret_ref whose name is the legacy bootstrap token must be rejected."""
        data = json.loads(VALID_MANIFEST.read_text())
        data["secret_refs"][LEGACY_BOOTSTRAP_SECRET_NAME] = {
            "source": "env",
            "key": "some_key",
        }
        manifest = HarnessManifest(**data)
        errors = validate_manifest(manifest)
        assert any("bootstrap" in e for e in errors)

    def test_legacy_bootstrap_token_key_rejected(self):
        """A secret_ref whose key is the legacy bootstrap token must be rejected."""
        data = json.loads(VALID_MANIFEST.read_text())
        data["secret_refs"]["connector_legacy"] = {
            "source": "env",
            "key": "kei_harness_token",
        }
        manifest = HarnessManifest(**data)
        errors = validate_manifest(manifest)
        assert any("bootstrap" in e for e in errors)

    def test_config_resolves_no_secrets(self):
        """HarnessConfig must not resolve or carry any secret values."""
        os.environ["KEI_RUNTIME_TOKEN"] = "must-not-appear"
        try:
            config = get_config(VALID_MANIFEST)
            dumped = json.dumps(config.model_dump())
            assert "must-not-appear" not in dumped
        finally:
            del os.environ["KEI_RUNTIME_TOKEN"]

    def test_tool_bindings_never_grant_permissions(self):
        """Self-reported tool bindings never grant permissions."""
        assert BINDINGS_GRANT_PERMISSIONS is False
        manifest = load_manifest(VALID_MANIFEST)
        for binding in manifest.tool_bindings:
            dumped = json.dumps(binding.model_dump())
            assert BOOTSTRAP_SECRET_NAME not in dumped
            assert LEGACY_BOOTSTRAP_SECRET_NAME not in dumped
            assert "permission" not in dumped.lower()


class TestValidateManifest:
    """Tests for manifest validation."""

    def test_valid_manifest_no_errors(self):
        """Test valid manifest has no errors."""
        manifest = load_manifest(VALID_MANIFEST)
        errors = validate_manifest(manifest)
        assert errors == []

    def test_unsupported_version_rejected(self):
        """Unsupported schema version fails closed."""
        with pytest.raises(ValueError, match="schema version"):
            load_manifest(UNSUPPORTED_VERSION_MANIFEST)


class TestGetConfig:
    """Tests for get_config convenience function."""

    def test_get_config(self):
        """Test get_config loads and validates manifest."""
        config = get_config(VALID_MANIFEST)
        assert isinstance(config, HarnessConfig)
        assert config.manifest.harness_id == "test-harness-001"
        assert config.kei_api_url == "https://kei.example.com/api/v1"
        assert config.harness_id == "test-harness-001"
        assert config.config_revision == "abc123def"
