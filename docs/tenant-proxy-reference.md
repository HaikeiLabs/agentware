# Tenant-Side Distributed Proxy: Architecture Reference

**Status**: Authoritative reference. Replaces the older "KEI proxy integration"
narrative (formerly tracked as `kei-proxy-integration-prompt.md`), which
described only the local enforcement seam. This document is the canonical
tenant-side proxy architecture every worker must read before planning changes
to middleware, tool bindings, or the `kei/` integration surface.

**Source of truth**: captured in the shared Herdr wiki under *"Documentation
audit: tenant data must stay behind distributed proxy"* and the governed
connector decisions (`wiki search "tenant data distributed proxy"`). Trust the
code in `python/src/pedro_agentware/kei/` and this reference over older prose.

---

## The boundary in one paragraph

Execution happens **on the tenant side**. Agentware runs local tool middleware,
tracks delegation, resolves semantic tool bindings, and — for governed external
operations — calls out through a **distributed proxy** that is the
connector/provider runtime and the policy enforcement point. Kei (the control
plane) is only a **metadata catalog and an ABAC policy decision point**. What
crosses the control-plane boundary is metadata — registration, auth method and
*references*, workspace/agent/tool bindings, semantic tags, scopes/resources,
and audit metadata. **Provider payloads and results, customer content,
credentials, embeddings, and indexes never enter Kei.**

---

## What agentware provides

- **Local tool middleware** — policy enforcement, rate limits, filter/redact,
  and one audit record per tool call, fail-closed by default
  (`CallerContext.Trusted` defaults to `false`).
- **Delegation** — `InvokingSubject` is the human and is carried unchanged
  across every hop; `ParentSpan` / `DelegationDepth` record where a call sits.
- **Semantic tool bindings** — `tool_bindings` map a tool name to a connector
  plus routing config (`python/.../kei/config.py`). They are self-reported
  *routing metadata* and never grant permissions.
- **Proxy integration** — `KeiProxyEvaluator` (`kei/evaluator.py`) is the
  policy-enforcement seam: it authorizes a tool call against kei-proxy and
  fails closed on every path that is not an explicit `permit`/`allow`.

## Metadata-only invariant

The harness manifest (`kei-harness.json`) is versioned, non-secret metadata:

- `tool_bindings` — tool → connector routing, non-secret, no permissions.
- `secret_refs` — **non-secret reference identifiers only**. The wrapper never
  resolves them, and they must never be able to reveal credentials to the
  agent. The bootstrap secret (`KEI_RUNTIME_TOKEN`) is loaded separately from
  the environment or a secret provider and is rejected from the manifest.
- `workspace_id` is informational only; authority is server-derived.

Self-reported bindings never grant permissions; enforcement stays with the
middleware policy layer. A manifest that carries the bootstrap secret, or a
`secret_ref` that resolves to it, fails validation.

## The distributed proxy

The distributed proxy is the **connector/provider runtime** and the **policy
enforcement point**:

- It resolves bindings and auth (from metadata the control plane holds as
  references, never as materialized credentials).
- It invokes provider adapters and returns data **only inside the tenant
  runtime**.
- It is the explicit approval/governance boundary for agent actions that cross
  out of the local loop (a proxy/approval protocol carrying delegation context:
  `invoking_subject`, `agent_id`, tenant/workspace, `trace_id`, idempotency key,
  approval id).

## ABAC is a decision point, not a data service

ABAC (via Kei) is consulted for **metadata policy decisions only**:

- subject/object/operation/environment over **data-source resources**
  (read/data-source authorization, tenant/workspace/subject binding,
  lifecycle, scopes/resources, credential references, fail-closed decisions,
  audit/trace context).
- ABAC **decides**; it does **not** execute writes and does **not** serve or
  store credentials. Connectors are data-source integrations; GitHub/CRM/Linear
  *mutations* are agent capabilities executed in the harness/agentware/agents
  local tool loop, governed through the proxy boundary — not ABAC connectors.

## What never enters Kei

- Provider payloads and results
- Customer content
- Credentials (including materialized secrets and OAuth tokens)
- Embeddings
- Indexes

## Execution without the proxy

Local agent execution remains fully possible **without a direct ABAC connector
dependency**: local middleware evaluates policy and audits locally. The proxy
boundary is only required for **governed external operations** — actions that
cross out of the tenant to connectors/providers.

---

## Related documents

- `AGENTS.md` — mandatory wiki-first workflow and boundary preservation rules.
- `docs/harness-contract.md` — the contract third-party agent builders
  implement against; see its *Control-Plane Boundary* section.
- `docs/engineering-design.md` — the middleware SDK design; see the
  *Tenant-Side Distributed Proxy & Control-Plane Boundary* section.
- `README.md` — the overview; see the harness-contract bullet.
- Herdr wiki captures: `decision/documentation-audit_-tenant-data-must-stay-
  behind-distributed-proxy` and the governed-connector decisions
  (`wiki search "tenant data distributed proxy"`).
