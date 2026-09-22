# Action-Tool / Data-Source-Connector Boundary

This note documents the separation between **agentware** (`pedro-agentware`) and
the out-of-band enforcement/execution layers, and pins the invariants with
contract tests. It is the agentware-side statement of the authoritative
architecture: **tenant-side proxy/data-plane execution, with Kei as a metadata
catalog and ABAC policy-decision-point (PDP) only.**

## Roles

| Layer | Role | Owns |
|-------|------|------|
| **Kei** | Metadata catalog + ABAC PDP | registration metadata, auth method/reference, workspace/agent/tool bindings, semantic tags, scopes/resources, audit metadata. **Never** provider payloads, result data, tool content, indexes, embeddings, or customer credentials. |
| **Proxy** | PEP / orchestrator / provider-connector runtime | resolves bindings + auth, invokes provider adapters, returns data **only inside the tenant runtime** (data residency). |
| **Agentware** | Local governance + modeling | models tool-to-data-source bindings, opaque auth references, local policy, and delegation. **No** provider execution, **no** secret resolution, **no** network decision point of its own. |

Agentware is deliberately the *metadata + local-policy* side. It can run a fully
governed tool loop with zero dependence on Kei or the proxy, and it can be
*optionally* wired to the proxy through a `PolicyEvaluator` adapter — but it
never resolves connector credentials or executes providers itself.

## Invariants (pinned by contract tests)

1. **`tool_bindings` are routing metadata and never grant permission.**
   A binding names which connector serves a tool (`tool_name`, `connector_id`,
   `config`). It carries no permission/action/read taxonomy, and the enforcement
   path never reads it. A bound tool is still denied by a deny-all local policy.
   *`python/tests/action_tool_boundary_test.py::TestBindingsNeverGrantPermission`*

2. **Connector `secret_refs` are opaque and never resolved or exposed.**
   A ref is `{source, key}` — an identifier, not a credential. The config layer
   reads no environment, retains no smuggled value, and exposes no resolver.
   Resolution happens out-of-band (in the proxy). The bootstrap secret
   (`KEI_RUNTIME_TOKEN`) is the one secret agentware handles, and only to hand
   to the proxy via the environment.
   *`::TestSecretRefsAreOpaque`*

3. **Local policy/middleware governs an isolated tool loop without a direct ABAC
   connector dependency.**
   `AuditedToolClient` + a local `Policy` enforces and audits with no proxy, no
   kei, no ABAC. The `middleware` package does not import the kei module.
   *`::TestLocalLoopHasNoABACDependency`*; Go:
   `go/middleware/action_tool_boundary_test.go::TestIsolatedLocalLoopSeparatesActionFromRead`

4. **Optional proxy governance is an explicit adapter boundary.**
   The middleware's only governance seam is the `PolicyEvaluator` interface. The
   proxy is reached, if at all, through an injected `AuthorizationClient` behind
   `KeiProxyEvaluator`; the middleware never spawns a subprocess or talks to a
   proxy directly. A local policy and the proxy evaluator are interchangeable
   behind that interface.
   *`::TestProxyIsAnOptionalAdapterBoundary`*

5. **CallerContext delegation preserves the original subject and trace
   attribution.**
   `invoking_subject` (the human) is carried unchanged across every delegation
   hop; `parent_span` and `delegation_depth` record position in the chain. A
   subagent running under its own service id still authorizes and audits as the
   human.
   *`::TestDelegationPreservesSubjectAtTheBoundary`*; Go:
   `::TestIsolatedLocalLoopPreservesDelegationInAudit`

6. **Action tools and read connector bindings remain conceptually separate.**
   The binding does not classify a tool as action vs. read; that distinction is
   policy's concern. Local policy can allow a read tool and deny an action
   (mutation) tool purely by rule, independent of any binding.
   *`::TestActionToolsAndReadBindingsStaySeparate`*

## What agentware does NOT do

- Grant permission from self-reported tool bindings.
- Resolve or retain connector secret values (customer credentials).
- Require Kei/ABAC to run a governed local tool loop.
- Execute providers or own the proxy subprocess (tenant-side data plane).
- Send provider payloads, result data, or tool content to Kei.

## Not frozen

The KEI **taxonomy** — semantic tags and the action-vs-read classification — is
still in progress. This note and its tests pin the *boundary* (what agentware
does and does not do), not the taxonomy vocabulary. Do not add manifest fields
for tags/classification here; that is the taxonomy correction's scope.

## References

- Contract tests: `python/tests/action_tool_boundary_test.py`,
  `go/middleware/action_tool_boundary_test.go`.
- Existing coverage: `python/tests/caller_context_test.py`,
  `python/tests/kei/{config,evaluator,proxy,contract}_test.py`,
  `python/tests/third_party_harness_test.py`,
  `go/middleware/{delegation_contract,tool_client,middleware}_test.go`.
- Harness contract: `docs/harness-contract.md`.
