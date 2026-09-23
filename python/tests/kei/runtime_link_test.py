"""Contract tests for the RuntimeLink types (shared fixtures in testing/contracts/runtime-link)."""

from __future__ import annotations

import asyncio
import json
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
        return "identity", asdict(ev)
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
