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
  everything else in the parent environment is dropped.
- stdout can carry a credential or an enrollment claim link and stderr is
  free-form, so neither is ever logged or copied into an error message.
"""

import json
import os
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
# fixtures/kei/authorize-cases.v1.json "child_env.allowlist". KEI_PROXY_*
# identity variables are deliberately absent: identity travels as flags so a
# stale parent value can never be attributed to this call.
AUTHORIZE_CHILD_ENV_ALLOWLIST = (
    "PATH",
    "HOME",
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
    ) -> None:
        """Build a client.

        Args:
            executable: Path or name of the kei-proxy binary.
            timeout: Seconds to wait for one authorize call.
            env: Parent environment to draw allowlisted variables from.
                Defaults to ``os.environ``.
            extra_env: Variables passed to the child verbatim, for settings
                outside the allowlist (e.g. a secret-store profile).
        """
        self._executable = executable
        self._timeout = timeout
        self._env = env
        self._extra_env = dict(extra_env or {})

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
            proc = subprocess.run(
                argv,
                env=env,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=self._timeout,
                check=False,
            )
        except subprocess.TimeoutExpired:
            raise KeiProxyAuthorizeError(
                "proxy_timeout", f"no answer within {self._timeout:g}s"
            ) from None
        except OSError as exc:
            raise KeiProxyAuthorizeError(
                "proxy_unavailable", f"could not start kei-proxy ({type(exc).__name__})"
            ) from None

        return parse_authorize_output(proc.stdout, proc.returncode)


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
