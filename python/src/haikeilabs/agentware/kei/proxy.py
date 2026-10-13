"""KEI local proxy executable discovery and subprocess management.

This module provides standardized local kei-proxy executable discovery
and subprocess invocation with fail-closed security behavior.

Reconciles local-CLI vs shared-HTTP documentation in favor of the local
wrapper distributed with the harness: agents invoke the local kei-proxy
executable, which owns the network boundary to the KEI API.

Security contract:
- The bootstrap secret is passed to the subprocess ONLY via the
  KEI_RUNTIME_TOKEN environment variable (or an injected secret
  provider). It is never taken from the manifest, never placed on the
  command line, and never logged.
- Discovery fails closed: if no executable is found, no process is
  started and an error is raised.
"""

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, runtime_checkable

from .auth import BOOTSTRAP_TOKEN_ENV, SecretProvider


class ProxyDiscoveryError(Exception):
    """Error during proxy discovery."""

    pass


class ProxyExecutionError(Exception):
    """Error during proxy execution."""

    pass


class ProxyNotFoundError(ProxyDiscoveryError):
    """KEI proxy executable not found."""

    pass


@dataclass
class ProxyConfig:
    """Configuration for local KEI proxy process.

    The bootstrap secret is resolved from token, secret_provider, or the
    KEI_RUNTIME_TOKEN environment variable — never from the manifest.
    It is delivered to the subprocess only via the environment.
    """

    executable: str = "kei-proxy"
    port: int = 8080
    api_url: str = ""
    token: str | None = None
    secret_provider: SecretProvider | None = None
    env: dict[str, str] = field(default_factory=dict)
    timeout: float = 30.0

    def resolve_token(self) -> str:
        """Resolve the bootstrap secret. Fails closed if unavailable."""
        if self.token:
            return self.token
        if self.secret_provider is not None:
            value = self.secret_provider.get_secret(BOOTSTRAP_TOKEN_ENV)
            if value:
                return value
        value = os.environ.get(BOOTSTRAP_TOKEN_ENV)
        if not value:
            raise ProxyExecutionError(
                f"No bootstrap secret available: set {BOOTSTRAP_TOKEN_ENV} "
                f"or provide a SecretProvider"
            )
        return value


@runtime_checkable
class ProxyProcess(Protocol):
    """Protocol for proxy process management."""

    def start(self) -> None:
        """Start the proxy process."""
        ...

    def stop(self) -> None:
        """Stop the proxy process."""
        ...

    def is_running(self) -> bool:
        """Check if proxy is running."""
        ...

    def get_url(self) -> str:
        """Get proxy URL."""
        ...


class LocalProxyProcess:
    """Manages a local kei-proxy subprocess.

    This implementation:
    - Discovers kei-proxy executable in PATH or adjacent to harness
    - Starts proxy as subprocess with configured parameters
    - Handles cleanup on stop
    - Fail-closed: raises on any error
    """

    def __init__(self, config: ProxyConfig):
        """Initialize with configuration.

        Args:
            config: Proxy configuration
        """
        self._config = config
        self._process: subprocess.Popen[str] | None = None
        self._executable_path: Path | None = None

    def _discover_executable(self) -> Path:
        """Discover kei-proxy executable.

        Search order:
        1. Explicit path from config
        2. PATH environment variable
        3. Adjacent to current Python process
        4. Current working directory

        Raises:
            ProxyNotFoundError: If executable not found
        """
        if self._config.executable and os.path.isabs(self._config.executable):
            path = Path(self._config.executable)
            if path.exists() and os.access(path, os.X_OK):
                return path
            raise ProxyNotFoundError(f"Explicit executable not found: {path}")

        exe_name = self._config.executable
        if sys.platform == "win32":
            exe_name += ".exe"

        which_path = shutil.which(exe_name)
        if which_path:
            return Path(which_path)

        current_py = Path(sys.executable).parent
        adjacent = current_py / exe_name
        if adjacent.exists() and os.access(adjacent, os.X_OK):
            return adjacent

        cwd = Path.cwd() / exe_name
        if cwd.exists() and os.access(cwd, os.X_OK):
            return cwd

        raise ProxyNotFoundError(
            f"kei-proxy executable not found. Searched: PATH, "
            f"{current_py}, {Path.cwd()}. Consider installing kei-proxy "
            f"or providing explicit path."
        )

    def start(self) -> None:
        """Start the kei-proxy subprocess.

        Raises:
            ProxyExecutionError: If proxy fails to start
            ProxyNotFoundError: If executable not found
        """
        if self._process and self._process.poll() is None:
            return

        try:
            self._executable_path = self._discover_executable()
        except ProxyNotFoundError:
            raise

        env = os.environ.copy()
        env.update(self._config.env)
        env[BOOTSTRAP_TOKEN_ENV] = self._config.resolve_token()

        args = [
            str(self._executable_path),
            "--port",
            str(self._config.port),
        ]
        if self._config.api_url:
            args.extend(["--api-url", self._config.api_url])

        try:
            self._process = subprocess.Popen(
                args,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
        except FileNotFoundError:
            raise ProxyNotFoundError(
                f"Failed to start kei-proxy: executable not found at {self._executable_path}"
            ) from None
        except PermissionError as e:
            raise ProxyExecutionError(f"Permission denied executing kei-proxy: {e}") from e
        except Exception as e:
            raise ProxyExecutionError(f"Failed to start kei-proxy: {e}") from e

    def stop(self) -> None:
        """Stop the kei-proxy subprocess."""
        if self._process and self._process.poll() is None:
            self._process.terminate()
            try:
                self._process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._process.kill()
                self._process.wait()
        self._process = None

    def is_running(self) -> bool:
        """Check if proxy is running."""
        return self._process is not None and self._process.poll() is None

    def get_url(self) -> str:
        """Get proxy base URL."""
        return f"http://localhost:{self._config.port}"

    def __enter__(self) -> "LocalProxyProcess":
        """Context manager entry."""
        self.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: object,
    ) -> None:
        """Context manager exit."""
        self.stop()


def discover_proxy(executable: str = "kei-proxy") -> Path | None:
    """Discover kei-proxy executable without starting it.

    Args:
        executable: Executable name or path to search for

    Returns:
        Path to executable if found, None otherwise
    """
    config = ProxyConfig(executable=executable)
    try:
        process = LocalProxyProcess(config)
        return process._discover_executable()
    except ProxyNotFoundError:
        return None


def run_proxy(
    api_url: str,
    token: str | None = None,
    secret_provider: SecretProvider | None = None,
    port: int = 8080,
    timeout: float = 30.0,
) -> LocalProxyProcess:
    """Start a local kei-proxy process.

    This is a convenience function that creates and starts a proxy process.

    Args:
        api_url: KEI API URL to proxy
        token: Optional explicit bootstrap token
        secret_provider: Optional secret source for the bootstrap token
        port: Port to listen on
        timeout: Connection timeout

    Returns:
        Running LocalProxyProcess instance

    Raises:
        ProxyNotFoundError: If proxy executable not found
        ProxyExecutionError: If proxy fails to start or no secret available
    """
    config = ProxyConfig(
        api_url=api_url,
        token=token,
        secret_provider=secret_provider,
        port=port,
        timeout=timeout,
    )
    process = LocalProxyProcess(config)
    process.start()
    return process


def stop_proxy(process: LocalProxyProcess) -> None:
    """Stop a running kei-proxy process.

    Args:
        process: Process to stop
    """
    process.stop()
