"""Policy evaluation backed by kei-proxy.

Enforcement belongs in the shared library, not in each agent harness. Any
harness that builds an ``AuditedToolClient`` with a :class:`KeiProxyEvaluator`
is governed by construction: the same authorization call, the same vocabulary
translation, and the same fail-closed behaviour, without reimplementing them.

The evaluator talks to kei-proxy through an injected :class:`AuthorizationClient`
so it is testable with no proxy binary present. Agentware deliberately does not
import any harness package -- a library must not depend on its consumer.
"""

import hashlib
import inspect
import json
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol, runtime_checkable

from ..middleware.types import Action, CallerContext, Decision

logger = logging.getLogger(__name__)

__all__ = [
    "AFFIRMATIVE_DECISIONS",
    "AuthorizationClient",
    "AuthorizationResponse",
    "KeiProxyAuthorizeError",
    "KeiProxyEvaluator",
    "REASON_CLASSES",
    "resources_touched",
]


# kei-proxy's affirmative decision is currently "permit". kei is expected to
# rename it to "allow"; both are accepted so the rename is not a flag day.
# Everything else -- including "deny", "enrollment_required", an empty string
# and anything unrecognised -- denies.
AFFIRMATIVE_DECISIONS = frozenset({"permit", "allow"})

# Every Decision this evaluator returns has a reason of the form
# ``"kei-proxy <class>"`` or ``"kei-proxy <class>: <detail>"``. The classes are
# shared with the TypeScript and Go ports (fixtures/kei/authorize-cases.v1.json)
# so a harness can branch on them identically in every language.
REASON_CLASSES = (
    "allow",
    "deny",
    "enrollment_required",
    "unknown_decision",
    "no_decision",
    "malformed_response",
    "empty_response",
    "proxy_error",
    "exit_mismatch",
    "proxy_unavailable",
    "proxy_timeout",
    "missing_token",
    "pin_mismatch",
)


class KeiProxyAuthorizeError(Exception):
    """A kei-proxy authorize call that produced no usable answer.

    ``reason_class`` is one of :data:`REASON_CLASSES`. The message is built by
    this library and never contains proxy stdout or stderr: stdout can carry a
    credential or an enrollment claim link, and stderr is free-form.
    """

    def __init__(self, reason_class: str, detail: str) -> None:
        super().__init__(detail)
        self.reason_class = reason_class
        self.detail = detail


@dataclass
class AuthorizationResponse:
    """Normalized view of a kei-proxy authorize response.

    kei-proxy returns a JSON object; harnesses wrap it in their own dataclass.
    Rather than binding to either shape, the evaluator coerces whatever the
    injected client returns into this via :meth:`from_result`.
    """

    decision: str = ""
    reason: str | None = None
    policy_id: str | None = None
    enrollment: dict[str, Any] | None = None
    connect: dict[str, Any] | None = None

    @classmethod
    def from_result(cls, result: Any) -> "AuthorizationResponse":
        """Coerce a client result -- mapping or attribute-bearing object.

        A result that carries no readable ``decision`` yields an empty decision
        string, which is not affirmative and therefore denies.
        """
        if result is None:
            return cls()

        def read(name: str) -> Any:
            if isinstance(result, dict):
                return result.get(name)
            return getattr(result, name, None)

        decision = read("decision")
        # policy_id is optional in the wire contract; accept the two spellings
        # kei uses rather than silently dropping the attribution.
        policy_id = read("policy_id")
        if policy_id is None:
            policy_id = read("policy")

        enrollment = read("enrollment")
        if enrollment is not None and not isinstance(enrollment, dict):
            enrollment = None

        connect = read("connect")
        if connect is not None and not isinstance(connect, dict):
            connect = None

        return cls(
            decision=str(decision) if decision is not None else "",
            reason=_as_optional_str(read("reason")),
            policy_id=_as_optional_str(policy_id),
            enrollment=enrollment,
            connect=connect,
        )


def _reason(reason_class: str, detail: str | None = None) -> str:
    """Format a Decision reason as ``kei-proxy <class>[: <detail>]``."""
    return f"kei-proxy {reason_class}: {detail}" if detail else f"kei-proxy {reason_class}"


def _as_optional_str(value: Any) -> str | None:
    """Return ``value`` as a string, or None when absent/empty."""
    if value is None:
        return None
    text = str(value)
    return text if text else None


@runtime_checkable
class AuthorizationClient(Protocol):
    """The single call the evaluator needs from a kei-proxy client.

    Structural, and deliberately minimal: any object exposing this signature
    works, including a harness's own client and a test double. Implementations
    are synchronous -- both kei-proxy's CLI invocation and
    ``PolicyEvaluator.evaluate`` are synchronous, and this layer introduces no
    async plumbing.
    """

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
    ) -> Any:
        """Authorize a tool request with its canonical audit context."""
        ...


def resources_touched(tool_name: str, args: dict[str, Any]) -> list[str]:
    """Derive ``type:kind:id`` resource identifiers from a tool call's arguments.

    Entries follow the audit contract's triple form, e.g.
    ``github:repo:owner/sales-pipeline`` and ``github:issue:123``.

    This is the *pre-call* half of resource lineage. Only resources named in
    the arguments are knowable before the tool runs; resources a tool discovers
    or creates are visible only in its result, and are therefore absent here.
    Enriching a record with those is a post-call step this function does not
    and cannot perform -- see :meth:`KeiProxyEvaluator.evaluate`.
    """
    resource_type = tool_name.split(".", 1)[0].split("_", 1)[0].lower()
    if not resource_type:
        return []

    touched: list[str] = []

    def add(kind: str, identifier: Any) -> None:
        if identifier is None:
            return
        value = str(identifier).strip()
        if not value:
            return
        entry = f"{resource_type}:{kind}:{value}"
        if entry not in touched:
            touched.append(entry)

    owner = args.get("owner")
    repo = args.get("repo") or args.get("repository")
    if owner and repo:
        add("repo", f"{owner}/{repo}")
    elif repo:
        add("repo", repo)

    add("issue", args.get("issue_number"))
    add("pull_request", args.get("pull_number") or args.get("pr_number"))
    add("branch", args.get("branch") or args.get("ref"))
    add("file", args.get("path") or args.get("file_path"))

    return touched


def tool_args_digest(args: dict[str, Any]) -> str:
    """Return a deterministic SHA-256 digest without storing raw arguments."""
    encoded = json.dumps(args, sort_keys=True, separators=(",", ":"), default=str).encode()
    return hashlib.sha256(encoded).hexdigest()


class KeiProxyEvaluator:
    """A :class:`~pedro_agentware.middleware.policy.PolicyEvaluator` over kei-proxy.

    Translates kei-proxy's decision vocabulary into agentware's ``Action`` and
    fails closed on every path that is not an explicit affirmative:

    ==========================  ============  ===========================
    proxy decision              action        reason class / enrollment
    ==========================  ============  ===========================
    ``permit`` / ``allow``      ``ALLOW``     ``allow``
    ``deny``                    ``DENY``      ``deny``; enrollment carried
    ``enrollment_required``     ``DENY``      ``enrollment_required``;
                                              enrollment carried
    empty / missing             ``DENY``      ``no_decision``
    anything else               ``DENY``      ``unknown_decision``
    client raised / timed out   ``DENY``      the error's class; never an
                                              allow
    ==========================  ============  ===========================

    Every reason reads ``kei-proxy <class>[: <detail>]`` with a class from
    :data:`REASON_CLASSES`. The enrollment object (which may hold a one-time
    claim link) is carried on the Decision only when it is a JSON object, and
    is never logged. :class:`~pedro_agentware.kei.KeiProxyAuthorizeClient` is
    the kei-proxy CLI client (it lives in its own module so this one never
    spawns the proxy); the table in ``fixtures/kei/authorize-cases.v1.json``
    pins the behaviour shared with the TypeScript and Go ports.
    """

    RULE = "kei-proxy"

    def __init__(
        self,
        client: AuthorizationClient,
        default_action: str = "execute",
    ) -> None:
        """Build an evaluator over ``client``.

        Args:
            client: Anything satisfying :class:`AuthorizationClient`. Injected
                rather than constructed so the evaluator is exercisable with no
                kei-proxy binary present.
            default_action: Action verb sent to kei-proxy when a tool call does
                not name one in its arguments.
        """
        self._client = client
        self._default_action = default_action

    def evaluate(self, tool_name: str, args: dict[str, Any], caller: CallerContext) -> Decision:
        """Authorize ``tool_name`` via kei-proxy and translate the answer.

        The returned ``Decision`` is what ``AuditedToolClient`` both enforces
        and audits, so a denial is recorded rather than inferred.

        The resource sent to kei-proxy is derived from ``args`` by
        :func:`resources_touched`, so only resources named in the arguments are
        knowable here. A tool's result may name further resources; attaching
        those is post-call enrichment, which this pre-execution decision does
        not claim to perform.
        """
        resources = resources_touched(tool_name, args)
        resource = resources[0] if resources else tool_name
        action = str(args.get("action") or self._default_action)
        user_id = caller.invoking_subject or caller.user_id

        try:
            raw = self._authorize(
                user_id=user_id,
                tool=tool_name,
                action=action,
                resource=resource,
                caller=caller,
                resources=resources,
                args=args,
            )
        except KeiProxyAuthorizeError as exc:
            logger.warning("kei-proxy authorize for %s denied: %s", tool_name, exc.reason_class)
            return self._decision(Action.DENY, _reason(exc.reason_class, exc.detail))
        except Exception as exc:
            # Unreachable proxy, timeout, transport error: fail closed. An
            # exception must never become an allow.
            logger.warning("kei-proxy authorize failed for %s, denying: %s", tool_name, exc)
            return self._decision(
                Action.DENY,
                reason=_reason("proxy_error", f"authorize failed: {exc}"),
            )

        try:
            response = AuthorizationResponse.from_result(raw)
        except Exception as exc:
            logger.warning("kei-proxy returned an unreadable response for %s: %s", tool_name, exc)
            return self._decision(
                Action.DENY,
                reason=_reason("malformed_response", f"unreadable response: {exc}"),
            )

        decision = response.decision.strip().lower()

        if decision in AFFIRMATIVE_DECISIONS:
            return self._decision(
                Action.ALLOW,
                reason=_reason("allow", response.reason),
                policy_id=response.policy_id,
            )

        enrollment = None
        connect = None
        if not decision:
            reason_class, detail = "no_decision", None
        elif decision == "deny":
            reason_class, detail = "deny", response.reason
            enrollment = response.enrollment
            connect = response.connect
        elif decision == "enrollment_required":
            reason_class = "enrollment_required"
            detail = response.reason or "enrollment is required before this call"
            enrollment = response.enrollment
            connect = response.connect
        else:
            reason_class = "unknown_decision"
            detail = f"{decision!r}" + (f" ({response.reason})" if response.reason else "")

        logger.debug("kei-proxy %s for %s", reason_class, tool_name)
        return self._decision(
            Action.DENY,
            reason=_reason(reason_class, detail),
            policy_id=response.policy_id,
            enrollment=enrollment,
            connect=connect,
        )

    def _authorize(
        self,
        *,
        user_id: str,
        tool: str,
        action: str,
        resource: str,
        caller: CallerContext,
        resources: list[str],
        args: dict[str, Any],
    ) -> Any:
        """Call a modern client, with a narrow path for legacy clients.

        Signature binding selects compatibility before making the call. This
        avoids retrying an authorization request after a client has already
        run it and raised a ``TypeError`` internally.
        """
        base_args = (user_id, tool, action, resource)
        context: dict[str, Any] = {
            "span_id": caller.span_id,
            "invoking_subject": user_id,
            "parent_span": caller.parent_span,
            "delegation_depth": caller.delegation_depth,
            "agent_id": caller.agent_id or caller.metadata.get("agent_id", ""),
            "agent_version": caller.agent_version or caller.metadata.get("agent_version", ""),
            "framework": caller.framework or caller.source,
            "tool_args_digest": tool_args_digest(args),
            "resources": resources,
            "workspace_id": caller.workspace_id or caller.metadata.get("workspace_id", ""),
        }
        authorize = self._client.authorize

        try:
            signature = inspect.signature(authorize)
        except (TypeError, ValueError):
            # Some extension/builtin callables do not expose a signature. Give
            # them the modern contract; any exception still fails closed above.
            return authorize(*base_args, **context)

        try:
            signature.bind(*base_args, **context)
        except TypeError as modern_error:
            try:
                signature.bind(*base_args)
            except TypeError:
                raise modern_error
            return authorize(*base_args)

        return authorize(*base_args, **context)

    def _decision(
        self,
        action: Action,
        reason: str,
        policy_id: str | None = None,
        enrollment: dict[str, Any] | None = None,
        connect: dict[str, Any] | None = None,
    ) -> Decision:
        """Assemble a Decision, attributing it to the proxy policy when known.

        ``redacted_args`` is left empty: ``AuditedToolClient`` merges it into
        the tool's arguments on a FILTER decision, so it carries argument
        overrides only. Resource lineage travels via :func:`resources_touched`,
        which the caller assembling the audit record calls directly.
        """
        return Decision(
            action=action,
            rule=policy_id or self.RULE,
            reason=reason,
            enrollment=enrollment,
            connect=connect,
            timestamp=datetime.now(),
        )
