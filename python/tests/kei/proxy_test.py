"""Tests for KEI proxy module."""

import os
import stat
import tempfile
from pathlib import Path

import pytest

from pedro_agentware.kei.auth import BOOTSTRAP_TOKEN_ENV
from pedro_agentware.kei.proxy import (
    LocalProxyProcess,
    ProxyConfig,
    ProxyExecutionError,
    ProxyNotFoundError,
    discover_proxy,
)


class FakeSecretProvider:
    """In-memory secret provider for tests."""

    def __init__(self, secrets: dict[str, str]) -> None:
        self._secrets = secrets

    def get_secret(self, name: str) -> str | None:
        return self._secrets.get(name)


def _make_executable(path: Path, body: str = "#!/bin/sh\nexit 0\n") -> Path:
    """Write a fake executable script."""
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return path


class TestProxyDiscovery:
    """Tests for kei-proxy executable discovery (fail closed)."""

    def test_discover_not_found_fails_closed(self):
        """No executable anywhere: discovery raises, nothing starts."""
        config = ProxyConfig(executable="definitely-not-a-real-kei-proxy-xyz")
        process = LocalProxyProcess(config)
        with pytest.raises(ProxyNotFoundError):
            process._discover_executable()
        assert process._process is None

    def test_discover_explicit_path(self):
        """Explicit absolute path is honored."""
        with tempfile.TemporaryDirectory() as tmp:
            exe = _make_executable(Path(tmp) / "kei-proxy")
            config = ProxyConfig(executable=str(exe))
            process = LocalProxyProcess(config)
            assert process._discover_executable() == exe

    def test_discover_explicit_path_missing(self):
        """Explicit path that doesn't exist: fail closed."""
        config = ProxyConfig(executable="/nonexistent/kei-proxy")
        process = LocalProxyProcess(config)
        with pytest.raises(ProxyNotFoundError):
            process._discover_executable()

    def test_discover_from_path_env(self, tmp_path: Path):
        """Executable found via PATH."""
        fake_bin = tmp_path / "bin"
        fake_bin.mkdir()
        exe = _make_executable(fake_bin / "kei-proxy-test")
        os.environ["PATH"] = f"{fake_bin}{os.pathsep}{os.environ.get('PATH', '')}"
        try:
            config = ProxyConfig(executable="kei-proxy-test")
            process = LocalProxyProcess(config)
            assert process._discover_executable() == exe
        finally:
            os.environ.pop("PATH")

    def test_discover_proxy_helper(self):
        """discover_proxy returns None when not found."""
        assert discover_proxy("definitely-not-a-real-kei-proxy-xyz") is None


class TestProxyTokenResolution:
    """Tests for bootstrap secret resolution (fail closed)."""

    def test_token_from_config(self):
        config = ProxyConfig(token="explicit")
        assert config.resolve_token() == "explicit"

    def test_token_from_secret_provider(self):
        os.environ.pop(BOOTSTRAP_TOKEN_ENV, None)
        config = ProxyConfig(secret_provider=FakeSecretProvider({BOOTSTRAP_TOKEN_ENV: "vault"}))
        assert config.resolve_token() == "vault"

    def test_token_from_env(self):
        os.environ[BOOTSTRAP_TOKEN_ENV] = "env-token"
        try:
            config = ProxyConfig()
            assert config.resolve_token() == "env-token"
        finally:
            del os.environ[BOOTSTRAP_TOKEN_ENV]

    def test_missing_token_fails_closed(self):
        """No token from any source: fail closed."""
        os.environ.pop(BOOTSTRAP_TOKEN_ENV, None)
        config = ProxyConfig()
        with pytest.raises(ProxyExecutionError, match="bootstrap secret"):
            config.resolve_token()


class TestProxyProcess:
    """Tests for subprocess lifecycle."""

    def test_start_stop_lifecycle(self, tmp_path: Path):
        """Start a real (fake) proxy subprocess and stop it cleanly."""
        exe = _make_executable(
            tmp_path / "kei-proxy",
            body="#!/bin/sh\nsleep 30\n",
        )
        config = ProxyConfig(executable=str(exe), token="tok", api_url="https://kei.example.com")
        process = LocalProxyProcess(config)
        try:
            process.start()
            assert process.is_running()
            assert process.get_url() == f"http://localhost:{config.port}"
        finally:
            process.stop()
        assert not process.is_running()

    def test_start_without_token_fails_closed(self, tmp_path: Path):
        """Starting without any bootstrap secret fails closed."""
        os.environ.pop(BOOTSTRAP_TOKEN_ENV, None)
        exe = _make_executable(tmp_path / "kei-proxy")
        config = ProxyConfig(executable=str(exe))
        process = LocalProxyProcess(config)
        with pytest.raises(ProxyExecutionError, match="bootstrap secret"):
            process.start()
        assert process._process is None

    def test_context_manager(self, tmp_path: Path):
        """Context manager starts and stops the proxy."""
        exe = _make_executable(tmp_path / "kei-proxy", body="#!/bin/sh\nsleep 30\n")
        config = ProxyConfig(executable=str(exe), token="tok")
        with LocalProxyProcess(config) as process:
            assert process.is_running()
        assert not process.is_running()

    def test_token_not_on_command_line(self, tmp_path: Path):
        """Bootstrap secret is passed via env, never argv."""
        exe = _make_executable(
            tmp_path / "kei-proxy",
            body='#!/bin/sh\necho "$KEI_RUNTIME_TOKEN" > /dev/null\nsleep 30\n',
        )
        config = ProxyConfig(executable=str(exe), token="secret-value")
        process = LocalProxyProcess(config)
        try:
            process.start()
            # Verify the secret is not in the process args
            assert process._process is not None
            raw_args = process._process.args
            if isinstance(raw_args, (str, bytes)):
                args = [os.fsdecode(raw_args)]
            else:
                assert isinstance(raw_args, (list, tuple))
                args = [os.fsdecode(a) for a in raw_args]
            assert "secret-value" not in " ".join(args)
        finally:
            process.stop()
