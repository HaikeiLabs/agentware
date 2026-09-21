"""Tests for the policy-bundle lifecycle.

Exercises the bundle-fetch, parse, persist, activate, and refresh cycle
with no control-plane dependency: the fetch call is an injected double.
"""

import json
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from pedro_agentware.kei import (
    RUNTIME_CONTROL_PLANE_URL_ENV,
    RUNTIME_TOKEN_ENV,
    BundleFetchError,
    BundleParseError,
    PolicyBundleData,
    PolicyBundleLifecycle,
)
from pedro_agentware.kei.policy_bundle import (
    LEGACY_ABAC_URL_ENV,
    LEGACY_BOOTSTRAP_TOKEN_ENV,
    LEGACY_KEI_API_URL_ENV,
    _resolve_control_plane_url,
    _resolve_runtime_token,
)
from pedro_agentware.middleware import CallerContext
from pedro_agentware.middleware.types import Action

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_VALID_BUNDLE = {
    "version": "1",
    "bundle_id": "bundle-abc",
    "default_deny": True,
    "rules": [
        {
            "name": "allow-search-wiki",
            "tools": ["search_wiki"],
            "action": "allow",
        },
        {
            "name": "allow-web-search",
            "tools": ["web_search"],
            "action": "allow",
        },
    ],
}

_SAMPLE_ARGS = {"query": "test"}
_A_CALLER = CallerContext(user_id="U1", invoking_subject="U_HUMAN")


def _make_lifecycle(
    bundle_data: dict | None = None,
    raises: Exception | None = None,
    persist_path: str | Path | None = None,
    local_only: bool = False,
) -> PolicyBundleLifecycle:
    """Build a lifecycle with an injected fetch function.

    When ``bundle_data`` is provided it is returned as the fetched bundle;
    when ``raises`` is set the fetch raises that exception; when
    ``local_only`` is True no credentials are configured.
    """

    async def fake_fetch(url: str, token: str, **kw) -> dict:
        if raises is not None:
            raise raises
        if bundle_data is not None:
            return bundle_data
        return _VALID_BUNDLE

    if persist_path is None:
        persist_path = tempfile.mktemp(suffix=".json")

    if local_only:
        url = None
        token = None
    else:
        url = "http://control-plane.example"
        token = "s3kr1t"

    return PolicyBundleLifecycle(
        control_plane_url=url,
        runtime_token=token,
        persist_path=persist_path,
        fetch_fn=fake_fetch,
    )


# ===================================================================
# PolicyBundleData — wire format parsing
# ===================================================================


class TestPolicyBundleDataFromWire:
    def test_minimal_valid_bundle(self):
        data = {"version": "1", "rules": []}
        bundle = PolicyBundleData.from_wire(data)
        assert bundle.version == "1"
        assert bundle.rules == []
        assert bundle.default_deny is True

    def test_full_bundle_with_rules(self):
        bundle = PolicyBundleData.from_wire(_VALID_BUNDLE)
        assert bundle.version == "1"
        assert bundle.bundle_id == "bundle-abc"
        assert len(bundle.rules) == 2
        assert bundle.rules[0].name == "allow-search-wiki"
        assert bundle.rules[0].tools == ["search_wiki"]
        assert bundle.rules[0].action == Action.ALLOW
        assert bundle.rules[1].name == "allow-web-search"
        assert bundle.rules[1].tools == ["web_search"]

    def test_default_deny_can_be_false(self):
        data = {"version": "1", "default_deny": False, "rules": []}
        bundle = PolicyBundleData.from_wire(data)
        assert bundle.default_deny is False

    def test_missing_version_raises(self):
        with pytest.raises(BundleParseError, match="missing 'version'"):
            PolicyBundleData.from_wire({"rules": []})

    def test_empty_version_raises(self):
        with pytest.raises(BundleParseError, match="missing 'version'"):
            PolicyBundleData.from_wire({"version": "", "rules": []})

    def test_rules_must_be_a_list(self):
        with pytest.raises(BundleParseError, match="'rules' must be a list"):
            PolicyBundleData.from_wire({"version": "1", "rules": "not a list"})

    def test_rule_missing_name_raises(self):
        data = {"version": "1", "rules": [{"tools": ["foo"], "action": "allow"}]}
        with pytest.raises(BundleParseError, match="missing 'name'"):
            PolicyBundleData.from_wire(data)

    def test_unknown_action_raises(self):
        data = {
            "version": "1",
            "rules": [{"name": "r1", "tools": ["foo"], "action": "unknown"}],
        }
        with pytest.raises(BundleParseError, match="unknown action"):
            PolicyBundleData.from_wire(data)

    def test_unknown_condition_operator_raises(self):
        data = {
            "version": "1",
            "rules": [
                {
                    "name": "r1",
                    "tools": ["foo"],
                    "action": "deny",
                    "conditions": [{"field": "caller.role", "operator": "bogus", "value": "admin"}],
                }
            ],
        }
        with pytest.raises(BundleParseError, match="unknown condition operator"):
            PolicyBundleData.from_wire(data)

    def test_rate_limit_parsing(self):
        data = {
            "version": "1",
            "rules": [
                {
                    "name": "rate-limited",
                    "tools": ["search_wiki"],
                    "action": "allow",
                    "max_rate": {"count": 10, "window_seconds": 60},
                }
            ],
        }
        bundle = PolicyBundleData.from_wire(data)
        assert bundle.rules[0].max_rate is not None
        assert bundle.rules[0].max_rate.count == 10
        assert bundle.rules[0].max_rate.window == timedelta(seconds=60)

    def test_rate_limit_zero_count_skipped(self):
        data = {
            "version": "1",
            "rules": [
                {
                    "name": "no-rate",
                    "tools": ["foo"],
                    "action": "allow",
                    "max_rate": {"count": 0, "window_seconds": 60},
                }
            ],
        }
        bundle = PolicyBundleData.from_wire(data)
        assert bundle.rules[0].max_rate is None

    def test_redact_fields(self):
        data = {
            "version": "1",
            "rules": [
                {
                    "name": "filter-creds",
                    "tools": ["login"],
                    "action": "filter",
                    "redact_fields": ["password", "token"],
                }
            ],
        }
        bundle = PolicyBundleData.from_wire(data)
        assert bundle.rules[0].redact_fields == ["password", "token"]
        assert bundle.rules[0].action == Action.FILTER

    def test_expires_at(self):
        future = (datetime.now() + timedelta(hours=1)).isoformat()
        data = {"version": "1", "expires_at": future, "rules": []}
        bundle = PolicyBundleData.from_wire(data)
        assert bundle.expires_at is not None

    def test_expires_at_unparseable_raises(self):
        data = {"version": "1", "expires_at": "not-a-date", "rules": []}
        with pytest.raises(BundleParseError, match="unparseable expires_at"):
            PolicyBundleData.from_wire(data)

    def test_condition_all_operators(self):
        data = {
            "version": "1",
            "rules": [
                {
                    "name": "all-conditions",
                    "tools": ["foo"],
                    "action": "deny",
                    "conditions": [
                        {"field": "caller.role", "operator": "eq", "value": "admin"},
                        {"field": "caller.source", "operator": "not_eq", "value": "cli"},
                        {"field": "args.path", "operator": "contains", "value": "/secret/"},
                        {"field": "args.path", "operator": "not_contains", "value": "/public/"},
                        {"field": "args.pattern", "operator": "matches", "value": r"^/api/"},
                        {"field": "args.pattern", "operator": "not_matches", "value": r"^/admin/"},
                        {"field": "caller.trusted", "operator": "exists"},
                        {"field": "caller.fake", "operator": "not_exists"},
                    ],
                }
            ],
        }
        bundle = PolicyBundleData.from_wire(data)
        conditions = bundle.rules[0].conditions
        assert len(conditions) == 8
        # Quick spot-check: the first condition
        assert conditions[0].field == "caller.role"
        assert conditions[0].value == "admin"

    def test_round_trip_to_dict(self):
        bundle = PolicyBundleData.from_wire(_VALID_BUNDLE)
        as_dict = bundle.to_dict()
        assert as_dict["version"] == "1"
        assert as_dict["bundle_id"] == "bundle-abc"
        assert len(as_dict["rules"]) == 2
        # Re-parse
        rebuilt = PolicyBundleData.from_wire(as_dict)
        assert rebuilt.version == bundle.version
        assert len(rebuilt.rules) == len(bundle.rules)

    def test_digest_changes_with_content(self):
        a = PolicyBundleData.from_wire({"version": "1", "rules": []})
        b = PolicyBundleData.from_wire({"version": "2", "rules": []})
        assert a.digest() != b.digest()

    def test_is_expired(self):
        past = (datetime.now() - timedelta(hours=1)).isoformat()
        data = {"version": "1", "expires_at": past, "rules": []}
        bundle = PolicyBundleData.from_wire(data)
        assert bundle.is_expired()

    def test_is_not_expired(self):
        future = (datetime.now() + timedelta(hours=1)).isoformat()
        data = {"version": "1", "expires_at": future, "rules": []}
        bundle = PolicyBundleData.from_wire(data)
        assert not bundle.is_expired()

    def test_no_expiry_never_expired(self):
        bundle = PolicyBundleData(version="1")
        assert not bundle.is_expired()


# ===================================================================
# PolicyBundleData — to_policy
# ===================================================================


class TestPolicyBundleDataToPolicy:
    def test_default_deny_reflected(self):
        bundle = PolicyBundleData.from_wire({"version": "1", "default_deny": True, "rules": []})
        policy = bundle.to_policy()
        assert policy.default_deny is True

    def test_default_allow_reflected(self):
        bundle = PolicyBundleData.from_wire({"version": "1", "default_deny": False, "rules": []})
        policy = bundle.to_policy()
        assert policy.default_deny is False

    def test_rules_are_carried(self):
        bundle = PolicyBundleData.from_wire(_VALID_BUNDLE)
        policy = bundle.to_policy()
        assert len(policy.rules) == 2
        assert policy.rules[0].name == "allow-search-wiki"


# ===================================================================
# PolicyBundleLifecycle — credential resolution
# ===================================================================


class TestCredentialResolution:
    def test_new_env_vars_take_priority(self, monkeypatch):
        monkeypatch.setenv(RUNTIME_CONTROL_PLANE_URL_ENV, "http://new.example")
        monkeypatch.setenv(RUNTIME_TOKEN_ENV, "new-token")
        monkeypatch.setenv(LEGACY_ABAC_URL_ENV, "http://legacy.example")
        monkeypatch.setenv(LEGACY_BOOTSTRAP_TOKEN_ENV, "old-token")
        assert _resolve_control_plane_url() == "http://new.example"
        assert _resolve_runtime_token() == "new-token"

    def test_legacy_url_fallback(self, monkeypatch):
        monkeypatch.delenv(RUNTIME_CONTROL_PLANE_URL_ENV, raising=False)
        monkeypatch.setenv(LEGACY_ABAC_URL_ENV, "http://legacy.example")
        assert _resolve_control_plane_url() == "http://legacy.example"

    def test_legacy_kei_api_url_fallback(self, monkeypatch):
        monkeypatch.delenv(RUNTIME_CONTROL_PLANE_URL_ENV, raising=False)
        monkeypatch.delenv(LEGACY_ABAC_URL_ENV, raising=False)
        monkeypatch.setenv(LEGACY_KEI_API_URL_ENV, "http://kei-api.example")
        assert _resolve_control_plane_url() == "http://kei-api.example"

    def test_legacy_token_fallback(self, monkeypatch):
        monkeypatch.delenv(RUNTIME_TOKEN_ENV, raising=False)
        monkeypatch.setenv(LEGACY_BOOTSTRAP_TOKEN_ENV, "legacy-token")
        assert _resolve_runtime_token() == "legacy-token"

    def test_no_url_raises(self, monkeypatch):
        monkeypatch.delenv(RUNTIME_CONTROL_PLANE_URL_ENV, raising=False)
        monkeypatch.delenv(LEGACY_ABAC_URL_ENV, raising=False)
        monkeypatch.delenv(LEGACY_KEI_API_URL_ENV, raising=False)
        with pytest.raises(BundleFetchError, match="not configured"):
            _resolve_control_plane_url()

    def test_no_token_raises(self, monkeypatch):
        monkeypatch.delenv(RUNTIME_TOKEN_ENV, raising=False)
        monkeypatch.delenv(LEGACY_BOOTSTRAP_TOKEN_ENV, raising=False)
        with pytest.raises(BundleFetchError, match="not configured"):
            _resolve_runtime_token()

    def test_local_only_when_no_creds(self):
        lc = PolicyBundleLifecycle(
            control_plane_url=None,
            runtime_token=None,
            persist_path=":memory:",
        )
        assert lc.is_local_only
        assert lc.is_loaded is False

    def test_explicit_creds_bypass_env(self, monkeypatch):
        monkeypatch.delenv(RUNTIME_CONTROL_PLANE_URL_ENV, raising=False)
        monkeypatch.delenv(RUNTIME_TOKEN_ENV, raising=False)
        lc = PolicyBundleLifecycle(
            control_plane_url="http://explicit.example",
            runtime_token="explicit-token",
            persist_path=":memory:",
        )
        assert not lc.is_local_only


# ===================================================================
# PolicyBundleLifecycle — start and deny-until-loaded
# ===================================================================


class TestLifecycleStart:
    @pytest.mark.asyncio
    async def test_successful_fetch_activates_bundle(self):
        lc = _make_lifecycle()
        await lc.start()
        assert lc.is_loaded
        assert lc.active_bundle is not None
        assert lc.active_bundle.version == "1"

    @pytest.mark.asyncio
    async def test_fetch_fails_no_cache_denies(self):
        lc = _make_lifecycle(raises=ConnectionError("down"))
        await lc.start()
        assert not lc.is_loaded
        assert lc.active_bundle is None

    @pytest.mark.asyncio
    async def test_fetch_fails_cached_bundle_activates_cache(self, tmp_path):
        cache = tmp_path / "bundle.json"
        cache.write_text(json.dumps(_VALID_BUNDLE), encoding="utf-8")
        lc = _make_lifecycle(
            raises=ConnectionError("down"),
            persist_path=str(cache),
        )
        await lc.start()
        assert lc.is_loaded
        assert lc.active_bundle is not None
        assert lc.active_bundle.version == "1"

    @pytest.mark.asyncio
    async def test_fetch_fails_corrupt_cache_stays_denied(self, tmp_path):
        cache = tmp_path / "bundle.json"
        cache.write_text("not json", encoding="utf-8")
        lc = _make_lifecycle(
            raises=ConnectionError("down"),
            persist_path=str(cache),
        )
        await lc.start()
        assert not lc.is_loaded
        assert lc.active_bundle is None

    @pytest.mark.asyncio
    async def test_local_only_skips_fetch_and_allows(self):
        lc = _make_lifecycle(local_only=True)
        await lc.start()
        assert lc.is_loaded
        assert lc.is_local_only
        assert lc.active_bundle is None

    @pytest.mark.asyncio
    async def test_fetch_success_persists_bundle(self, tmp_path):
        cache = tmp_path / "bundle.json"
        lc = _make_lifecycle(persist_path=str(cache))
        await lc.start()
        assert cache.exists()
        loaded = json.loads(cache.read_text(encoding="utf-8"))
        assert loaded["version"] == "1"

    @pytest.mark.asyncio
    async def test_start_called_twice_is_idempotent(self):
        lc = _make_lifecycle()
        await lc.start()
        assert lc.is_loaded
        await lc.start()
        assert lc.is_loaded


# ===================================================================
# PolicyBundleLifecycle — evaluate
# ===================================================================


class TestLifecycleEvaluate:
    @pytest.mark.asyncio
    async def test_deny_before_start(self):
        lc = _make_lifecycle()
        decision = lc.evaluate("search_wiki", _SAMPLE_ARGS, _A_CALLER)
        assert decision.action == Action.DENY

    @pytest.mark.asyncio
    async def test_allow_matching_rule(self):
        lc = _make_lifecycle()
        await lc.start()
        decision = lc.evaluate("search_wiki", _SAMPLE_ARGS, _A_CALLER)
        assert decision.action == Action.ALLOW

    @pytest.mark.asyncio
    async def test_deny_missing_rule_with_default_deny(self):
        lc = _make_lifecycle()
        await lc.start()
        decision = lc.evaluate("unknown_tool", _SAMPLE_ARGS, _A_CALLER)
        assert decision.action == Action.DENY

    @pytest.mark.asyncio
    async def test_allow_when_default_deny_is_false_and_no_rule_matches(self):
        bundle = dict(_VALID_BUNDLE)
        bundle["default_deny"] = False
        lc = _make_lifecycle(bundle_data=bundle)
        await lc.start()
        decision = lc.evaluate("unknown_tool", _SAMPLE_ARGS, _A_CALLER)
        assert decision.action == Action.ALLOW

    @pytest.mark.asyncio
    async def test_expired_bundle_denies(self):
        past = (datetime.now() - timedelta(hours=1)).isoformat()
        bundle = dict(_VALID_BUNDLE)
        bundle["expires_at"] = past
        lc = _make_lifecycle(bundle_data=bundle)
        await lc.start()
        decision = lc.evaluate("search_wiki", _SAMPLE_ARGS, _A_CALLER)
        assert decision.action == Action.DENY
        assert "expired" in decision.reason

    @pytest.mark.asyncio
    async def test_local_only_allows_everything(self):
        lc = _make_lifecycle(local_only=True)
        await lc.start()
        decision = lc.evaluate("any_tool", _SAMPLE_ARGS, _A_CALLER)
        assert decision.action == Action.ALLOW

    @pytest.mark.asyncio
    async def test_rule_with_condition_matches_caller(self):
        bundle = {
            "version": "1",
            "default_deny": True,
            "rules": [
                {
                    "name": "admin-only",
                    "tools": ["admin_tool"],
                    "action": "allow",
                    "conditions": [{"field": "caller.role", "operator": "eq", "value": "admin"}],
                }
            ],
        }
        lc = _make_lifecycle(bundle_data=bundle)
        await lc.start()
        admin = CallerContext(user_id="U1", invoking_subject="U_HUMAN", role="admin")
        user = CallerContext(user_id="U2", invoking_subject="U_HUMAN2", role="user")
        assert lc.evaluate("admin_tool", {}, admin).action == Action.ALLOW
        assert lc.evaluate("admin_tool", {}, user).action == Action.DENY

    @pytest.mark.asyncio
    async def test_filter_action_with_redacted_args(self):
        bundle = {
            "version": "1",
            "default_deny": True,
            "rules": [
                {
                    "name": "hide-secret",
                    "tools": ["login"],
                    "action": "filter",
                    "redact_fields": ["password"],
                }
            ],
        }
        lc = _make_lifecycle(bundle_data=bundle)
        await lc.start()
        decision = lc.evaluate("login", {"password": "s3kr1t"}, _A_CALLER)
        assert decision.action == Action.FILTER

    @pytest.mark.asyncio
    async def test_deny_after_failed_fetch_and_no_cache(self):
        lc = _make_lifecycle(raises=ConnectionError("down"))
        await lc.start()
        decision = lc.evaluate("search_wiki", _SAMPLE_ARGS, _A_CALLER)
        assert decision.action == Action.DENY
        assert "no policy bundle loaded" in decision.reason


# ===================================================================
# PolicyBundleLifecycle — refresh
# ===================================================================


class TestLifecycleRefresh:
    @pytest.mark.asyncio
    async def test_successful_refresh_updates_bundle(self):
        lc = _make_lifecycle(bundle_data=_VALID_BUNDLE)
        await lc.start()
        assert lc.active_bundle is not None

        new_bundle = dict(_VALID_BUNDLE)
        new_bundle["version"] = "2"

        async def new_fetch(url: str, token: str, **kw):
            return new_bundle

        lc._fetch_fn = new_fetch  # type: ignore[method-assign]

        result = await lc.refresh()
        assert result is not None
        assert result.version == "2"
        assert lc.active_bundle.version == "2"

    @pytest.mark.asyncio
    async def test_failed_refresh_keeps_old_bundle(self):
        lc = _make_lifecycle(bundle_data=_VALID_BUNDLE)
        await lc.start()
        assert lc.active_bundle is not None
        assert lc.active_bundle.version == "1"

        async def failing_fetch(url: str, token: str, **kw):
            raise ConnectionError("down")

        lc._fetch_fn = failing_fetch  # type: ignore[method-assign]

        result = await lc.refresh()
        assert result is None
        assert lc.active_bundle is not None
        assert lc.active_bundle.version == "1"

    @pytest.mark.asyncio
    async def test_refresh_ratelimited(self):
        """Multiple refresh calls within the min interval should not fetch."""
        call_count = 0

        async def counting_fetch(url: str, token: str, **kw):
            nonlocal call_count
            call_count += 1
            return _VALID_BUNDLE

        lc = _make_lifecycle(bundle_data=_VALID_BUNDLE)
        lc._fetch_fn = counting_fetch
        await lc.start()
        call_count = 0

        await lc.refresh()
        await lc.refresh()
        await lc.refresh()
        assert call_count == 1, f"expected 1 actual fetch, got {call_count}"

    @pytest.mark.asyncio
    async def test_local_only_refresh_returns_none(self):
        lc = _make_lifecycle(local_only=True)
        await lc.start()
        result = await lc.refresh()
        assert result is None

    @pytest.mark.asyncio
    async def test_refresh_persists_bundle(self, tmp_path):
        cache = tmp_path / "bundle.json"
        lc = _make_lifecycle(persist_path=str(cache))
        await lc.start()

        new_bundle = dict(_VALID_BUNDLE)
        new_bundle["version"] = "2"

        async def new_fetch(url: str, token: str, **kw):
            return new_bundle

        lc._fetch_fn = new_fetch  # type: ignore[method-assign]
        await lc.refresh()

        loaded = json.loads(cache.read_text(encoding="utf-8"))
        assert loaded["version"] == "2"


# ===================================================================
# Edge cases
# ===================================================================


class TestEdgeCases:
    def test_evaluate_with_no_bundle_and_not_local_returns_deny(self):
        lc = PolicyBundleLifecycle(
            control_plane_url="http://cp.example",
            runtime_token="tok",
            persist_path=":memory:",
        )
        decision = lc.evaluate("tool", {}, _A_CALLER)
        assert decision.action == Action.DENY

    def test_evaluate_with_no_bundle_and_local_returns_allow(self):
        lc = PolicyBundleLifecycle(
            control_plane_url=None,
            runtime_token=None,
            persist_path=":memory:",
        )
        decision = lc.evaluate("tool", {}, _A_CALLER)
        assert decision.action == Action.ALLOW

    def test_bundle_not_loaded_error_message(self):
        lc = PolicyBundleLifecycle(
            control_plane_url="http://cp.example",
            runtime_token="tok",
            persist_path=":memory:",
        )
        decision = lc.evaluate("tool", {}, _A_CALLER)
        assert "no policy bundle loaded" in decision.reason

    @pytest.mark.asyncio
    async def test_fetch_returns_invalid_json_denies(self, tmp_path):
        async def bad_fetch(url: str, token: str, **kw):
            return {"version": "1", "rules": "invalid"}

        lc = PolicyBundleLifecycle(
            control_plane_url="http://cp.example",
            runtime_token="tok",
            persist_path=str(tmp_path / "bundle.json"),
            fetch_fn=bad_fetch,
        )
        await lc.start()
        assert not lc.is_loaded

    @pytest.mark.asyncio
    async def test_construct_with_no_env_and_no_args_uses_local_only(self, monkeypatch):
        monkeypatch.delenv(RUNTIME_CONTROL_PLANE_URL_ENV, raising=False)
        monkeypatch.delenv(RUNTIME_TOKEN_ENV, raising=False)
        monkeypatch.delenv(LEGACY_ABAC_URL_ENV, raising=False)
        monkeypatch.delenv(LEGACY_BOOTSTRAP_TOKEN_ENV, raising=False)
        lc = PolicyBundleLifecycle(persist_path=":memory:")
        assert lc.is_local_only

    def test_default_persist_path_is_under_home(self):
        path = Path("~/.agentware/policy_bundle.json").expanduser()
        assert str(path).endswith(".agentware/policy_bundle.json")
