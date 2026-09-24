"""RuntimeLink contract: the harness → Agentware → kei-connector-runtime heartbeat.

Python mirror of the Go reference ``go/kei/runtimelink`` for
``docs/specs/runtime-heartbeat-liveness.md`` (HAI-141). This module holds
contract types only (configuration, status, identity, failure classes, the
child's JSONL wire events, and redacted lifecycle events). It never spawns a
process, opens a network connection, or reads a credential value. The only
heartbeat path is harness → RuntimeLink → kei-connector-runtime; nothing here
talks to the catalog.

All three SDK languages share the fixtures in
``testing/contracts/runtime-link``; a change there is a contract change.
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import random
import re
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Protocol
from urllib.parse import urlsplit

CONTRACT_VERSION = 1
"""The child stdout event version (``"v"``) this SDK accepts."""

MAX_CHILD_LINE_BYTES = 4096
"""Bound on one child stdout line, excluding the newline."""

EVENT_BUFFER_SIZE = 64
"""Capacity of the RuntimeLink event ring."""

SDK_LANG = "python"


class HarnessKind(str, Enum):
    """Closed set of harnesses that may start a RuntimeLink."""

    ASSISTANT = "assistant"
    PDE = "pde"
    CHAT_DISCORD = "chat-discord"
    CHAT_SLACK = "chat-slack"
    CHAT_TEAMS = "chat-teams"
    CLI = "cli"


class LinkState(str, Enum):
    """RuntimeLink supervisor state."""

    DISABLED = "disabled"
    STARTING = "starting"
    CONNECTED = "connected"
    DEGRADED = "degraded"
    RECONNECTING = "reconnecting"
    TERMINAL = "terminal"
    STOPPED = "stopped"


class FailureClass(str, Enum):
    """Closed failure taxonomy (spec §6.3).

    Separates harness↔runtime failures from runtime↔catalog failures so
    alerts route to the right owner.
    """

    CONFIG_INVALID = "config_invalid"
    RUNTIME_UNAVAILABLE = "runtime_unavailable"
    RUNTIME_CRASHLOOP = "runtime_crashloop"
    RUNTIME_UNRESPONSIVE = "runtime_unresponsive"
    CONTRACT_MISMATCH = "contract_mismatch"
    CATALOG_UNREACHABLE = "catalog_unreachable"
    CATALOG_TIMEOUT = "catalog_timeout"
    CATALOG_ERROR = "catalog_error"
    CATALOG_BACKPRESSURE = "catalog_backpressure"
    INSTALLATION_UNAUTHORIZED = "installation_unauthorized"
    INSTALLATION_STALE = "installation_stale"
    INSTALLATION_OFFLINE = "installation_offline"
    AUDIT_BACKLOG = "audit_backlog"
    LEGACY_RUNTIME_UNVERIFIED = "legacy_runtime_unverified"


class BeatOutcome(str, Enum):
    """Runtime-reported result of one catalog heartbeat POST."""

    OK = "ok"
    CATALOG_UNREACHABLE = "catalog_unreachable"
    CATALOG_TIMEOUT = "catalog_timeout"
    CATALOG_ERROR = "catalog_error"
    CATALOG_BACKPRESSURE = "catalog_backpressure"
    CATALOG_REJECTED = "catalog_rejected"
    UNAUTHORIZED = "unauthorized"


class LifecycleEventName(str, Enum):
    """Closed set of SDK lifecycle events (spec §5.3)."""

    STARTED = "runtime.link.started"
    CONNECTED = "runtime.link.connected"
    DEGRADED = "runtime.link.degraded"
    RECONNECTING = "runtime.link.reconnecting"
    TERMINAL = "runtime.link.terminal"
    STOPPED = "runtime.link.stopped"


SDK_LANGS: tuple[str, ...] = ("go", "python", "typescript")

CHILD_ENV_ALLOWLIST: tuple[str, ...] = (
    "KEI_RUNTIME_TOKEN",
    "KEI_RUNTIME_CONTROL_PLANE_URL",
    "KEI_HARNESS_KIND",
    "KEI_HARNESS_VERSION",
    "KEI_DEPLOYMENT_ENV",
    "KEI_AGENTWARE_SDK_LANG",
    "KEI_AGENTWARE_SDK_VERSION",
    "KEI_HEARTBEAT_RUN_ID",
    "PATH",
    "HOME",
    "TZ",
)
"""The exact environment a runtime child may receive; never ``os.environ``."""

# ---------------------------------------------------------------------------
# Environment variables (spec §3.2). KEI_PROXY_* keep their legacy names
# because they are the runtime's contract.
# ---------------------------------------------------------------------------

ENV_ENABLED = "KEI_RUNTIME_ENABLED"
ENV_TOKEN = "KEI_RUNTIME_TOKEN"
ENV_LEGACY_TOKEN = "KEI_HARNESS_TOKEN"
ENV_CONTROL_PLANE_URL = "KEI_RUNTIME_CONTROL_PLANE_URL"
ENV_PROXY_PATH = "KEI_PROXY_PATH"
ENV_PROXY_SHA = "KEI_PROXY_SHA"
ENV_INTERVAL = "KEI_HEARTBEAT_INTERVAL"
ENV_TIMEOUT = "KEI_HEARTBEAT_TIMEOUT"
ENV_RESTART_MIN = "KEI_HEARTBEAT_RESTART_MIN"
ENV_RESTART_MAX = "KEI_HEARTBEAT_RESTART_MAX"
ENV_STABLE_SECONDS = "KEI_HEARTBEAT_STABLE_SECONDS"
ENV_LOG_COUNT = "KEI_HEARTBEAT_LOG_COUNT"
ENV_HARNESS_KIND = "KEI_HARNESS_KIND"
ENV_HARNESS_VERSION = "KEI_HARNESS_VERSION"
ENV_DEPLOYMENT_ENV = "KEI_DEPLOYMENT_ENV"

DEFAULT_BINARY_PATH = "kei-proxy"
"""The distribution keeps the kei-proxy name; the repo is kei-connector-runtime."""

# Bounds (seconds). Interval is clamped; everything else is rejected when out
# of range so a typo fails closed instead of silently changing behavior.
MIN_INTERVAL = 15.0
MAX_INTERVAL = 300.0
MAX_GRACE = 300.0
MIN_RESTART = 1.0
MAX_RESTART = 3600.0
MIN_STABLE_RESET = 1.0
MAX_STABLE_RESET = 3600.0
MIN_STOP_TIMEOUT = 1.0
MAX_STOP_TIMEOUT = 60.0
MAX_LOG_COUNT = 100
_MAX_ENV_INT_DIGITS = 9

# Config error codes, shared through enums.json.
CODE_INVALID_VALUE = "invalid_value"
CODE_OUT_OF_RANGE = "out_of_range"
CODE_BEAT_TIMEOUT = "beat_timeout"
CODE_HARNESS_KIND = "harness_kind"
CODE_ENVELOPE = "envelope"
CODE_LEGACY_TOKEN = "legacy_token"
CODE_TOKEN_MISSING = "token_missing"
CODE_CONTROL_PLANE_URL = "control_plane_url"
CODE_BINARY = "binary"

CONFIG_ERROR_CODES: tuple[str, ...] = (
    CODE_INVALID_VALUE,
    CODE_OUT_OF_RANGE,
    CODE_BEAT_TIMEOUT,
    CODE_HARNESS_KIND,
    CODE_ENVELOPE,
    CODE_LEGACY_TOKEN,
    CODE_TOKEN_MISSING,
    CODE_CONTROL_PLANE_URL,
    CODE_BINARY,
)

_VERSION_RE = re.compile(r"^[A-Za-z0-9._+-]{1,64}$")
_ENV_NAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,32}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_REASON_RE = re.compile(r"^[a-z0-9_]{1,64}$")
_TIMESTAMP_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(\.\d{1,9})?(Z|[+-]\d{2}:\d{2})$"
)


class RuntimeLinkConfigError(ValueError):
    """Invalid RuntimeLink configuration.

    Carries the offending field and a stable ``code``, never the offending
    value, so a misplaced secret cannot leak through an error.
    """

    def __init__(self, code: str, field_name: str) -> None:
        super().__init__(f"runtime_link: invalid config: {field_name}: {code}")
        self.code = code
        self.field = field_name


@dataclass(frozen=True)
class BinaryRef:
    """The runtime binary and its expected SHA-256 (lowercase hex)."""

    path: str = DEFAULT_BINARY_PATH
    sha256: str = ""


@dataclass(frozen=True)
class SecretSource:
    """Where the runtime token lives: the env var name only, never the value."""

    env: str = ENV_TOKEN


@dataclass(frozen=True)
class Backoff:
    """Child restart backoff with full jitter. Durations are in seconds."""

    min: float = 1.0
    max: float = 300.0
    stable_reset: float = 300.0

    def delay_ms(self, attempt: int, r: float) -> int:
        """``floor(r × min(max, min × 2^attempt))`` in whole milliseconds.

        ``r`` is clamped to ``[0, 1)``; negative attempts count as 0.
        """
        attempt = min(max(attempt, 0), 30)
        cap_ms = min(round(self.max * 1000), round(self.min * 1000) << attempt)
        r = min(max(r, 0.0), math.nextafter(1.0, 0.0))
        return math.floor(r * cap_ms)

    def delay(self, attempt: int, r: float) -> float:
        """The same delay in seconds."""
        return self.delay_ms(attempt, r) / 1000

    async def wait(self, attempt: int, r: float | None = None) -> None:
        """Sleep for the jittered delay.

        Cancellation (``task.cancel()``) propagates ``asyncio.CancelledError``
        immediately, so a stop during backoff never waits out the delay.
        """
        await asyncio.sleep(self.delay(attempt, random.random() if r is None else r))


@dataclass(frozen=True)
class HarnessEnvelope:
    """Declared, non-authoritative harness metadata. Never used for authorization."""

    kind: str = ""
    version: str = ""
    deployment_env: str = ""


@dataclass(frozen=True)
class RuntimeLinkConfig:
    """RuntimeLink configuration. Durations are in seconds.

    Start from the defaults or :func:`config_from_env`;
    :func:`normalize_config` validates and clamps it.
    """

    enabled: bool = False
    binary: BinaryRef = field(default_factory=BinaryRef)
    control_plane_url: str = ""
    token: SecretSource = field(default_factory=SecretSource)
    interval: float = 60.0
    beat_timeout: float = 10.0
    grace: float = 15.0
    restart: Backoff = field(default_factory=Backoff)
    stop_timeout: float = 10.0
    log_count: int = 3
    harness: HarnessEnvelope = field(default_factory=HarnessEnvelope)


_HARNESS_KINDS = frozenset(k.value for k in HarnessKind)


def normalize_config(cfg: RuntimeLinkConfig) -> RuntimeLinkConfig:
    """Clamp ``interval`` to [15s, 300s] and validate every other field.

    Raises :class:`RuntimeLinkConfigError` on the first violation. A disabled
    link (local-only mode) is valid, but its declared envelope and timings are
    still checked so typos fail loudly.
    """
    h = cfg.harness
    if (h.kind and h.kind not in _HARNESS_KINDS) or (cfg.enabled and not h.kind):
        raise RuntimeLinkConfigError(CODE_HARNESS_KIND, "harness.kind")
    if h.version and not _VERSION_RE.match(h.version):
        raise RuntimeLinkConfigError(CODE_ENVELOPE, "harness.version")
    if h.deployment_env and not _ENV_NAME_RE.match(h.deployment_env):
        raise RuntimeLinkConfigError(CODE_ENVELOPE, "harness.deployment_env")

    if cfg.enabled:
        if cfg.token.env == ENV_LEGACY_TOKEN:
            raise RuntimeLinkConfigError(CODE_LEGACY_TOKEN, "token")
        if cfg.token.env != ENV_TOKEN:
            raise RuntimeLinkConfigError(CODE_INVALID_VALUE, "token")
        _validate_control_plane_url(cfg.control_plane_url)
        if not cfg.binary.path or "\x00" in cfg.binary.path:
            raise RuntimeLinkConfigError(CODE_BINARY, "binary.path")
        if not cfg.binary.sha256 and h.deployment_env == "prod":
            raise RuntimeLinkConfigError(CODE_BINARY, "binary.sha256")
    if cfg.binary.sha256 and not _SHA256_RE.match(cfg.binary.sha256):
        raise RuntimeLinkConfigError(CODE_BINARY, "binary.sha256")

    interval = min(max(cfg.interval, MIN_INTERVAL), MAX_INTERVAL)
    if cfg.beat_timeout <= 0 or 2 * cfg.beat_timeout >= interval:
        raise RuntimeLinkConfigError(CODE_BEAT_TIMEOUT, "beat_timeout")
    if not 0 <= cfg.grace <= MAX_GRACE:
        raise RuntimeLinkConfigError(CODE_OUT_OF_RANGE, "grace")
    r = cfg.restart
    if r.min < MIN_RESTART or r.max > MAX_RESTART or r.min > r.max:
        raise RuntimeLinkConfigError(CODE_OUT_OF_RANGE, "restart")
    if not MIN_STABLE_RESET <= r.stable_reset <= MAX_STABLE_RESET:
        raise RuntimeLinkConfigError(CODE_OUT_OF_RANGE, "restart.stable_reset")
    if not MIN_STOP_TIMEOUT <= cfg.stop_timeout <= MAX_STOP_TIMEOUT:
        raise RuntimeLinkConfigError(CODE_OUT_OF_RANGE, "stop_timeout")
    if not 0 <= cfg.log_count <= MAX_LOG_COUNT:
        raise RuntimeLinkConfigError(CODE_OUT_OF_RANGE, "log_count")
    return replace(cfg, interval=interval)


def _validate_control_plane_url(raw: str) -> None:
    try:
        u = urlsplit(raw)
        ok = u.scheme in ("http", "https") and bool(u.hostname) and "@" not in u.netloc
    except ValueError:
        ok = False
    if not ok:
        raise RuntimeLinkConfigError(CODE_CONTROL_PLANE_URL, "control_plane_url")


def _parse_env_int(name: str, raw: str) -> int:
    if len(raw) > _MAX_ENV_INT_DIGITS or not raw.isascii() or not raw.isdigit():
        raise RuntimeLinkConfigError(CODE_INVALID_VALUE, name)
    return int(raw)


def _parse_env_bool(name: str, raw: str) -> bool:
    value = raw.lower()
    if value in ("true", "1"):
        return True
    if value in ("false", "0"):
        return False
    raise RuntimeLinkConfigError(CODE_INVALID_VALUE, name)


def config_from_env(
    env: Mapping[str, str] | None = None,
    harness: HarnessEnvelope | None = None,
) -> RuntimeLinkConfig:
    """Build a normalized config from the spec §3.2 variables.

    Non-empty fields of ``harness`` override the ``KEI_HARNESS_*`` variables.
    The runtime token is only checked for presence; its value is never
    retained. ``env`` defaults to ``os.environ``.
    """
    source: Mapping[str, str] = os.environ if env is None else env
    harness = harness or HarnessEnvelope()

    def get(key: str) -> str:
        return source.get(key, "").strip()

    seconds: dict[str, float] = {}
    for name in (ENV_INTERVAL, ENV_TIMEOUT, ENV_RESTART_MIN, ENV_RESTART_MAX, ENV_STABLE_SECONDS):
        raw = get(name)
        if raw:
            seconds[name] = float(_parse_env_int(name, raw))
    log_count = 3
    if raw := get(ENV_LOG_COUNT):
        log_count = _parse_env_int(ENV_LOG_COUNT, raw)

    has_token = bool(get(ENV_TOKEN))
    if not has_token and get(ENV_LEGACY_TOKEN):
        raise RuntimeLinkConfigError(CODE_LEGACY_TOKEN, ENV_LEGACY_TOKEN)
    enabled = has_token
    if raw := get(ENV_ENABLED):
        enabled = _parse_env_bool(ENV_ENABLED, raw)
        if enabled and not has_token:
            raise RuntimeLinkConfigError(CODE_TOKEN_MISSING, ENV_TOKEN)

    defaults = RuntimeLinkConfig()
    cfg = RuntimeLinkConfig(
        enabled=enabled,
        binary=BinaryRef(
            path=get(ENV_PROXY_PATH) or DEFAULT_BINARY_PATH, sha256=get(ENV_PROXY_SHA)
        ),
        control_plane_url=get(ENV_CONTROL_PLANE_URL),
        interval=seconds.get(ENV_INTERVAL, defaults.interval),
        beat_timeout=seconds.get(ENV_TIMEOUT, defaults.beat_timeout),
        restart=Backoff(
            min=seconds.get(ENV_RESTART_MIN, defaults.restart.min),
            max=seconds.get(ENV_RESTART_MAX, defaults.restart.max),
            stable_reset=seconds.get(ENV_STABLE_SECONDS, defaults.restart.stable_reset),
        ),
        log_count=log_count,
        harness=HarnessEnvelope(
            kind=harness.kind or get(ENV_HARNESS_KIND),
            version=harness.version or get(ENV_HARNESS_VERSION),
            deployment_env=harness.deployment_env or get(ENV_DEPLOYMENT_ENV),
        ),
    )
    return normalize_config(cfg)


# ---------------------------------------------------------------------------
# Status and identity
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LinkStatus:
    """Point-in-time RuntimeLink snapshot.

    Diagnostic only: harness readiness must never depend on it (spec §6.3).
    """

    state: LinkState
    reason: FailureClass | None = None
    last_beat_at: datetime | None = None
    last_catalog_ok_at: datetime | None = None
    consecutive_fails: int = 0
    run_id: str = ""
    restarts: int = 0


@dataclass(frozen=True)
class RuntimeIdentity:
    """Authoritative installation identity the runtime reports from catalog whoami.

    Declared harness config never overrides it.
    """

    run_id: str
    installation_id: str
    org_id: str
    workspace_id: str = ""
    platform: str = ""
    status: str = ""
    binding_status: str = ""
    runtime_version: str = ""


# ---------------------------------------------------------------------------
# Child stdout wire events (spec §4.1)
# ---------------------------------------------------------------------------

_MAX_SEQ = 2**53 - 1
_MAX_LATENCY_MS = 3_600_000
_MAX_NEXT_IN_MS = 3_600_000


class ChildLineError(ValueError):
    """Base for child lines the SDK must drop and count."""


class LineTooLongError(ChildLineError):
    """The line exceeds :data:`MAX_CHILD_LINE_BYTES`."""


class MalformedLineError(ChildLineError):
    """Not a JSON object, or a known event with a bad field."""


class ContractMismatchError(ChildLineError):
    """Unknown ``v`` or an outcome outside the closed set."""


@dataclass(frozen=True)
class BeatEvent:
    """One runtime→catalog heartbeat result as the runtime reports it."""

    run_id: str
    seq: int  # 0 when absent: the runtime omits seq on beats sent before identity
    at: str
    outcome: BeatOutcome
    http_status: int = 0
    latency_ms: int = 0
    next_in_ms: int = 0


@dataclass(frozen=True)
class TerminalEvent:
    """The runtime is about to exit for good."""

    run_id: str
    reason: str


@dataclass(frozen=True)
class IgnoredEvent:
    """A well-formed v1 line with an event name this SDK does not know."""


ChildEvent = RuntimeIdentity | BeatEvent | TerminalEvent | IgnoredEvent


class _Fields:
    """Extracts typed, bounded fields; only allowlisted keys are ever read."""

    def __init__(self, raw: dict[str, Any]) -> None:
        self._raw = raw

    def text(self, key: str, required: bool) -> str:
        if key not in self._raw:
            if required:
                raise MalformedLineError(key)
            return ""
        value = self._raw[key]
        if not isinstance(value, str):
            raise MalformedLineError(key)
        return value

    def ident(self, key: str, required: bool) -> str:
        value = self.text(key, required)
        if (value or required) and not _ID_RE.match(value):
            raise MalformedLineError(key)
        return value

    def timestamp(self, key: str) -> str:
        value = self.text(key, True)
        if not _TIMESTAMP_RE.match(value):
            raise MalformedLineError(key)
        return value

    def integer(self, key: str, required: bool, lo: int, hi: int) -> int:
        if key not in self._raw:
            if required:
                raise MalformedLineError(key)
            return 0
        value = self._raw[key]
        if type(value) is not int or not lo <= value <= hi:
            raise MalformedLineError(key)
        return value


def parse_child_line(line: str | bytes) -> ChildEvent:
    """Parse one line of child stdout (a trailing ``\\n`` / ``\\r\\n`` is ignored).

    Only allowlisted fields survive; payloads, results, reasoning, tokens, or
    any other key the runtime might emit are dropped. Raises
    :class:`LineTooLongError`, :class:`ContractMismatchError`, or
    :class:`MalformedLineError` for lines the SDK must drop and count.
    """
    data = line.encode() if isinstance(line, str) else line
    data = data.removesuffix(b"\n").removesuffix(b"\r")
    if len(data) > MAX_CHILD_LINE_BYTES:
        raise LineTooLongError("line too long")
    try:
        raw = json.loads(data)
    except ValueError as exc:
        raise MalformedLineError("not json") from exc
    if not isinstance(raw, dict):
        raise MalformedLineError("not an object")
    v = raw.get("v")
    if type(v) is not int or v != CONTRACT_VERSION:
        raise ContractMismatchError("v")
    event = raw.get("event")
    if not isinstance(event, str):
        raise MalformedLineError("event")

    f = _Fields(raw)
    if event == "identity":
        return RuntimeIdentity(
            run_id=f.ident("run_id", True),
            installation_id=f.ident("installation_id", True),
            org_id=f.ident("org_id", True),
            workspace_id=f.ident("workspace_id", False),
            platform=f.ident("platform", False),
            status=f.ident("status", False),
            binding_status=f.ident("binding_status", False),
            runtime_version=f.ident("runtime_version", False),
        )
    if event == "beat":
        run_id = f.ident("run_id", True)
        seq = f.integer("seq", False, 0, _MAX_SEQ)
        at = f.timestamp("at")
        outcome = f.text("outcome", True)
        latency_ms = f.integer("latency_ms", False, 0, _MAX_LATENCY_MS)
        next_in_ms = f.integer("next_in_ms", False, 0, _MAX_NEXT_IN_MS)
        http_status = f.integer("http_status", False, 0, 599)
        if 0 < http_status < 100:
            raise MalformedLineError("http_status")
        try:
            beat_outcome = BeatOutcome(outcome)
        except ValueError as exc:
            raise ContractMismatchError("outcome") from exc
        return BeatEvent(run_id, seq, at, beat_outcome, http_status, latency_ms, next_in_ms)
    if event == "terminal":
        run_id = f.ident("run_id", True)
        reason = f.text("reason", True)
        if not _REASON_RE.match(reason):
            raise MalformedLineError("reason")
        return TerminalEvent(run_id, reason)
    return IgnoredEvent()


# ---------------------------------------------------------------------------
# Redacted lifecycle events (spec §5.3)
# ---------------------------------------------------------------------------


class InvalidLinkEventError(ValueError):
    """A lifecycle event has a value outside its closed set or bounds."""


@dataclass(frozen=True)
class EventHarness:
    """The declared envelope carried on every lifecycle event."""

    kind: str
    sdk_lang: str
    version: str = ""
    deployment_env: str = ""
    sdk_version: str = ""


@dataclass(frozen=True)
class LinkEvent:
    """A redacted lifecycle record.

    Redacted by construction: the type has no field that could carry a token,
    argv, env, child stderr, provider payload or result, reasoning, customer
    content, or a subject. :meth:`from_dict` drops every key outside the
    allowlist. ``at`` must be timezone-aware.
    """

    name: LifecycleEventName
    at: datetime
    state: LinkState
    sdk_instance_id: str
    harness: EventHarness
    reason: FailureClass | None = None
    run_id: str = ""
    seq: int = 0
    restarts: int = 0
    consecutive_fails: int = 0

    def __post_init__(self) -> None:
        h = self.harness
        checks = (
            isinstance(self.name, LifecycleEventName),
            isinstance(self.state, LinkState),
            self.reason is None or isinstance(self.reason, FailureClass),
            isinstance(self.at, datetime) and self.at.tzinfo is not None,
            not self.run_id or bool(_ID_RE.match(self.run_id)),
            bool(_ID_RE.match(self.sdk_instance_id)),
            h.kind in _HARNESS_KINDS,
            h.sdk_lang in SDK_LANGS,
            not h.version or bool(_VERSION_RE.match(h.version)),
            not h.deployment_env or bool(_ENV_NAME_RE.match(h.deployment_env)),
            not h.sdk_version or bool(_VERSION_RE.match(h.sdk_version)),
            _is_count(self.seq, _MAX_SEQ),
            _is_count(self.restarts, _MAX_SEQ),
            _is_count(self.consecutive_fails, _MAX_SEQ),
        )
        if not all(checks):
            raise InvalidLinkEventError("invalid lifecycle event")

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> LinkEvent:
        """Build an event from allowlisted keys only; everything else is dropped."""
        try:
            h = raw.get("harness") or {}
            reason = raw.get("reason") or None
            return cls(
                name=LifecycleEventName(raw.get("name")),
                at=_parse_timestamp(raw.get("at")),
                state=LinkState(raw.get("state")),
                reason=FailureClass(reason) if reason is not None else None,
                run_id=_as_str(raw.get("run_id", "")),
                seq=raw.get("seq", 0),
                sdk_instance_id=_as_str(raw.get("sdk_instance_id", "")),
                harness=EventHarness(
                    kind=_as_str(h.get("kind", "")),
                    sdk_lang=_as_str(h.get("sdk_lang", "")),
                    version=_as_str(h.get("version", "")),
                    deployment_env=_as_str(h.get("deployment_env", "")),
                    sdk_version=_as_str(h.get("sdk_version", "")),
                ),
                restarts=raw.get("restarts", 0),
                consecutive_fails=raw.get("consecutive_fails", 0),
            )
        except (ValueError, TypeError, AttributeError) as exc:
            raise InvalidLinkEventError("invalid lifecycle event") from exc

    def to_dict(self) -> dict[str, Any]:
        """Exactly the allowlisted keys, in contract order."""
        h = self.harness
        return {
            "name": self.name.value,
            "at": _format_timestamp(self.at),
            "state": self.state.value,
            "reason": self.reason.value if self.reason else "",
            "run_id": self.run_id,
            "seq": self.seq,
            "sdk_instance_id": self.sdk_instance_id,
            "harness": {
                "kind": h.kind,
                "version": h.version,
                "deployment_env": h.deployment_env,
                "sdk_lang": h.sdk_lang,
                "sdk_version": h.sdk_version,
            },
            "restarts": self.restarts,
            "consecutive_fails": self.consecutive_fails,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), separators=(",", ":"))


def _is_count(value: object, hi: int) -> bool:
    return type(value) is int and 0 <= value <= hi


def _as_str(value: object) -> str:
    if not isinstance(value, str):
        raise TypeError("expected string")
    return value


def _parse_timestamp(value: object) -> datetime:
    """Parse the shared RFC 3339 subset; fractions beyond microseconds truncate."""
    m = _TIMESTAMP_RE.match(value) if isinstance(value, str) else None
    if m is None:
        raise ValueError("timestamp")
    base, frac, zone = m.groups()
    micros = int(((frac or ".")[1:] + "000000")[:6])
    tz = timezone.utc
    if zone != "Z":
        sign = -1 if zone[0] == "-" else 1
        tz = timezone(sign * timedelta(hours=int(zone[1:3]), minutes=int(zone[4:6])))
    return datetime.strptime(base, "%Y-%m-%dT%H:%M:%S").replace(microsecond=micros, tzinfo=tz)


def _format_timestamp(at: datetime) -> str:
    """UTC, millisecond precision, ``Z`` suffix: the one wire format in every SDK."""
    utc = at.astimezone(timezone.utc)
    return utc.strftime("%Y-%m-%dT%H:%M:%S.") + f"{utc.microsecond // 1000:03d}Z"


# ---------------------------------------------------------------------------
# RuntimeLink interface (implementation arrives in a later slice)
# ---------------------------------------------------------------------------


class RuntimeLink(Protocol):
    """Supervises the harness→runtime heartbeat.

    ``start`` returns promptly and raises only for invalid config, never for
    network or runtime state. Cancelling the task that owns the link, or
    ``stop``, ends it; ``stop`` is idempotent and bounded by
    ``RuntimeLinkConfig.stop_timeout``.
    """

    async def start(self) -> None: ...

    async def stop(self) -> None: ...

    def status(self) -> LinkStatus: ...

    def identity(self) -> RuntimeIdentity | None: ...

    def events(self) -> AsyncIterator[LinkEvent]: ...
