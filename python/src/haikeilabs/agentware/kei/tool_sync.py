"""Sync a harness's governed tool manifest to the Kei control plane.

Kei's policy bundle only allows tools registered in the installation's tool
catalog, so a harness registers its tools before it serves:
``PUT $KEI_RUNTIME_CONTROL_PLANE_URL/api/v1/runtime/tools`` with the
``application/vnd.kei.tool-manifest.v3+json`` document, authenticated with
``KEI_RUNTIME_TOKEN``, the two variables kei-proxy itself reads.

- The PUT *replaces* the installation's tools (it is idempotent: a restart
  sends the same manifest and the catalog converges on it). So a manifest with
  no tools is **never** sent: it would unregister every tool. When tools are
  expected that is an error; when they are not, the sync is skipped.
- A network failure or non-2xx answer is retried with exponential backoff;
  once the attempts run out :class:`ToolManifestSyncError` is raised, and the
  harness should refuse to serve.
- The control plane is reached the way kei-proxy reaches it: through
  ``KEI_PROXY_EGRESS_PROXY`` unless ``KEI_PROXY_NO_PROXY`` bypasses the host.
  With an egress proxy configured, ambient ``HTTP(S)_PROXY`` variables are
  ignored. The proxy is set on this client only, never exported.
- The token goes in the ``Authorization`` header and nowhere else: not in a
  log line, an error message or the config's repr.
- Callers hold the manifest as JSON text, a mapping, typed
  :class:`~haikeilabs.agentware.toolspec.ToolSpec` values, or a file path;
  every entry point sends the same PUT for the same tools.

This is the Python home of kei-openwebui's ``tool_sync.py``.
"""

import json
import logging
import os
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from urllib.request import proxy_bypass_environment  # type: ignore[attr-defined]  # not in typeshed

import httpx

from haikeilabs.agentware.toolspec import ToolSpec, load_manifest_v3, serialize_tool_specs

from .policy_bundle import RUNTIME_CONTROL_PLANE_URL_ENV, RUNTIME_TOKEN_ENV

__all__ = [
    "EGRESS_PROXY_ENV",
    "NO_PROXY_ENV",
    "TOOL_MANIFEST_V3_MEDIA_TYPE",
    "TOOLS_PATH",
    "ToolManifestSyncConfig",
    "ToolManifestSyncError",
    "ToolManifestSyncResult",
    "manifest_tool_count",
    "sync_manifest_path",
    "sync_tool_manifest",
    "sync_tool_specs",
]

logger = logging.getLogger(__name__)

TOOLS_PATH = "/api/v1/runtime/tools"
TOOL_MANIFEST_V3_MEDIA_TYPE = "application/vnd.kei.tool-manifest.v3+json"
EGRESS_PROXY_ENV = "KEI_PROXY_EGRESS_PROXY"
NO_PROXY_ENV = "KEI_PROXY_NO_PROXY"

DEFAULT_ATTEMPTS = 5
DEFAULT_BACKOFF_SECONDS = 1.0
MAX_BACKOFF_SECONDS = 16.0
DEFAULT_TIMEOUT_SECONDS = 30.0
_ERROR_BODY_LIMIT = 300


class ToolManifestSyncError(Exception):
    """The manifest was not registered with Kei. The harness must not serve."""


@dataclass(frozen=True)
class ToolManifestSyncConfig:
    """Where and how to register the manifest. The token is kept out of ``repr``."""

    control_plane_url: str
    token: str = field(repr=False)
    egress_proxy: str = ""
    no_proxy: str = ""
    attempts: int = DEFAULT_ATTEMPTS
    backoff_seconds: float = DEFAULT_BACKOFF_SECONDS
    timeout: float = DEFAULT_TIMEOUT_SECONDS
    expect_tools: bool = True

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "ToolManifestSyncConfig":
        """Read the variables kei-proxy reads. Raises :class:`ToolManifestSyncError`."""
        env = os.environ if env is None else env
        url = env.get(RUNTIME_CONTROL_PLANE_URL_ENV, "").strip()
        token = env.get(RUNTIME_TOKEN_ENV, "").strip()
        if not url:
            raise ToolManifestSyncError(f"{RUNTIME_CONTROL_PLANE_URL_ENV} is not set")
        if not token:
            raise ToolManifestSyncError(f"{RUNTIME_TOKEN_ENV} is not set")
        parts = urlsplit(url)
        if parts.scheme not in ("https", "http") or not parts.netloc:
            raise ToolManifestSyncError(f"{RUNTIME_CONTROL_PLANE_URL_ENV} is not an http(s) URL")
        return cls(
            control_plane_url=url,
            token=token,
            egress_proxy=env.get(EGRESS_PROXY_ENV, "").strip(),
            no_proxy=env.get(NO_PROXY_ENV, "").strip(),
        )

    @property
    def tools_url(self) -> str:
        return self.control_plane_url.rstrip("/") + TOOLS_PATH

    def proxy(self) -> str | None:
        """The egress proxy for the control plane, or ``None`` to go direct."""
        if not self.egress_proxy:
            return None
        host = urlsplit(self.control_plane_url).hostname or ""
        if self.no_proxy and proxy_bypass_environment(host, {"no": self.no_proxy}):
            return None
        return self.egress_proxy


@dataclass(frozen=True)
class ToolManifestSyncResult:
    """What a sync did: whether it sent, how many tools, and the counts Kei answered."""

    sent: bool
    tool_count: int
    counts: dict[str, Any] = field(default_factory=dict)


def manifest_tool_count(manifest: str | Mapping[str, Any]) -> int:
    """The number of entries in the manifest's ``tools`` list (0 when absent)."""
    document = _document(manifest)
    tools = document.get("tools")
    return len(tools) if isinstance(tools, list) else 0


def sync_tool_manifest(
    manifest: str | Mapping[str, Any],
    config: ToolManifestSyncConfig,
    *,
    expect_tools: bool | None = None,
    transport: httpx.BaseTransport | None = None,
    sleep: Callable[[float], Any] = time.sleep,
) -> ToolManifestSyncResult:
    """Register ``manifest`` with Kei (replace mode).

    Args:
        manifest: The tool manifest v3 document, as JSON text or a mapping.
        config: Control plane, token and egress settings.
        expect_tools: Whether this harness is configured with tools; ``None``
            (the default) takes it from ``config.expect_tools``. An empty
            manifest is never sent; it raises when tools are expected and
            returns ``sent=False`` when they are not.
        transport: Replaces the HTTP transport (tests).
        sleep: Replaces ``time.sleep`` between attempts (tests).

    Raises:
        ToolManifestSyncError: The manifest is unreadable, empty while tools
            are expected, or every attempt failed.
    """
    document = _document(manifest)
    count = manifest_tool_count(document)
    if expect_tools is None:
        expect_tools = config.expect_tools
    if count == 0:
        if expect_tools:
            raise ToolManifestSyncError(
                f"refusing to PUT an empty manifest to {TOOLS_PATH}: it would unregister every tool"
            )
        logger.info("tool sync: no tools configured; nothing sent to %s", TOOLS_PATH)
        return ToolManifestSyncResult(sent=False, tool_count=0)

    headers = {
        "Authorization": f"Bearer {config.token}",
        "Content-Type": TOOL_MANIFEST_V3_MEDIA_TYPE,
        "Accept": TOOL_MANIFEST_V3_MEDIA_TYPE,
    }
    body = json.dumps(document).encode()
    attempts = max(1, config.attempts)
    last = "no attempt made"
    with _client(config, transport) as client:
        for attempt in range(1, attempts + 1):
            try:
                response = client.put(config.tools_url, content=body, headers=headers)
            except httpx.HTTPError as exc:
                last = f"network error: {type(exc).__name__}"
            else:
                if 200 <= response.status_code < 300:
                    counts = _counts(response)
                    logger.info(
                        "tool sync: registered %d tool(s) with Kei at %s (%s)",
                        count,
                        TOOLS_PATH,
                        ", ".join(f"{k}={v}" for k, v in sorted(counts.items()))
                        or "no counts returned",
                    )
                    return ToolManifestSyncResult(sent=True, tool_count=count, counts=counts)
                last = f"HTTP {response.status_code}: {response.text[:_ERROR_BODY_LIMIT]}"
            last = _scrub(last, config.token)
            if attempt < attempts:
                delay = min(config.backoff_seconds * 2 ** (attempt - 1), MAX_BACKOFF_SECONDS)
                logger.warning(
                    "tool sync attempt %d/%d failed (%s); retrying in %.0fs",
                    attempt,
                    attempts,
                    last,
                    delay,
                )
                sleep(delay)
    raise ToolManifestSyncError(f"PUT {TOOLS_PATH} failed after {attempts} attempt(s): {last}")


def sync_tool_specs(
    specs: Sequence[ToolSpec],
    config: ToolManifestSyncConfig,
    *,
    expect_tools: bool | None = None,
    transport: httpx.BaseTransport | None = None,
    sleep: Callable[[float], Any] = time.sleep,
) -> ToolManifestSyncResult:
    """Serialize typed specs and register them with Kei (replace mode).

    Sends exactly what :func:`sync_tool_manifest` sends for the same
    manifest, so the str/Mapping callers keep their behaviour.

    Args:
        specs: The typed tool declarations, in registration order.
        config, expect_tools, transport, sleep: As in :func:`sync_tool_manifest`.

    Raises:
        ValueError: A spec lacks the runtime routing metadata ``to_wire``
            requires (checked before anything is sent).
        ToolManifestSyncError: As in :func:`sync_tool_manifest`.
    """
    return sync_tool_manifest(
        serialize_tool_specs(list(specs)),
        config,
        expect_tools=expect_tools,
        transport=transport,
        sleep=sleep,
    )


def sync_manifest_path(
    path: str | Path,
    config: ToolManifestSyncConfig,
    *,
    expect_tools: bool | None = None,
    transport: httpx.BaseTransport | None = None,
    sleep: Callable[[float], Any] = time.sleep,
) -> ToolManifestSyncResult:
    """Load a kei.tool-manifest/v3 file and register its tools (replace mode).

    The file is parsed with :func:`load_manifest_v3` and the typed specs are
    re-serialized, so what is sent is the marshalled form of what was loaded:
    a bad document raises :class:`ManifestV3Error` and nothing is sent.

    Args:
        path: Path to the ``application/vnd.kei.tool-manifest.v3+json`` file.
        config, expect_tools, transport, sleep: As in :func:`sync_tool_manifest`.

    Raises:
        ManifestV3Error: The document violates the manifest contract.
        OSError: The file is missing or unreadable.
        ToolManifestSyncError: As in :func:`sync_tool_manifest`.
    """
    return sync_tool_specs(
        load_manifest_v3(path),
        config,
        expect_tools=expect_tools,
        transport=transport,
        sleep=sleep,
    )


def _document(manifest: str | Mapping[str, Any]) -> dict[str, Any]:
    if isinstance(manifest, str):
        try:
            parsed = json.loads(manifest)
        except ValueError:
            raise ToolManifestSyncError("the tool manifest is not JSON") from None
    else:
        parsed = dict(manifest)
    if not isinstance(parsed, dict):
        raise ToolManifestSyncError("the tool manifest is not a JSON object")
    return parsed


def _client(config: ToolManifestSyncConfig, transport: httpx.BaseTransport | None) -> httpx.Client:
    if transport is not None:
        return httpx.Client(transport=transport, timeout=config.timeout, follow_redirects=False)
    if config.egress_proxy:
        # The egress contract decides (proxy or bypass); ambient proxy variables don't.
        return httpx.Client(
            proxy=config.proxy(), trust_env=False, timeout=config.timeout, follow_redirects=False
        )
    # No egress proxy configured: the environment's own proxy settings, as kei-proxy would use.
    return httpx.Client(timeout=config.timeout, follow_redirects=False)


def _counts(response: httpx.Response) -> dict[str, Any]:
    try:
        counts = response.json()
    except ValueError:
        return {}
    return counts if isinstance(counts, dict) else {}


def _scrub(text: str, token: str) -> str:
    return text.replace(token, "[redacted]") if token else text
