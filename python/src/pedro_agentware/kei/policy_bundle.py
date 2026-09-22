"""Policy bundle lifecycle — fetch, persist, activate, refresh.

Fetches installation/workspace policy bundles from the control plane,
persists them locally, activates them for policy evaluation, and refreshes
boundedly with fail-closed behaviour.  Denies every tool call until a
valid bundle is loaded; on refresh failure the last valid bundle is kept
(and if none has ever been loaded, the deny-everything gate stays closed).

Environment variables
---------------------
``KEI_RUNTIME_CONTROL_PLANE_URL``
    Base URL of the control-plane endpoint that serves policy bundles.
``KEI_RUNTIME_TOKEN``
    Bearer token for authenticating with the control plane.

Legacy URL fallbacks (checked only when the runtime URL is unset):
    ``ABAC_URL``, ``KEI_API_URL``

The deprecated token name ``KEI_HARNESS_TOKEN`` is no longer accepted:
if it is set while ``KEI_RUNTIME_TOKEN`` is not, credential resolution
fails closed (ADR-020, decision 2).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import random
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    pass

from ..middleware.policy import Condition, Operator, Policy, RateLimit, Rule
from ..middleware.types import Action, CallerContext, Decision

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Env-var constants
# ---------------------------------------------------------------------------

RUNTIME_CONTROL_PLANE_URL_ENV = "KEI_RUNTIME_CONTROL_PLANE_URL"
RUNTIME_TOKEN_ENV = "KEI_RUNTIME_TOKEN"

# Legacy URL fallbacks — checked only when the runtime URL is unset.
LEGACY_ABAC_URL_ENV = "ABAC_URL"
LEGACY_KEI_API_URL_ENV = "KEI_API_URL"
# Deprecated token name — never read as a token source; presence without
# KEI_RUNTIME_TOKEN fails closed (ADR-020, decision 2).
LEGACY_BOOTSTRAP_TOKEN_ENV = "KEI_HARNESS_TOKEN"

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

DEFAULT_REFRESH_INTERVAL_SECONDS = 300  # 5 minutes
MIN_REFRESH_INTERVAL_SECONDS = 30
DEFAULT_PERSIST_PATH = "~/.agentware/policy_bundle.json"
DEFAULT_CONTROL_PLANE_TIMEOUT_SECONDS = 10

# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class PolicyBundleError(Exception):
    """Base error for policy-bundle operations."""


class BundleFetchError(PolicyBundleError):
    """Failed to fetch the policy bundle from the control plane."""


class BundleParseError(PolicyBundleError):
    """Failed to parse the fetched policy bundle."""


class BundleNotLoadedError(PolicyBundleError):
    """No bundle has been loaded yet — deny by construction."""


# ---------------------------------------------------------------------------
# Bundle data model
# ---------------------------------------------------------------------------


# Operator string-to-enum mapping shared by bundle and rule parsing.
_CONDITION_OPERATOR_MAP: dict[str, Operator] = {
    "eq": Operator.EQ,
    "not_eq": Operator.NOT_EQ,
    "contains": Operator.CONTAINS,
    "not_contains": Operator.NOT_CONTAINS,
    "matches": Operator.MATCHES,
    "not_matches": Operator.NOT_MATCHES,
    "exists": Operator.EXISTS,
    "not_exists": Operator.NOT_EXISTS,
}


@dataclass
class PolicyBundleData:
    """A policy bundle fetched from the control plane and ready for evaluation.

    ``rules`` are translated from the wire format into the middleware's own
    :class:`~pedro_agentware.middleware.policy.Rule` objects so the lifecycle
    can be used as a drop-in ``PolicyEvaluator``.
    """

    rules: list[Rule] = field(default_factory=list)
    version: str = ""
    bundle_id: str = ""
    default_deny: bool = True
    expires_at: datetime | None = None
    fetched_at: datetime = field(default_factory=datetime.now)

    @classmethod
    def from_wire(cls, data: dict[str, Any]) -> PolicyBundleData:
        """Parse a policy-bundle JSON response from the control plane.

        Fails closed: any unrecognised field, missing version, or
        unparseable rule causes a :class:`BundleParseError`.
        """
        version = data.get("version", "")
        if not version:
            raise BundleParseError("bundle is missing 'version'")

        bundle_id = str(data.get("bundle_id", ""))
        default_deny = bool(data.get("default_deny", True))
        expires_at: datetime | None = None
        expires_raw = data.get("expires_at")
        if expires_raw:
            try:
                expires_at = datetime.fromisoformat(str(expires_raw))
            except (ValueError, TypeError):
                raise BundleParseError(f"unparseable expires_at: {expires_raw!r}")

        raw_rules = data.get("rules", [])
        if not isinstance(raw_rules, list):
            raise BundleParseError("'rules' must be a list")

        rules = [_parse_rule(r) for r in raw_rules]

        return cls(
            rules=rules,
            version=version,
            bundle_id=bundle_id,
            default_deny=default_deny,
            expires_at=expires_at,
        )

    def is_expired(self, now: datetime | None = None) -> bool:
        if self.expires_at is None:
            return False
        return (now or datetime.now()) >= self.expires_at

    def to_policy(self) -> Policy:
        """Convert this bundle to a :class:`~pedro_agentware.middleware.policy.Policy`."""
        return Policy(rules=self.rules, default_deny=self.default_deny)

    def digest(self) -> str:
        """Return a deterministic content hash for change detection."""
        raw = json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(raw.encode()).hexdigest()[:16]

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "bundle_id": self.bundle_id,
            "default_deny": self.default_deny,
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "fetched_at": self.fetched_at.isoformat(),
            "rules": [_rule_to_dict(r) for r in self.rules],
        }


def _parse_rule(raw: dict[str, Any]) -> Rule:
    """Translate a wire-format rule dict into a middleware ``Rule``."""
    name = raw.get("name", "")
    if not name:
        raise BundleParseError("rule is missing 'name'")

    raw_tools = raw.get("tools", [])
    if not isinstance(raw_tools, list):
        raise BundleParseError(f"rule {name!r}: 'tools' must be a list")

    raw_action = str(raw.get("action", "deny")).strip().lower()
    try:
        action = Action(raw_action)
    except ValueError:
        raise BundleParseError(f"rule {name!r}: unknown action {raw_action!r}")

    conditions: list[Condition] = []
    for c in raw.get("conditions", []):
        conditions.append(_parse_condition(name, c))

    max_rate: RateLimit | None = None
    raw_rate = raw.get("max_rate")
    if raw_rate is not None:
        count = int(raw_rate.get("count", 0))
        window_seconds = int(raw_rate.get("window_seconds", 60))
        if count > 0 and window_seconds > 0:
            max_rate = RateLimit(count=count, window=timedelta(seconds=window_seconds))

    redact_fields = raw.get("redact_fields", [])
    if not isinstance(redact_fields, list):
        redact_fields = []

    return Rule(
        name=name,
        tools=raw_tools,
        action=action,
        conditions=conditions,
        max_rate=max_rate,
        redact_fields=redact_fields,
    )


def _parse_condition(rule_name: str, raw: Any) -> Condition:
    """Translate a wire-format condition dict."""
    if not isinstance(raw, dict):
        raise BundleParseError(
            f"rule {rule_name!r}: condition must be a dict, got {type(raw).__name__}"
        )
    field = str(raw.get("field", ""))
    if not field:
        raise BundleParseError(f"rule {rule_name!r}: condition is missing 'field'")
    op_str = str(raw.get("operator", "eq")).strip().lower()
    op = _CONDITION_OPERATOR_MAP.get(op_str)
    if op is None:
        raise BundleParseError(f"rule {rule_name!r}: unknown condition operator {op_str!r}")
    value = str(raw.get("value", ""))
    return Condition(field=field, operator=op, value=value)


def _rule_to_dict(rule: Rule) -> dict[str, Any]:
    """Serialize a middleware ``Rule`` back to a dict for persistence."""
    return {
        "name": rule.name,
        "tools": list(rule.tools),
        "action": rule.action.value,
        "conditions": [
            {"field": c.field, "operator": c.operator.value, "value": c.value}
            for c in rule.conditions
        ],
        "max_rate": (
            {
                "count": rule.max_rate.count,
                "window_seconds": int(rule.max_rate.window.total_seconds()),
            }
            if rule.max_rate
            else None
        ),
        "redact_fields": list(rule.redact_fields),
    }


# ---------------------------------------------------------------------------
# Credential resolution
# ---------------------------------------------------------------------------


def _resolve_control_plane_url() -> str:
    """Resolve the control-plane URL from env vars (new then legacy)."""
    url = os.environ.get(RUNTIME_CONTROL_PLANE_URL_ENV)
    if url:
        return url
    url = os.environ.get(LEGACY_ABAC_URL_ENV)
    if url:
        logger.warning(
            "Using deprecated %s; migrate to %s",
            LEGACY_ABAC_URL_ENV,
            RUNTIME_CONTROL_PLANE_URL_ENV,
        )
        return url
    url = os.environ.get(LEGACY_KEI_API_URL_ENV)
    if url:
        logger.warning(
            "Using deprecated %s; migrate to %s",
            LEGACY_KEI_API_URL_ENV,
            RUNTIME_CONTROL_PLANE_URL_ENV,
        )
        return url
    raise BundleFetchError(
        f"Control-plane URL not configured. Set {RUNTIME_CONTROL_PLANE_URL_ENV} "
        f"(or legacy {LEGACY_ABAC_URL_ENV}/{LEGACY_KEI_API_URL_ENV})."
    )


def _legacy_token_error() -> BundleFetchError:
    """Error raised when only the deprecated token name is set."""
    return BundleFetchError(
        f"{LEGACY_BOOTSTRAP_TOKEN_ENV} is deprecated and no longer accepted; "
        f"set {RUNTIME_TOKEN_ENV} instead."
    )


def _resolve_runtime_token() -> str:
    """Resolve the runtime token from the environment.

    Only ``KEI_RUNTIME_TOKEN`` is accepted.  The deprecated
    ``KEI_HARNESS_TOKEN`` name is no longer read (ADR-020, decision 2):
    if it is the only token set, resolution fails closed with an error
    naming the replacement.
    """
    token = os.environ.get(RUNTIME_TOKEN_ENV)
    if token:
        return token
    if os.environ.get(LEGACY_BOOTSTRAP_TOKEN_ENV):
        raise _legacy_token_error()
    raise BundleFetchError(f"Runtime token not configured. Set {RUNTIME_TOKEN_ENV}.")


# ---------------------------------------------------------------------------
# HTTP fetch
# ---------------------------------------------------------------------------


async def _fetch_with_httpx(
    url: str,
    headers: dict[str, str],
    timeout: float,
) -> dict[str, Any]:
    import httpx

    async with httpx.AsyncClient(timeout=timeout) as client:
        try:
            response = await client.get(url, headers=headers)
            response.raise_for_status()
            return response.json()
        except httpx.HTTPStatusError as exc:
            raise BundleFetchError(
                f"Control plane returned HTTP {exc.response.status_code}: {exc.response.text[:500]}"
            ) from exc
        except httpx.TimeoutException as exc:
            raise BundleFetchError(f"Control plane timed out after {timeout}s: {url}") from exc
        except httpx.RequestError as exc:
            raise BundleFetchError(f"Control plane unreachable: {exc}") from exc


async def _fetch_with_urllib(
    url: str,
    headers: dict[str, str],
    timeout: float,
) -> dict[str, Any]:
    import urllib.error
    import urllib.request

    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8")
            return json.loads(body)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        raise BundleFetchError(f"Control plane returned HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise BundleFetchError(f"Control plane unreachable: {exc.reason}") from exc
    except TimeoutError as exc:
        raise BundleFetchError(f"Control plane timed out after {timeout}s: {url}") from exc


async def _fetch_bundle_from_plane(
    base_url: str,
    token: str,
    timeout: float = DEFAULT_CONTROL_PLANE_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Fetch a policy bundle from the control plane.

    The endpoint is expected at ``<base_url>/v1/policy-bundle`` and returns
    a JSON object matching the wire format expected by
    :meth:`PolicyBundleData.from_wire`.

    Uses ``httpx`` when available (async natively), otherwise falls back
    to ``urllib`` (thread-pooled via ``asyncio.to_thread``).
    """
    url = base_url.rstrip("/") + "/v1/policy-bundle"
    headers: dict[str, str] = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "User-Agent": "pedro-agentware/0.1",
    }

    try:
        return await _fetch_with_httpx(url, headers, timeout)
    except ImportError:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, _fetch_with_urllib_sync, url, headers, timeout)


def _fetch_with_urllib_sync(
    url: str,
    headers: dict[str, str],
    timeout: float,
) -> dict[str, Any]:
    """Synchronous fallback using urllib (runs in a thread-pool executor)."""
    import urllib.error
    import urllib.request

    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8")
            return json.loads(body)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        raise BundleFetchError(f"Control plane returned HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise BundleFetchError(f"Control plane unreachable: {exc.reason}") from exc
    except TimeoutError as exc:
        raise BundleFetchError(f"Control plane timed out after {timeout}s: {url}") from exc


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def _default_persist_path() -> Path:
    return Path(DEFAULT_PERSIST_PATH).expanduser().resolve()


def _persist_bundle(bundle: PolicyBundleData, path: Path) -> None:
    """Write a bundle to disk atomically."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(bundle.to_dict(), indent=2), encoding="utf-8")
    tmp.rename(path)


def _load_bundle_from_disk(path: Path) -> PolicyBundleData | None:
    """Load a previously persisted bundle from disk.

    Returns ``None`` when the file does not exist or is corrupt (fail-closed).
    """
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return PolicyBundleData.from_wire(data)
    except (json.JSONDecodeError, BundleParseError, OSError) as exc:
        logger.warning("Failed to load cached policy bundle from %s: %s", path, exc)
        return None


# ---------------------------------------------------------------------------
# Backoff
# ---------------------------------------------------------------------------


def _jittered_backoff(attempt: int, base: float = 1.0, cap: float = 60.0) -> float:
    """Exponential backoff with full jitter, capped at ``cap`` seconds."""
    delay = min(cap, base * (2**attempt))
    return delay * (0.5 + random.random() * 0.5)


# ---------------------------------------------------------------------------
# The lifecycle
# ---------------------------------------------------------------------------


class PolicyBundleLifecycle:
    """Manages the policy-bundle lifecycle.

    Behaviour
    ---------
    *   **Deny-until-loaded** — ``evaluate()`` returns ``DENY`` with
        ``BundleNotLoadedError`` until a bundle is successfully fetched and
        activated.
    *   **Activate on first successful fetch** — the bundle is parsed,
        persisted, and used for all subsequent ``evaluate()`` calls.
    *   **Bounded refresh** — :meth:`refresh` fetches a new bundle,
        validates it, and swaps it in atomically.  On failure the current
        bundle (if any) is kept.  Jittered exponential backoff prevents
        thundering-herd.
    *   **Fail-closed** — a missing or corrupt persisted bundle denies.
        A missing control-plane URL or token raises immediately on
        construction / refresh so the operator knows.
    *   **Local-agent compatibility** — when no control-plane
        credentials are configured (runtime env vars unset, no legacy
        URL fallbacks), the lifecycle logs a warning and **allows** all
        calls (local-only mode).  This preserves
        ``docs/action-tool-boundary.md`` invariant 3 (local loop without
        ABAC connector dependency).  The deprecated
        ``KEI_HARNESS_TOKEN`` name never triggers local-only mode; it
        fails closed at construction (ADR-020, decision 2).
    """

    def __init__(
        self,
        control_plane_url: str | None = None,
        runtime_token: str | None = None,
        persist_path: str | Path | None = None,
        refresh_interval: float = DEFAULT_REFRESH_INTERVAL_SECONDS,
        fetch_timeout: float = DEFAULT_CONTROL_PLANE_TIMEOUT_SECONDS,
        fetch_fn: Callable[..., Any] | None = None,
    ):
        self._control_plane_url = control_plane_url
        self._runtime_token = runtime_token
        self._persist_path = (
            Path(persist_path).expanduser().resolve() if persist_path else _default_persist_path()
        )
        self._refresh_interval = max(refresh_interval, MIN_REFRESH_INTERVAL_SECONDS)
        self._fetch_timeout = fetch_timeout
        self._fetch_fn = fetch_fn or _fetch_bundle_from_plane

        # State
        self._bundle: PolicyBundleData | None = None
        self._loaded: bool = False
        self._last_refresh_attempt: float = 0.0
        self._refresh_task: asyncio.Task[None] | None = None
        self._local_only: bool = False

        # Resolve credentials once at construction.
        self._resolved_url: str | None = None
        self._resolved_token: str | None = None
        self._resolve_credentials()

    # ------------------------------------------------------------------
    # Credential resolution
    # ------------------------------------------------------------------

    def _resolve_credentials(self) -> None:
        """Resolve control-plane URL and token.

        When no control-plane credentials are configured (runtime env
        vars unset, no legacy URL fallbacks), mark as *local-only* so
        that evaluate() allows all calls.

        The deprecated ``KEI_HARNESS_TOKEN`` name is never accepted as a
        token source (ADR-020, decision 2): if it is set while
        ``KEI_RUNTIME_TOKEN`` is not, construction fails closed instead
        of degrading to local-only mode.
        """
        url = self._control_plane_url
        token = self._runtime_token

        if url and token:
            self._resolved_url = url
            self._resolved_token = token
            return

        if (
            token is None
            and os.environ.get(RUNTIME_TOKEN_ENV) is None
            and os.environ.get(LEGACY_BOOTSTRAP_TOKEN_ENV) is not None
        ):
            raise _legacy_token_error()

        try:
            self._resolved_url = url or _resolve_control_plane_url()
        except BundleFetchError:
            pass

        try:
            self._resolved_token = token or _resolve_runtime_token()
        except BundleFetchError:
            pass

        if not self._resolved_url or not self._resolved_token:
            logger.info(
                "No control-plane credentials configured (%s/%s not set). "
                "Operating in local-only mode — all tool calls allowed.",
                RUNTIME_CONTROL_PLANE_URL_ENV,
                RUNTIME_TOKEN_ENV,
            )
            self._local_only = True

    # ------------------------------------------------------------------
    # Lifecycle: start / refresh / stop
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Initialise the lifecycle.

        1. Try to load a previously persisted bundle from disk.
        2. Attempt a fresh fetch from the control plane.
        3. If fetch succeeds, persist and activate.
        4. If fetch fails but a cached bundle exists, activate the cache.
        5. If fetch fails and no cache exists, stay in deny-until-loaded
           state so that every ``evaluate()`` returns ``DENY``.
        """
        if self._local_only:
            self._loaded = True
            return

        # Step 1: load cached bundle
        cached = _load_bundle_from_disk(self._persist_path)
        if cached is not None:
            logger.info(
                "Loaded cached policy bundle version=%s id=%s",
                cached.version,
                cached.bundle_id,
            )
            self._bundle = cached

        # Step 2: fresh fetch
        try:
            bundle = await self._do_fetch()
            self._bundle = bundle
            self._loaded = True
            _persist_bundle(bundle, self._persist_path)
            logger.info(
                "Policy bundle activated: version=%s id=%s rules=%d",
                bundle.version,
                bundle.bundle_id,
                len(bundle.rules),
            )
        except PolicyBundleError:
            if self._bundle is not None:
                logger.warning(
                    "Fresh fetch failed; using cached bundle version=%s id=%s",
                    self._bundle.version,
                    self._bundle.bundle_id,
                )
                self._loaded = True
            else:
                logger.error(
                    "No policy bundle available. All tool calls will be denied "
                    "until a successful fetch."
                )

    async def refresh(self) -> PolicyBundleData | None:
        """Perform a bounded refresh of the policy bundle.

        Returns the new bundle on success, or ``None`` when the refresh
        fails (the current bundle is preserved).  Implements jittered
        exponential backoff so that repeated failures do not hammer the
        control plane.

        Callers should throttle calls to this method; a 5-second minimum
        interval between actual HTTP requests is enforced in-process.
        """
        if self._local_only:
            return None

        now = time.monotonic()
        if now - self._last_refresh_attempt < MIN_REFRESH_INTERVAL_SECONDS:
            return self._bundle
        self._last_refresh_attempt = now

        try:
            bundle = await self._do_fetch()
            self._bundle = bundle
            self._loaded = True
            _persist_bundle(bundle, self._persist_path)
            logger.info(
                "Policy bundle refreshed: version=%s id=%s rules=%d",
                bundle.version,
                bundle.bundle_id,
                len(bundle.rules),
            )
            return bundle
        except PolicyBundleError as exc:
            logger.warning(
                "Policy bundle refresh failed: %s. Keeping current bundle (loaded=%s).",
                exc,
                self._loaded,
            )
            return None

    async def _do_fetch(self) -> PolicyBundleData:
        """Fetch and parse a policy bundle. Raises ``PolicyBundleError`` on failure."""
        url = self._resolved_url
        token = self._resolved_token
        if not url or not token:
            raise BundleFetchError(
                f"Control-plane credentials not resolved; set {RUNTIME_CONTROL_PLANE_URL_ENV} "
                f"and {RUNTIME_TOKEN_ENV}"
            )
        try:
            raw = await self._fetch_fn(url, token, timeout=self._fetch_timeout)
        except Exception as exc:
            raise BundleFetchError(f"Fetch failed: {exc}") from exc
        bundle = PolicyBundleData.from_wire(raw)
        return bundle

    # ------------------------------------------------------------------
    # PolicyEvaluator protocol
    # ------------------------------------------------------------------

    def evaluate(self, tool_name: str, args: dict[str, Any], caller: CallerContext) -> Decision:
        """Evaluate a tool call against the current policy bundle.

        Denies every call when:
        *   No bundle has ever been loaded.
        *   The bundle is expired.
        *   No matching rule is found and ``default_deny`` is ``True``.
        """
        if not self._loaded or self._bundle is None:
            if self._local_only:
                return Decision(
                    action=Action.ALLOW,
                    rule="local-only",
                    reason="local-only mode (no control plane configured)",
                )
            return Decision(
                action=Action.DENY,
                rule="no-bundle",
                reason="no policy bundle loaded; all tool calls denied until a valid bundle is fetched",
            )

        bundle = self._bundle

        if bundle.is_expired():
            return Decision(
                action=Action.DENY,
                rule="expired-bundle",
                reason=f"policy bundle version={bundle.version} expired at {bundle.expires_at}",
            )

        return bundle.to_policy().evaluate(tool_name, args, caller)

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------

    @property
    def is_loaded(self) -> bool:
        return self._loaded

    @property
    def active_bundle(self) -> PolicyBundleData | None:
        return self._bundle

    @property
    def is_local_only(self) -> bool:
        return self._local_only
