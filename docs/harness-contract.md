# Third-Party Harness Contract

This contract describes the requirements for a custom harness in any language.
The harness may use an agentware port or call the local `kei-proxy` runtime
socket directly. It must keep the invoking user's identity with each governed
tool call and ask the proxy for an authorization decision before execution.

The SDK (Agentware) path is one of Kei's six user-facing harness types. The
other five types and their integration paths are listed in the section below.

## Registration and tool manifest

Register a custom harness with `kei harness add --kind custom`. Register the
tools it exposes with `kei harness sync --harness custom --manifest tools.json`.
The manifest describes tools; it does not grant permission. Keep it as
non-secret metadata. In particular, `tool_bindings` and `secret_refs` contain
routing information and opaque references only. Never put `KEI_RUNTIME_TOKEN`
or any other credential in a manifest.

## Identity and context

For each authorization request, preserve the authenticated runtime context and
send the caller context that belongs to the invocation:

- `user_id` identifies the invoking user; `invoking_subject` preserves that
  same human identity across delegation.
- `framework` identifies the harness/framework making the call.
- The runtime token establishes the workspace and installation scope. Do not
  treat caller-supplied workspace or installation values as authority.
- Do not send `agent_id` to `authorize`. Agent identity is discovered from the
  runtime identity event when the harness needs it; it is not an authorization
  request field.

A missing caller identity or authenticated runtime context must fail closed.
Do not infer identity from prompt text or an agent-supplied environment value.

## Authorize every governed tool call

Send each governed tool call to `kei-proxy authorize` through the local Unix
socket before executing it. The proxy is the connector/provider runtime and
policy enforcement point. Treat the result as `allow` or `deny`; execute the
tool only for an explicit allow/permit decision. Deny on an unknown or missing
decision, an unreachable proxy, a missing credential, or an expired token.
Never fall back to ungoverned execution when authorization cannot complete.

A denial includes a machine-readable `reason_code` and a user-facing `hint`.
Preserve and surface those values so the user can understand the next step.
The reason and hint wording contract lives in
[`deny-hints.md`](deny-hints.md).

## Audit lineage and data boundaries

Record each authorization outcome with its harness/framework and the invoking
user preserved through delegation. Keep the decision and its reason available
for audit. Do not log the runtime token or other credentials.

The harness and agentware run on the tenant side. Kei is a metadata-only
control plane: provider payloads and results, customer content, credentials,
embeddings, and indexes never enter Kei. The proxy may use metadata and
credential references from the control plane, but resolves credentials and
executes providers within the tenant runtime. See
[`tenant-proxy-reference.md`](tenant-proxy-reference.md) for the boundary.

## Language guides and minimal harnesses

- Go: [`docs/go/README.md`](go/README.md) and [`examples/go/minimal-harness/README.md`](../examples/go/minimal-harness/README.md).
- Python: [`docs/python/README.md`](python/README.md) and [`examples/python/minimal-harness/README.md`](../examples/python/minimal-harness/README.md).
- TypeScript: [`docs/typescript/README.md`](typescript/README.md) and [`examples/typescript/minimal-harness/README.md`](../examples/typescript/minimal-harness/README.md).

## Python helper

Python provides optional helper types for assembling a harness. These are
Python APIs, not requirements for Go or TypeScript implementations.

`HarnessContract`, `HarnessManifest`, and `validate_contract` are Python
helpers. See the Python implementation and tests for their exact signatures
and behavior. Python integrations can also use `KeiProxyEvaluator` and the
Python runtime socket client; verify those APIs against the Python source and
tests.

## Kei harness types

Kei has six user-facing harness types:

- **SDK (Agentware)** — a harness built on Agentware that uses
  `KeiProxyEvaluator` → `kei-proxy authorize`, fail closed. Covers
  pydantic-ai, Gemini SDK, AWS AgentCore, and bespoke loops. API kind
  `custom`.
- **Claude Code** — native permission files + hook.
- **Codex** — native permission files + hook.
- **OpenCode** — native permission files + hook.
- **Pi** — own package + local-socket adapter.
- **Open WebUI** — own Python library (`openwebui-kei`), not the SDK path.

For the authoritative tenant-side proxy architecture, see
[`tenant-proxy-reference.md`](tenant-proxy-reference.md).
