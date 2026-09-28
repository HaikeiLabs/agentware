"""An :class:`AuthorizationClient` that runs ``kei-proxy authorize``.

This is the subprocess half of :class:`KeiProxyEvaluator`. The TypeScript and
Go ports implement the same client, and all three are held to the parity table
in ``fixtures/kei/authorize-cases.v1.json``.

The kei-proxy authorize contract (kei-connector-runtime ``authorize.go``):

- stdout is one JSON object: ``decision`` plus optional ``reason``,
  ``service``, ``credential`` and ``enrollment``;
- exit 0 for ``allow`` / ``enrollment_required``, exit 1 for ``deny``, and
  exit 1 with nothing on stdout (a diagnostic on stderr) for every error.

Security contract:

- ``KEI_RUNTIME_TOKEN`` reaches the child only through its environment,
  never argv. The child environment is built from
  :data:`AUTHORIZE_CHILD_ENV_ALLOWLIST` plus explicit ``extra_env``;
  everything else in the parent environment, including PATH and HOME, is
  dropped. The executable is resolved to an absolute path in the parent.
- With ``expected_sha256`` the binary is re-proved before every spawn: the
  path must equal its realpath, it is opened with ``O_NOFOLLOW``, must be a
  regular file, and its SHA-256 is read from that descriptor and compared in
  constant time. Any drift denies (``pin_mismatch``) without spawning. What
  remains is the hash-then-exec race; close it by making the binary's
  directory and its ancestors writable only by the deploy owner.
- The child runs in its own session (process group); a timeout SIGKILLs the
  whole group.
- stdout can carry a credential or an enrollment claim link and stderr is
  free-form, so stderr is discarded and neither is ever logged or copied into
  an error message.
"""

import hashlib
import hmac
import json
import os
import re
import shutil
import signal
import stat
import subprocess
from collections.abc import Mapping
from typing import Any

from .auth import BOOTSTRAP_TOKEN_ENV
from .evaluator import AFFIRMATIVE_DECISIONS, KeiProxyAuthorizeError

__all__ = [
    "AUTHORIZE_CHILD_ENV_ALLOWLIST",
    "DEFAULT_AUTHORIZE_TIMEOUT",
    "KeiProxyAuthorizeClient",
    "parse_authorize_output",
]

# Parent variables the kei-proxy child may inherit. Mirrors
# fixtures/kei/authorize-cases.v1.json "child_env.allowlist". PATH and HOME
# are deliberately absent, as are KEI_PROXY_* identity variables: identity
# travels as flags so a stale parent value can never be attributed to this call.
AUTHORIZE_CHILD_ENV_ALLOWLIST = (
    "TZ",
    BOOTSTRAP_TOKEN_ENV,
    "KEI_RUNTIME_CONTROL_PLANE_URL",
    "KEI_RUNTIME_VERSION",
    "KEI_API_URL",
    "KEI_PROXY_REGISTRY",
    "KEI_PROXY_AUDIT",
    "KEI_PROXY_AUDIT_MAX_BYTES",
    "KEI_PROXY_PROVIDER_USER",
    "KEI_CREDENTIAL_STORE_INSTALLATION_ID",
    "KEI_AWS_SECRETS_MANAGER_ENDPOINT",
)

DEFAULT_AUTHORIZE_TIMEOUT = 10.0

_SHA256_HEX = re.compile(r"[0-9a-fA-F]{64}")
_HASH_CHUNK = 64 * 1024
_MAX_EXECUTABLE_BYTES = 256 * 1024 * 1024


class KeiProxyAuthorizeClient:
    """Run ``kei-proxy authorize`` once per call and return its JSON object.

    Raises :class:`KeiProxyAuthorizeError` on every path that does not yield
    a readable answer; :class:`KeiProxyEvaluator` turns that into a DENY.
    """

    def __init__(
        self,
        executable: str = "kei-proxy",
        timeout: float = DEFAULT_AUTHORIZE_TIMEOUT,
        env: Mapping[str, str] | None = None,
        extra_env: Mapping[str, str] | None = None,
        expected_sha256: str | None = None,
    ) -> None:
        """Build a client.

        Args:
            executable: Path or name of the kei-proxy binary. A bare name is
                resolved once against the parent PATH; the child is always
                spawned by absolute path.
            timeout: Seconds to wait for one authorize call.
            env: Parent environment to draw allowlisted variables from.
                Defaults to ``os.environ``.
            extra_env: Variables passed to the child verbatim, for settings
                outside the allowlist (e.g. a secret-store profile).
            expected_sha256: Pin the binary to this SHA-256 (64 hex digits).
                ``executable`` must then be an absolute, symlink-free path.

        Raises:
            ValueError: The pin is malformed, or set with a relative path.
        """
        self._expected_digest: bytes | None = None
        if expected_sha256 is not None:
            if not _SHA256_HEX.fullmatch(expected_sha256):
                raise ValueError("expected_sha256 must be 64 hex digits")
            if not os.path.isabs(executable):
                raise ValueError("a pinned executable must be an absolute path")
            self._expected_digest = bytes.fromhex(expected_sha256)
        self._timeout = timeout
        self._env = env
        self._extra_env = dict(extra_env or {})
        self._executable = _resolve_executable(
            executable, (os.environ if env is None else env).get("PATH")
        )

    def child_env(self) -> dict[str, str]:
        """The exact environment the kei-proxy child receives."""
        source = os.environ if self._env is None else self._env
        env = {k: source[k] for k in AUTHORIZE_CHILD_ENV_ALLOWLIST if k in source}
        env.update(self._extra_env)
        return env

    def authorize(
        self,
        user_id: str,
        tool: str,
        action: str,
        resource: str,
        *,
        span_id: str = "",
        invoking_subject: str = "",
        parent_span: str = "",
        delegation_depth: int = 0,
        agent_id: str = "",
        agent_version: str = "",
        framework: str = "",
        tool_args_digest: str = "",
        resources: list[str] | None = None,
        workspace_id: str = "",
    ) -> dict[str, Any]:
        """Authorize one call.

        ``agent_id`` and ``workspace_id`` are accepted and not sent: kei-proxy
        (>= 0.1.11) resolves the agent from the runtime installation, and
        derives workspace scope from the runtime token.
        """
        env = self.child_env()
        if not env.get(BOOTSTRAP_TOKEN_ENV):
            raise KeiProxyAuthorizeError("missing_token", f"{BOOTSTRAP_TOKEN_ENV} is not set")
        if self._executable is None:
            raise KeiProxyAuthorizeError("proxy_unavailable", "kei-proxy was not found on PATH")
        if self._expected_digest is not None:
            _prove_executable(self._executable, self._expected_digest)

        argv = [
            self._executable,
            "authorize",
            "--user",
            user_id,
            "--tool",
            tool,
            "--action",
            action,
            "--resource",
            resource,
        ]
        optional = [
            ("--span-id", span_id),
            ("--invoking-subject", invoking_subject),
            ("--parent-span", parent_span),
            ("--delegation-depth", str(delegation_depth) if delegation_depth > 0 else ""),
            ("--agent-version", agent_version),
            ("--framework", framework),
            ("--tool-args-digest", tool_args_digest),
            ("--resources", ",".join(resources or [])),
        ]
        for flag, value in optional:
            if value:
                argv += [flag, value]

        try:
            proc = subprocess.Popen(
                argv,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except OSError as exc:
            raise KeiProxyAuthorizeError(
                "proxy_unavailable", f"could not start kei-proxy ({type(exc).__name__})"
            ) from None
        try:
            stdout, _ = proc.communicate(timeout=self._timeout)
        except subprocess.TimeoutExpired:
            _kill_group(proc)
            raise KeiProxyAuthorizeError(
                "proxy_timeout", f"no answer within {self._timeout:g}s"
            ) from None
        except BaseException:
            _kill_group(proc)
            raise
        if proc.returncode < 0:
            raise KeiProxyAuthorizeError(
                "proxy_error", f"kei-proxy terminated by signal {-proc.returncode}"
            )
        return parse_authorize_output(stdout, proc.returncode)


def _kill_group(proc: subprocess.Popen[bytes]) -> None:
    """SIGKILL the child's whole process group, then reap the child."""
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except OSError:
        proc.kill()
    if proc.stdout is not None:
        proc.stdout.close()
    proc.wait()


def _resolve_executable(executable: str, path: str | None) -> str | None:
    """An absolute path for ``executable``; bare names search ``path``.

    Relative ``path`` entries are skipped so the working directory never
    decides which binary runs.
    """
    if os.path.isabs(executable):
        return executable
    if os.sep in executable or (os.altsep and os.altsep in executable):
        return os.path.abspath(executable)
    search = os.pathsep.join(d for d in (path or "").split(os.pathsep) if os.path.isabs(d))
    return shutil.which(executable, path=search) if search else None


def _pin_mismatch(detail: str) -> KeiProxyAuthorizeError:
    return KeiProxyAuthorizeError("pin_mismatch", detail)


def _prove_executable(path: str, expected: bytes) -> None:
    """Re-prove the pinned executable immediately before a spawn."""
    try:
        real = os.path.realpath(path, strict=True)
    except OSError:
        raise _pin_mismatch("executable is missing") from None
    if real != path:
        raise _pin_mismatch("executable path is not canonical")
    no_follow = getattr(os, "O_NOFOLLOW", None)
    if no_follow is None:
        raise _pin_mismatch("O_NOFOLLOW is unavailable")
    try:
        fd = os.open(path, os.O_RDONLY | no_follow)
    except OSError:
        raise _pin_mismatch("executable could not be opened without following links") from None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise _pin_mismatch("executable is not a regular file")
        digest = hashlib.sha256()
        total = 0
        while chunk := os.read(fd, _HASH_CHUNK):
            total += len(chunk)
            if total > _MAX_EXECUTABLE_BYTES:
                raise _pin_mismatch("executable exceeds the size limit")
            digest.update(chunk)
    finally:
        os.close(fd)
    if not hmac.compare_digest(digest.digest(), expected):
        raise _pin_mismatch("executable digest does not match the pin")


def parse_authorize_output(stdout: bytes, exit_code: int) -> dict[str, Any]:
    """Validate one authorize result against the exit-code contract."""
    if exit_code not in (0, 1):
        raise KeiProxyAuthorizeError("proxy_error", f"kei-proxy exited {exit_code}")
    text = stdout.decode("utf-8", errors="replace").strip()
    if not text:
        if exit_code != 0:
            raise KeiProxyAuthorizeError("proxy_error", f"kei-proxy exited {exit_code}")
        raise KeiProxyAuthorizeError("empty_response", "kei-proxy printed nothing")
    try:
        result = json.loads(text)
    except ValueError:
        raise KeiProxyAuthorizeError("malformed_response", "stdout is not JSON") from None
    if not isinstance(result, dict):
        raise KeiProxyAuthorizeError("malformed_response", "stdout is not a JSON object")
    decision = result.get("decision")
    if (
        exit_code != 0
        and isinstance(decision, str)
        and decision.strip().lower() in AFFIRMATIVE_DECISIONS
    ):
        raise KeiProxyAuthorizeError("exit_mismatch", f"{decision} with exit {exit_code}")
    return result
