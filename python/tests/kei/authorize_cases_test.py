"""KeiProxyEvaluator parity table (fixtures/kei/authorize-cases.v1.json).

Each case drives a real subprocess: the shared fake kei-proxy
(fixtures/kei/fake-kei-proxy.sh) is copied into a temp directory next to the
case's canned stdout, stderr and exit code. The TypeScript and Go suites load
the same files and must reach the same decision for every case.
"""

import json
import logging
import shutil
import sys
from pathlib import Path
from typing import Any

import pytest

from pedro_agentware.kei import (
    AUTHORIZE_CHILD_ENV_ALLOWLIST,
    KeiProxyAuthorizeClient,
    KeiProxyEvaluator,
)
from pedro_agentware.middleware import Action, CallerContext, Decision

FIXTURES = Path(__file__).resolve().parents[3] / "fixtures" / "kei"
TABLE: dict[str, Any] = json.loads((FIXTURES / "authorize-cases.v1.json").read_text())
POLICY: dict[str, Any] = json.loads((FIXTURES / TABLE["policy"]).read_text())
CASES: list[dict[str, Any]] = TABLE["cases"]
CANARIES: dict[str, str] = TABLE["canaries"]

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="fake kei-proxy is a POSIX sh script"
)


def build_caller(case: dict[str, Any]) -> CallerContext:
    """Build the caller the table describes from policy.v1.json."""
    subject = POLICY["users"][case["input"]["user"]]["id"]
    overrides = case["input"].get("caller", {})
    return CallerContext(
        user_id=overrides.get("user_id", subject),
        invoking_subject=subject,
        parent_span=overrides.get("parent_span", ""),
        delegation_depth=overrides.get("delegation_depth", 0),
        span_id=TABLE["caller_defaults"]["span_id"],
        agent_id=POLICY["agent"]["id"],
        agent_version=POLICY["agent"]["version"],
        framework=POLICY["agent"]["framework"],
        workspace_id=POLICY["workspace"]["id"],
    )


def build_env(case: dict[str, Any]) -> dict[str, str]:
    """A parent environment holding the token plus secrets that must not leak."""
    env = {"PATH": "/usr/bin:/bin"}
    for name in TABLE["child_env"]["stripped"]:
        env[name] = f"leaked-{name}"
    if not case.get("env", {}).get("omit_token"):
        env["KEI_RUNTIME_TOKEN"] = CANARIES["runtime_token"]
    return env


def install_fake(case: dict[str, Any], tmp_path: Path) -> Path:
    """Write the fake kei-proxy and the case's canned answer into tmp_path."""
    proxy = case["proxy"]
    fake = tmp_path / "kei-proxy"
    if proxy.get("missing_binary"):
        return fake
    shutil.copy(FIXTURES / "fake-kei-proxy.sh", fake)
    fake.chmod(0o755)
    (tmp_path / "stdout").write_text(proxy["stdout"])
    (tmp_path / "stderr").write_text(proxy["stderr"])
    (tmp_path / "exit_code").write_text(str(proxy["exit_code"]))
    if proxy.get("hang"):
        (tmp_path / "hang").touch()
    return fake


def run_case(case: dict[str, Any], tmp_path: Path) -> Decision:
    client = KeiProxyAuthorizeClient(
        executable=str(install_fake(case, tmp_path)),
        timeout=TABLE["timeout_ms"] / 1000,
        env=build_env(case),
    )
    return KeiProxyEvaluator(client).evaluate(
        case["input"]["tool"], case["input"]["args"], build_caller(case)
    )


@pytest.mark.parametrize("case", CASES, ids=[c["id"] for c in CASES])
def test_authorize_case(
    case: dict[str, Any], tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    expected = case["expected"]

    decision = run_case(case, tmp_path)

    assert decision.action == Action(expected["action"])
    klass = f"kei-proxy {expected['reason_class']}"
    assert decision.reason == klass or decision.reason.startswith(klass + ":"), decision.reason
    assert decision.rule == expected["rule"]
    assert decision.enrollment == expected["enrollment"]
    if "reason_contains" in expected:
        assert expected["reason_contains"] in decision.reason

    for name, canary in CANARIES.items():
        assert canary not in decision.reason, f"{name} leaked into the reason"
        assert canary not in caplog.text, f"{name} leaked into the logs"

    argv_file = tmp_path / "argv"
    assert argv_file.exists() == expected["invoked"]
    if expected["invoked"]:
        argv = argv_file.read_text().splitlines()
        assert all(CANARIES["runtime_token"] not in arg for arg in argv)
        if "argv" in expected:
            assert argv == expected["argv"]


def test_child_env_carries_the_token_and_only_allowlisted_vars(tmp_path: Path) -> None:
    case = next(c for c in CASES if c["id"] == "allow_member_read")
    run_case(case, tmp_path)

    child = dict(
        line.split("=", 1) for line in (tmp_path / "env").read_text().splitlines() if "=" in line
    )
    assert child["KEI_RUNTIME_TOKEN"] == CANARIES["runtime_token"]
    for name in TABLE["child_env"]["stripped"]:
        assert name not in child
    # sh may add PWD/SHLVL/_ on its own; nothing else from the parent may appear.
    shell_added = {"PWD", "SHLVL", "_", "OLDPWD"}
    assert set(child) - shell_added <= set(TABLE["child_env"]["allowlist"])


def test_child_env_allowlist_matches_the_fixture() -> None:
    assert list(AUTHORIZE_CHILD_ENV_ALLOWLIST) == TABLE["child_env"]["allowlist"]


def test_extra_env_is_passed_explicitly(tmp_path: Path) -> None:
    case = next(c for c in CASES if c["id"] == "allow_member_read")
    client = KeiProxyAuthorizeClient(
        executable=str(install_fake(case, tmp_path)),
        timeout=TABLE["timeout_ms"] / 1000,
        env=build_env(case),
        extra_env={"AWS_PROFILE": "fixture"},
    )
    KeiProxyEvaluator(client).evaluate(
        case["input"]["tool"], case["input"]["args"], build_caller(case)
    )
    assert "AWS_PROFILE=fixture" in (tmp_path / "env").read_text().splitlines()


# --- fixture integrity (checked once, here) ---------------------------------


def test_every_case_uses_a_known_reason_class() -> None:
    for case in CASES:
        assert case["expected"]["reason_class"] in TABLE["reason_classes"], case["id"]


def test_case_ids_are_unique() -> None:
    ids = [c["id"] for c in CASES]
    assert len(ids) == len(set(ids))


@pytest.mark.parametrize("case", [c for c in CASES if c["derived_from"]], ids=lambda c: c["id"])
def test_derived_cases_agree_with_the_seeded_policy(case: dict[str, Any]) -> None:
    """A case derived from a rule must name a rule that applies and match its effect."""
    rules = {r["id"]: r for r in POLICY["rules"]}
    rule = rules[case["derived_from"]]
    user = POLICY["users"][case["input"]["user"]]
    assert user["role"] in rule["roles"]
    assert "*" in rule["tools"] or case["input"]["tool"] in rule["tools"]
    assert case["input"]["tool"] in POLICY["tools"]
    if "linked" in rule:
        assert user["linked"] == rule["linked"]

    expected = case["expected"]
    if rule["effect"] == "allow":
        assert expected["action"] == "allow"
    else:
        assert expected["action"] == "deny"
    if rule["effect"] == "enrollment" and expected["enrollment"] is not None:
        assert expected["enrollment"]["provider_user_id"] == user["provider_user_id"]
        assert expected["enrollment"]["org_id"] == POLICY["org"]["id"]
        assert expected["enrollment"]["workspace_id"] == POLICY["workspace"]["id"]


def test_the_table_covers_the_required_scenarios() -> None:
    classes = {c["expected"]["reason_class"] for c in CASES}
    assert set(TABLE["reason_classes"]) <= classes
    assert any(
        c["expected"]["enrollment"] for c in CASES if c["expected"]["reason_class"] == "deny"
    )
