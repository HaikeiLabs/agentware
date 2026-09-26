"""Contract tests for the RuntimeLink types (shared fixtures in testing/contracts/runtime-link)."""

from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from pedro_agentware.kei import runtime_link as rl

CONTRACTS = Path(__file__).resolve().parents[3] / "testing" / "contracts" / "runtime-link"


def load(name: str) -> Any:
    return json.loads((CONTRACTS / name).read_text())


ENUMS = load("enums.json")
CONFIG = load("config-cases.json")
BACKOFF = load("backoff-cases.json")
WIRE = load("wire/child-events.json")
REDACTION = load("lifecycle-redaction.json")
EXIT_CODE_CASES = load("exit-code-cases.json")
CHILD_ENV_ALLOWLIST = load("child-env-allowlist.json")


def test_enums_match_contract() -> None:
    assert rl.CONTRACT_VERSION == ENUMS["contract_version"]
    assert [k.value for k in rl.HarnessKind] == ENUMS["harness_kinds"]
    assert [s.value for s in rl.LinkState] == ENUMS["link_states"]
    assert [c.value for c in rl.FailureClass] == ENUMS["failure_classes"]
    assert [o.value for o in rl.BeatOutcome] == ENUMS["beat_outcomes"]
    assert [e.value for e in rl.LifecycleEventName] == ENUMS["lifecycle_events"]
    assert list(rl.SDK_LANGS) == ENUMS["sdk_langs"]
    assert list(rl.CONFIG_ERROR_CODES) == ENUMS["config_error_codes"]
    assert list(rl.CHILD_ENV_ALLOWLIST) == ENUMS["child_env_allowlist"]
    assert rl.MAX_CHILD_LINE_BYTES == ENUMS["max_child_line_bytes"]
    assert rl.EVENT_BUFFER_SIZE == ENUMS["event_buffer_size"]


def to_contract(cfg: rl.RuntimeLinkConfig) -> dict[str, Any]:
    return {
        "enabled": cfg.enabled,
        "token_env": cfg.token.env,
        "control_plane_url": cfg.control_plane_url,
        "binary_path": cfg.binary.path,
        "binary_sha256": cfg.binary.sha256,
        "interval_s": int(cfg.interval),
        "beat_timeout_s": int(cfg.beat_timeout),
        "grace_s": int(cfg.grace),
        "restart_min_s": int(cfg.restart.min),
        "restart_max_s": int(cfg.restart.max),
        "stable_reset_s": int(cfg.restart.stable_reset),
        "stop_timeout_s": int(cfg.stop_timeout),
        "log_count": cfg.log_count,
        "harness": asdict(cfg.harness),
    }


@pytest.mark.parametrize("case", CONFIG["cases"], ids=lambda c: c["name"])
def test_config_from_env_contract(case: dict[str, Any]) -> None:
    harness = rl.HarnessEnvelope(**case["harness"]) if "harness" in case else None
    expect = case["expect"]
    if not expect["ok"]:
        with pytest.raises(rl.RuntimeLinkConfigError) as exc:
            rl.config_from_env(case["env"], harness)
        assert exc.value.code == expect["error"]
        assert CONFIG["token_canary"] not in str(exc.value)
        return
    cfg = rl.config_from_env(case["env"], harness)
    assert to_contract(cfg) == expect["config"]
    assert CONFIG["token_canary"] not in repr(cfg)


def test_config_from_env_defaults_to_process_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "KEI_RUNTIME_TOKEN",
        "KEI_HARNESS_TOKEN",
        "KEI_RUNTIME_ENABLED",
        "KEI_HARNESS_KIND",
    ):
        monkeypatch.delenv(name, raising=False)
    assert rl.config_from_env().enabled is False


BASE = rl.RuntimeLinkConfig(
    enabled=True,
    control_plane_url="http://localhost:8080",
    harness=rl.HarnessEnvelope(kind="cli"),
)


@pytest.mark.parametrize(
    ("changes", "code"),
    [
        ({"harness": rl.HarnessEnvelope(kind="telegram")}, "harness_kind"),
        ({"token": rl.SecretSource(env="KEI_HARNESS_TOKEN")}, "legacy_token"),
        ({"token": rl.SecretSource(env="DISCORD_TOKEN")}, "invalid_value"),
        ({"interval": 20.0, "beat_timeout": 10.0}, "beat_timeout"),
        ({"grace": -1.0}, "out_of_range"),
        ({"grace": rl.MAX_GRACE + 1}, "out_of_range"),
        ({"stop_timeout": 0.0}, "out_of_range"),
        ({"stop_timeout": 120.0}, "out_of_range"),
        ({"restart": rl.Backoff(stable_reset=0.0)}, "out_of_range"),
        ({"binary": rl.BinaryRef(path="")}, "binary"),
        ({"control_plane_url": "https://"}, "control_plane_url"),
    ],
)
def test_normalize_config_direct(changes: dict[str, Any], code: str) -> None:
    rl.normalize_config(BASE)
    with pytest.raises(rl.RuntimeLinkConfigError) as exc:
        rl.normalize_config(replace(BASE, **changes))
    assert exc.value.code == code


def test_disabled_config_clamps_interval() -> None:
    cfg = rl.normalize_config(rl.RuntimeLinkConfig(interval=0.0, beat_timeout=5.0))
    assert cfg.interval == rl.MIN_INTERVAL


@pytest.mark.parametrize("case", BACKOFF["cases"], ids=lambda c: f"{c['attempt']}-{c['rand']}")
def test_backoff_contract(case: dict[str, Any]) -> None:
    b = rl.Backoff(min=case["min_ms"] / 1000, max=case["max_ms"] / 1000)
    assert b.delay_ms(case["attempt"], case["rand"]) == case["delay_ms"]


async def test_backoff_wait_cancellation() -> None:
    b = rl.Backoff(min=3600.0, max=3600.0)
    task = asyncio.create_task(b.wait(0, 0.5))
    await asyncio.sleep(0.02)
    start = time.monotonic()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert time.monotonic() - start < 1.0

    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(b.wait(0, 0.5), timeout=0.01)

    await rl.Backoff(min=1.0, max=1.0).wait(0, 0.001)


def _child_kind(line: str) -> tuple[str, Any]:
    try:
        ev = rl.parse_child_line(line)
    except rl.LineTooLongError:
        return "line_too_long", None
    except rl.ContractMismatchError:
        return "contract_mismatch", None
    except rl.MalformedLineError:
        return "malformed", None
    if isinstance(ev, rl.RuntimeIdentity):
        fields = asdict(ev)
        fields["agent_id"] = fields.pop("default_agent_id") or ""
        fields["agents"] = list(fields["agents"])
        return "identity", fields
    if isinstance(ev, rl.BeatEvent):
        fields = asdict(ev)
        fields["outcome"] = ev.outcome.value
        return "beat", fields
    if isinstance(ev, rl.TerminalEvent):
        return "terminal", asdict(ev)
    return "ignored", None


@pytest.mark.parametrize("case", WIRE["cases"], ids=lambda c: c["name"])
def test_parse_child_line_contract(case: dict[str, Any]) -> None:
    kind, fields = _child_kind(case["line"])
    assert kind == case["expect"]["kind"]
    if "fields" in case["expect"]:
        assert fields == case["expect"]["fields"]
    if kind == "identity":
        _, dropped = rl.parse_child_line_counted(case["line"])
        assert dropped == case["expect"].get("dropped_agents", 0)


_IDENTITY_BASE = {
    "v": 1,
    "event": "identity",
    "run_id": "r1",
    "installation_id": "i1",
    "org_id": "o1",
}


def _parse_identity(**extra: Any) -> tuple[rl.RuntimeIdentity, int]:
    ev, dropped = rl.parse_child_line_counted(json.dumps({**_IDENTITY_BASE, **extra}))
    assert isinstance(ev, rl.RuntimeIdentity)
    return ev, dropped


def test_identity_agents_present() -> None:
    ident, dropped = _parse_identity(
        agent_id="agent_sales",
        agents=[
            {"agent_id": "agent_sales", "is_default": True},
            {"agent_id": "agent_support", "is_default": False},
        ],
    )
    assert ident.default_agent_id == "agent_sales"
    assert ident.agents == (
        rl.AssignedAgent("agent_sales", True),
        rl.AssignedAgent("agent_support", False),
    )
    assert dropped == 0


def test_identity_agents_absent_old_runtime() -> None:
    ident, dropped = _parse_identity()
    assert ident.default_agent_id is None
    assert ident.agents == ()
    assert dropped == 0


def test_identity_agents_malformed_entry_dropped_and_counted() -> None:
    ident, dropped = _parse_identity(
        agents=[
            {"agent_id": "bad id!", "is_default": True},
            {"agent_id": "agent_b", "is_default": True},
        ]
    )
    assert ident.default_agent_id == "agent_b"
    assert ident.agents == (rl.AssignedAgent("agent_b", True),)
    assert dropped == 1


async def test_identity_dropped_agents_counted_as_fails() -> None:
    link = _make_link()
    async with link._lock:
        link._transition_locked(rl.LinkState.STARTING, None)
    ident, dropped = _parse_identity(agents=[{"agent_id": "a1", "is_default": True}, 7, "x"])
    await link._handle_identity(ident, dropped)
    st = link.status()
    assert st.state == rl.LinkState.CONNECTED
    assert st.consecutive_fails == 2
    got = link.identity()
    assert got is not None and got.default_agent_id == "a1"


def pad_line(n: int) -> str:
    head, tail = '{"v":1,"event":"pad","pad":"', '"}'
    return head + "x" * (n - len(head) - len(tail)) + tail


@pytest.mark.parametrize("case", WIRE["synthesized"], ids=lambda c: c["name"])
def test_parse_child_line_size_bound(case: dict[str, Any]) -> None:
    line = pad_line(case["bytes"])
    assert len(line.encode()) == case["bytes"]
    assert _child_kind(line + "\n")[0] == case["expect"]["kind"]


@pytest.mark.parametrize("case", REDACTION["cases"], ids=lambda c: c["name"])
def test_lifecycle_redaction_contract(case: dict[str, Any]) -> None:
    if not case["expect"]["ok"]:
        with pytest.raises(rl.InvalidLinkEventError):
            rl.LinkEvent.from_dict(case["input"])
        return
    ev = rl.LinkEvent.from_dict(case["input"])
    out = ev.to_json()
    for canary in REDACTION["canaries"]:
        assert canary not in out
    decoded = json.loads(out)
    assert decoded == case["expect"]["serialized"]
    assert list(decoded) == REDACTION["record_keys"]
    assert sorted(decoded["harness"]) == sorted(REDACTION["harness_keys"])


def test_exit_code_mapping_fixture() -> None:
    valid = {c.value for c in rl.FailureClass}
    for case in EXIT_CODE_CASES["cases"]:
        fc = case["failure_class"]
        if fc:
            assert fc in valid, f"unknown failure_class {fc!r} in fixture"
    # TODO: Add behavioral assertions when handleChildExit is implemented.


def test_child_env_allowlist_fixture() -> None:
    fx = CHILD_ENV_ALLOWLIST
    assert sorted(fx["allowlist"]) == sorted(rl.CHILD_ENV_ALLOWLIST)
    assert "canaries" in fx
    assert "sdk_injected" in fx
    assert set(fx["sdk_injected"]).issubset(fx["allowlist"])
    assert fx["env_count_max"] == len(rl.CHILD_ENV_ALLOWLIST) + 2
    # TODO: Add behavioral assertions when buildCommand is implemented.


def test_link_event_rejects_invalid_direct_construction() -> None:
    harness = rl.EventHarness(kind="cli", sdk_lang=rl.SDK_LANG)
    with pytest.raises(rl.InvalidLinkEventError):
        rl.LinkEvent(
            name=rl.LifecycleEventName.STARTED,
            at=datetime(2026, 9, 23, 16, 0),  # naive
            state=rl.LinkState.STARTING,
            sdk_instance_id="sdk-1",
            harness=harness,
        )
    ev = rl.LinkEvent(
        name=rl.LifecycleEventName.STARTED,
        at=datetime(2026, 9, 23, 18, 0, tzinfo=timezone(timedelta(hours=2))),
        state=rl.LinkState.STARTING,
        sdk_instance_id="sdk-1",
        harness=harness,
    )
    assert ev.to_dict()["at"] == "2026-09-23T16:00:00.000Z"
    with pytest.raises(rl.InvalidLinkEventError):
        replace(ev, harness=rl.EventHarness(kind="telegram", sdk_lang="python"))


# ---------------------------------------------------------------------------
# Link unit tests (no subprocess)
# ---------------------------------------------------------------------------


def _make_link(
    enabled: bool = True,
    harness_kind: str = "cli",
    **kw: Any,
) -> rl.Link:
    """Create a Link in testable state (no supervisor task running)."""
    cfg = rl.RuntimeLinkConfig(
        enabled=enabled,
        control_plane_url="http://localhost:8080",
        harness=rl.HarnessEnvelope(kind=harness_kind, version="1.0.0"),
        binary=rl.BinaryRef(path="/test/binary"),
        **kw,
    )
    link = rl.Link(cfg)
    link._sdk_id = "sdk-test-1"
    return link


async def test_new_disabled() -> None:
    link = _make_link(enabled=False)
    st = link.status()
    assert st.state == rl.LinkState.DISABLED
    assert link.identity() is None

    await link.start()
    st = link.status()
    assert st.state == rl.LinkState.DISABLED

    await link.stop()
    st = link.status()
    assert st.state == rl.LinkState.STOPPED


async def test_new_link_enabled_starts_as_disabled() -> None:
    link = _make_link(enabled=True)
    st = link.status()
    assert st.state == rl.LinkState.DISABLED
    assert link.identity() is None


async def test_start_stops_immediately_without_config_error() -> None:
    link = _make_link(enabled=False)
    await link.start()
    await link.stop()


async def test_start_idempotent() -> None:
    link = _make_link(enabled=False)
    await link.start()
    await link.start()  # second call should be no-op
    await link.stop()
    await link.stop()  # second call should be no-op


async def test_status_lock_free_read() -> None:
    """Status should return immediately without waiting for the lock."""
    link = _make_link()
    st = link.status()
    assert isinstance(st, rl.LinkStatus)
    assert st.state == rl.LinkState.DISABLED


# ---------------------------------------------------------------------------
# State machine transitions (direct calls, no supervisor)
# ---------------------------------------------------------------------------


async def test_state_machine_identity_during_start() -> None:
    link = _make_link()
    async with link._lock:
        link._transition_locked(rl.LinkState.STARTING, None)

    await link._handle_identity(
        rl.RuntimeIdentity(
            run_id="run-1",
            installation_id="inst-1",
            org_id="org-1",
            workspace_id="ws-1",
        )
    )

    st = link.status()
    assert st.state == rl.LinkState.CONNECTED
    id_ = link.identity()
    assert id_ is not None
    assert id_.run_id == "run-1"


async def test_state_machine_identity_after_degraded() -> None:
    link = _make_link()
    async with link._lock:
        link._transition_locked(rl.LinkState.CONNECTED, None)
    await link._handle_beat(
        rl.BeatEvent(
            run_id="r1",
            seq=1,
            at="2026-09-23T18:00:00Z",
            outcome=rl.BeatOutcome.CATALOG_UNREACHABLE,
        )
    )
    st = link.status()
    assert st.state == rl.LinkState.DEGRADED

    await link._handle_identity(
        rl.RuntimeIdentity(
            run_id="run-2",
            installation_id="inst-2",
            org_id="org-2",
        )
    )
    st = link.status()
    assert st.state == rl.LinkState.DEGRADED  # stays degraded


async def test_state_machine_ok_beat_from_degraded() -> None:
    link = _make_link()
    async with link._lock:
        link._transition_locked(rl.LinkState.DEGRADED, rl.FailureClass.CATALOG_UNREACHABLE)

    await link._handle_beat(
        rl.BeatEvent(run_id="r1", seq=2, at="2026-09-23T18:01:00Z", outcome=rl.BeatOutcome.OK)
    )
    st = link.status()
    assert st.state == rl.LinkState.CONNECTED
    assert st.reason is None


async def test_state_machine_ok_beat_from_connected() -> None:
    link = _make_link()
    async with link._lock:
        link._transition_locked(rl.LinkState.CONNECTED, None)
    link._identity = rl.RuntimeIdentity(run_id="run-1", installation_id="i1", org_id="o1")

    await link._handle_beat(
        rl.BeatEvent(run_id="r1", seq=3, at="2026-09-23T18:02:00Z", outcome=rl.BeatOutcome.OK)
    )
    st = link.status()
    assert st.state == rl.LinkState.CONNECTED


async def test_state_machine_unauthorized_beat() -> None:
    link = _make_link()
    async with link._lock:
        link._transition_locked(rl.LinkState.CONNECTED, None)
    link._identity = rl.RuntimeIdentity(run_id="run-1", installation_id="i1", org_id="o1")

    await link._handle_beat(
        rl.BeatEvent(
            run_id="r1", seq=4, at="2026-09-23T18:03:00Z", outcome=rl.BeatOutcome.UNAUTHORIZED
        )
    )
    st = link.status()
    assert st.state == rl.LinkState.TERMINAL
    assert st.reason == rl.FailureClass.INSTALLATION_UNAUTHORIZED


async def test_state_machine_child_terminal() -> None:
    link = _make_link()
    async with link._lock:
        link._transition_locked(rl.LinkState.CONNECTED, None)

    await link._handle_terminal(rl.TerminalEvent(run_id="r1", reason="unauthorized"))
    st = link.status()
    assert st.state == rl.LinkState.TERMINAL
    assert st.reason == rl.FailureClass.INSTALLATION_UNAUTHORIZED


async def test_state_machine_child_terminal_contract() -> None:
    link = _make_link()
    async with link._lock:
        link._transition_locked(rl.LinkState.CONNECTED, None)

    await link._handle_terminal(rl.TerminalEvent(run_id="r1", reason="contract"))
    st = link.status()
    assert st.reason == rl.FailureClass.CONTRACT_MISMATCH


async def test_state_machine_child_terminal_config() -> None:
    link = _make_link()
    async with link._lock:
        link._transition_locked(rl.LinkState.CONNECTED, None)

    await link._handle_terminal(rl.TerminalEvent(run_id="r1", reason="config"))
    st = link.status()
    assert st.reason == rl.FailureClass.CONFIG_INVALID


async def test_state_machine_child_terminal_stale() -> None:
    link = _make_link()
    async with link._lock:
        link._transition_locked(rl.LinkState.CONNECTED, None)

    await link._handle_terminal(rl.TerminalEvent(run_id="r1", reason="stale"))
    st = link.status()
    assert st.reason == rl.FailureClass.INSTALLATION_STALE


async def test_state_machine_child_terminal_unknown() -> None:
    link = _make_link()
    async with link._lock:
        link._transition_locked(rl.LinkState.CONNECTED, None)

    await link._handle_terminal(rl.TerminalEvent(run_id="r1", reason="something_else"))
    st = link.status()
    assert st.reason == rl.FailureClass.RUNTIME_UNAVAILABLE


# ---------------------------------------------------------------------------
# Exit code mapping
# ---------------------------------------------------------------------------


async def test_exit_code_mapping_terminal() -> None:
    cases = [
        (2, rl.FailureClass.CONTRACT_MISMATCH),
        (3, rl.FailureClass.INSTALLATION_UNAUTHORIZED),
        (4, rl.FailureClass.CONFIG_INVALID),
        (5, rl.FailureClass.CONFIG_INVALID),
    ]
    for exit_code, expected_reason in cases:
        link = _make_link()
        async with link._lock:
            link._transition_locked(rl.LinkState.CONNECTED, None)

        err = await link._handle_child_exit(exit_code)
        assert isinstance(err, rl._ErrTerminalError), f"exit {exit_code}: want terminal error"
        st = link.status()
        assert st.state == rl.LinkState.TERMINAL, f"exit {exit_code}: want terminal state"
        assert st.reason == expected_reason, f"exit {exit_code}: want {expected_reason}"


async def test_exit_code_mapping_transient() -> None:
    link = _make_link()
    async with link._lock:
        link._transition_locked(rl.LinkState.CONNECTED, None)

    err = await link._handle_child_exit(1)
    assert err is None, "exit 1: want None (transient)"
    st = link.status()
    assert st.state == rl.LinkState.CONNECTED, "exit 1: state unchanged on transient"
    assert link._restarts == 1, "exit 1: restarts should be 1"


async def test_exit_code_zero_is_clean() -> None:
    link = _make_link()
    async with link._lock:
        link._transition_locked(rl.LinkState.CONNECTED, None)

    err = await link._handle_child_exit(0)
    assert err is None, "exit 0: want None"


# ---------------------------------------------------------------------------
# Child environment building
# ---------------------------------------------------------------------------


def test_build_env_allowlist() -> None:
    """Only allowlisted env vars are passed to the child."""
    saved = dict(os.environ)
    try:
        os.environ.clear()
        os.environ["PATH"] = "/usr/bin:/bin"
        os.environ["HOME"] = "/root"
        os.environ["KEI_RUNTIME_TOKEN"] = "test-token"
        os.environ["DISCORD_TOKEN"] = "should-not-leak"
        os.environ["TWILIO_AUTH_TOKEN"] = "should-not-leak"

        link = _make_link()
        env = link._build_env()

        # allowlisted vars present
        assert env["PATH"] == "/usr/bin:/bin"
        assert env["KEI_RUNTIME_TOKEN"] == "test-token"

        # canary secrets absent
        assert "DISCORD_TOKEN" not in env
        assert "TWILIO_AUTH_TOKEN" not in env

        # SDK identity vars injected
        assert env["KEI_AGENTWARE_SDK_LANG"] == "python"
        assert "KEI_AGENTWARE_SDK_VERSION" in env
    finally:
        os.environ.clear()
        os.environ.update(saved)


def test_build_env_max_count() -> None:
    saved = dict(os.environ)
    try:
        os.environ.clear()
        link = _make_link()
        env = link._build_env()
        # count should be len(allowlist) + 2 injected vars at most
        max_want = len(rl.CHILD_ENV_ALLOWLIST) + 2
        assert len(env) <= max_want
    finally:
        os.environ.clear()
        os.environ.update(saved)


# ---------------------------------------------------------------------------
# Classify beat outcome
# ---------------------------------------------------------------------------


def test_classify_beat_outcome() -> None:
    cases = [
        (rl.BeatOutcome.CATALOG_UNREACHABLE, rl.FailureClass.CATALOG_UNREACHABLE),
        (rl.BeatOutcome.CATALOG_TIMEOUT, rl.FailureClass.CATALOG_TIMEOUT),
        (rl.BeatOutcome.CATALOG_ERROR, rl.FailureClass.CATALOG_ERROR),
        (rl.BeatOutcome.CATALOG_BACKPRESSURE, rl.FailureClass.CATALOG_BACKPRESSURE),
        (rl.BeatOutcome.CATALOG_REJECTED, rl.FailureClass.CONTRACT_MISMATCH),
        (
            rl.BeatOutcome.UNAUTHORIZED,
            rl.FailureClass.RUNTIME_UNAVAILABLE,
        ),  # unlikely, maps to generic
    ]
    for outcome, expected in cases:
        got = rl._classify_beat_outcome(outcome)
        assert got == expected, f"classifyBeatOutcome({outcome}) = {got}, want {expected}"


# ---------------------------------------------------------------------------
# Lifecycle event emission
# ---------------------------------------------------------------------------


async def test_lifecycle_event_emitted() -> None:
    captured: list[rl.LinkEvent] = []

    async def capture(event: rl.LinkEvent) -> None:
        captured.append(event)

    class CaptureSink:
        async def record_lifecycle(self, event: rl.LinkEvent) -> None:
            captured.append(event)

    link = _make_link()
    link._audit_sink = CaptureSink()

    async with link._lock:
        link._transition_locked(rl.LinkState.CONNECTED, None)

    await asyncio.sleep(0.01)  # let fire-and-forget tasks run

    assert len(captured) >= 1
    ev = captured[0]
    assert ev.name == rl.LifecycleEventName.CONNECTED
    assert ev.state == rl.LinkState.CONNECTED
    assert ev.harness.kind == "cli"
    assert ev.sdk_instance_id == "sdk-test-1"


async def test_slow_sink_does_not_block() -> None:
    """A blocking audit sink must not delay state transitions."""
    blocked = asyncio.Event()

    class BlockingSink:
        async def record_lifecycle(self, event: rl.LinkEvent) -> None:
            await blocked.wait()

    link = _make_link()
    link._audit_sink = BlockingSink()

    done = asyncio.Event()

    async def transition() -> None:
        async with link._lock:
            link._transition_locked(rl.LinkState.CONNECTED, None)
        done.set()

    task = asyncio.create_task(transition())
    try:
        await asyncio.wait_for(done.wait(), timeout=1.0)
    finally:
        blocked.set()
        await asyncio.wait_for(task, timeout=1.0)


async def test_events_channel() -> None:
    link = _make_link()

    async with link._lock:
        link._transition_locked(rl.LinkState.STARTING, None)
        link._transition_locked(rl.LinkState.CONNECTED, None)

    events = []
    async for ev in link.events():
        events.append(ev)
        if len(events) >= 2:
            break

    assert len(events) >= 1
    assert events[0].name in (rl.LifecycleEventName.STARTED, rl.LifecycleEventName.CONNECTED)


async def test_events_bounded() -> None:
    link = _make_link()

    # fill the event ring past capacity
    for _ in range(rl.EVENT_BUFFER_SIZE * 2):
        link._emit_lifecycle_event(rl.LifecycleEventName.STARTED, rl.LinkState.STARTING)

    # drain with short timeout — the ring holds at most EVENT_BUFFER_SIZE items
    count = 0
    it = link.events()
    try:
        while True:
            try:
                await asyncio.wait_for(it.__anext__(), timeout=0.5)
                count += 1
            except asyncio.TimeoutError:
                break
    except StopAsyncIteration:
        pass

    assert 0 < count <= rl.EVENT_BUFFER_SIZE


async def test_metrics_sink_called() -> None:
    transitions: list[rl.LinkState] = []

    class TrackingSink:
        async def emit_beat(self, beat: rl.BeatEvent) -> None: ...
        async def emit_restart(self, attempt: int, cause: rl.FailureClass) -> None: ...
        async def emit_lifecycle(self, state: rl.LinkState, reason: rl.FailureClass | None) -> None:
            transitions.append(state)

    link = _make_link()
    link._metrics_sink = TrackingSink()

    async with link._lock:
        link._transition_locked(rl.LinkState.STARTING, None)
        link._transition_locked(rl.LinkState.CONNECTED, None)

    await asyncio.sleep(0.01)  # let fire-and-forget tasks run
    assert len(transitions) >= 2


# ---------------------------------------------------------------------------
# Crashloop detection
# ---------------------------------------------------------------------------


def test_crashloop_detection_under_threshold() -> None:
    link = _make_link()
    assert link._is_crashloop() is False

    link._crash_times = [100.0, 200.0, 300.0, 400.0]  # only 4 entries
    assert link._is_crashloop() is False


def test_crashloop_detection_within_window() -> None:
    link = _make_link()
    base = 1000.0
    link._crash_times = [base + i * 100 for i in range(rl.CRASHLOOP_THRESHOLD)]
    # window is (base + 400) - base = 400 <= 900
    assert link._is_crashloop() is True


def test_crashloop_detection_outside_window() -> None:
    link = _make_link()
    base = 1000.0
    link._crash_times = [base + i * 300 for i in range(rl.CRASHLOOP_THRESHOLD)]
    # window is (base + 1200) - base = 1200 > 900
    assert link._is_crashloop() is False


# ---------------------------------------------------------------------------
# new_link factory
# ---------------------------------------------------------------------------


def test_new_link_returns_link() -> None:
    cfg = rl.RuntimeLinkConfig(
        enabled=False,
    )
    link = rl.new_link(cfg)
    assert isinstance(link, rl.Link)
    assert link.cfg.enabled is False


def test_new_link_normalizes_config() -> None:
    """Interval below min is clamped, not an error."""
    cfg = rl.RuntimeLinkConfig(
        enabled=True,
        control_plane_url="http://localhost:8080",
        harness=rl.HarnessEnvelope(kind="cli"),
        interval=5.0,  # below MIN_INTERVAL, should be clamped
        beat_timeout=2.0,
    )
    link = rl.new_link(cfg)
    assert link.cfg.interval == rl.MIN_INTERVAL


def test_new_link_rejects_bad_config() -> None:
    """A bad harness kind raises."""
    cfg = rl.RuntimeLinkConfig(
        enabled=True,
        control_plane_url="http://localhost:8080",
        harness=rl.HarnessEnvelope(kind="invalid_harness"),
    )
    with pytest.raises(rl.RuntimeLinkConfigError):
        rl.new_link(cfg)


def test_new_link_with_sinks() -> None:
    metrics_called = False

    class TestMetricsSink:
        async def emit_beat(self, beat: rl.BeatEvent) -> None: ...
        async def emit_restart(self, attempt: int, cause: rl.FailureClass) -> None: ...
        async def emit_lifecycle(self, state: rl.LinkState, reason: rl.FailureClass | None) -> None:
            nonlocal metrics_called
            metrics_called = True

    cfg = rl.RuntimeLinkConfig(enabled=False)
    link = rl.new_link(cfg, metrics_sink=TestMetricsSink())
    assert isinstance(link, rl.Link)


# ---------------------------------------------------------------------------
# Integration tests (controlled subprocess)
# ---------------------------------------------------------------------------


async def test_read_stdout_identity() -> None:
    """Read a valid identity line from a stream."""
    reader = asyncio.StreamReader()
    reader.feed_data(
        b'{"v":1,"event":"identity","run_id":"r1","installation_id":"i1","org_id":"o1"}\n'
    )
    reader.feed_eof()

    link = _make_link()
    result = await link._read_stdout(reader)
    assert result is not None
    kind, payload = result
    assert kind == "identity"
    identity, dropped_agents = payload
    assert isinstance(identity, rl.RuntimeIdentity)
    assert identity.run_id == "r1"
    assert dropped_agents == 0


async def test_read_stdout_beat() -> None:
    reader = asyncio.StreamReader()
    reader.feed_data(
        b'{"v":1,"event":"beat","run_id":"r1","seq":1,"at":"2026-09-23T18:00:00Z","outcome":"ok"}\n'
    )
    reader.feed_eof()

    link = _make_link()
    result = await link._read_stdout(reader)
    assert result is not None
    kind, payload = result
    assert kind == "beat"
    assert isinstance(payload, rl.BeatEvent)
    assert payload.outcome == rl.BeatOutcome.OK


async def test_read_stdout_terminal() -> None:
    reader = asyncio.StreamReader()
    reader.feed_data(b'{"v":1,"event":"terminal","run_id":"r1","reason":"unauthorized"}\n')
    reader.feed_eof()

    link = _make_link()
    result = await link._read_stdout(reader)
    assert result is not None
    kind, payload = result
    assert kind == "terminal"
    assert isinstance(payload, rl.TerminalEvent)
    assert payload.reason == "unauthorized"


async def test_read_stdout_ignored_event() -> None:
    reader = asyncio.StreamReader()
    reader.feed_data(b'{"v":1,"event":"unknown_event","run_id":"r1"}\n')
    reader.feed_eof()

    link = _make_link()
    result = await link._read_stdout(reader)
    assert result is None  # ignored


async def test_read_stdout_malformed_returns_none() -> None:
    reader = asyncio.StreamReader()
    reader.feed_data(b"not json\n")
    reader.feed_eof()

    link = _make_link()
    result = await link._read_stdout(reader)
    assert result is None
    assert link._fails == 1  # malformed line counted


async def test_read_stdout_empty_line() -> None:
    reader = asyncio.StreamReader()
    reader.feed_data(b"\n")
    reader.feed_eof()

    link = _make_link()
    result = await link._read_stdout(reader)
    assert result is None


async def test_read_stdout_eof() -> None:
    reader = asyncio.StreamReader()
    reader.feed_eof()

    link = _make_link()
    result = await link._read_stdout(reader)
    assert result is None


# ---------------------------------------------------------------------------
# _exit_code_failure_class unit tests
# ---------------------------------------------------------------------------


def test_exit_code_failure_class_fn() -> None:
    assert rl._exit_code_failure_class(0) == (None, False)
    assert rl._exit_code_failure_class(1) == (rl.FailureClass.RUNTIME_UNAVAILABLE, False)
    assert rl._exit_code_failure_class(2) == (rl.FailureClass.CONTRACT_MISMATCH, True)
    assert rl._exit_code_failure_class(3) == (rl.FailureClass.INSTALLATION_UNAUTHORIZED, True)
    assert rl._exit_code_failure_class(4) == (rl.FailureClass.CONFIG_INVALID, True)
    assert rl._exit_code_failure_class(127) == (rl.FailureClass.CONFIG_INVALID, True)


def test_new_sdk_instance_id_format() -> None:
    sid = rl._new_sdk_instance_id()
    assert isinstance(sid, str)
    assert len(sid) == 16
    assert all(c in "0123456789abcdef" for c in sid)
