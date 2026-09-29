# Fail-Closed Design

This document describes the fail-closed design that governs every path through
KeiProxyEvaluator and the middleware layer. It is a companion to the parity
table in `docs/kei-proxy-evaluator-parity.md` and to the language READMEs in
`docs/{go,python,typescript}/README.md`.

## Decision table

KeiProxyEvaluator translates the raw `kei-proxy authorize` output into an
agentware `Decision`. Only an explicit affirmative (`allow` or `permit`) with
exit code 0 produces `Action.ALLOW`. Everything else — every error, every
surprising output, every missing field — produces `Action.DENY`.

The full table lives in `docs/kei-proxy-evaluator-parity.md` and is enforced
by the shared fixtures in `fixtures/kei/authorize-cases.v1.json`. A summary:

| Proxy result | Action | Reason class |
|---|---|---|
| `allow` / `permit`, exit 0 | ALLOW | `allow` |
| `deny` | DENY | `deny` |
| `enrollment_required` | DENY | `enrollment_required` |
| `decision` missing, null, or `""` | DENY | `no_decision` |
| any other decision string | DENY | `unknown_decision` |
| `allow` with exit 1 | DENY | `exit_mismatch` |
| exit code other than 0 or 1 | DENY | `proxy_error` |
| exit 1 with empty stdout | DENY | `proxy_error` |
| exit 0 with empty stdout | DENY | `empty_response` |
| stdout not JSON or not an object | DENY | `malformed_response` |
| binary missing or not executable | DENY | `proxy_unavailable` |
| no answer within the timeout | DENY | `proxy_timeout` |
| no `KEI_RUNTIME_TOKEN` | DENY | `missing_token` |
| pinned binary drifted | DENY | `pin_mismatch` |
| `authorize()` raised/threw | DENY | `proxy_error` |

Every reason reads `kei-proxy <class>` or `kei-proxy <class>: <detail>`.

## `KEI_PROXY_DISABLED`: fail-closed, never an allow switch

`KEI_PROXY_DISABLED=true` is a **valid** deployment-time signal. It does not
bypass the evaluator or shortcut to ALLOW. The only correct behaviour when
`KEI_PROXY_DISABLED` is true is to **deny every governed tool call** — the same
as any other unreachable or missing policy endpoint. The variable exists so that:

- A runtime without a connected Kei installation can still start and serve
  non-governed (local) tool calls.
- The startup path can warn that governed calls will be denied, rather than
  hang on a missing binary.

A "disable" switch that implicitly allows would violate the fail-closed
invariant. If no policy endpoint is available, the safe default is deny.

### Why no disable-allow switch exists

The fail-closed invariant is one decision: **"no decision" means deny**. Every
layer — the evaluator, the middleware, the `CallerContext` defaults, the
`Trusted` field — is built around this rule. Adding a "disable and allow
everything" switch would introduce a second, contradictory invariant that
bypasses every security control in the stack. Such a switch would be:

- **Untestable in production** — a misconfiguration silently permits every call.
- **Un-auditable** — no decision is recorded because the evaluator is never
  invoked.
- **Impossible to revoke** — a compromised runtime could self-disable.

No language port provides such a switch.

## Governed vs non-governed tool calls

Tool calls fall into one of two categories. The distinction is a deployment
choice, not a code-level flag: a harness decides which tools route through
`KeiProxyEvaluator` by how it wires its `PolicyEvaluator`.

### Governed (through KeiProxyEvaluator)

A governed call goes through `KeiProxyEvaluator`, which runs
`kei-proxy authorize` — the policy engine decides (allow / deny / filter),
every decision is audited, and the caller context is sent with full delegation
attribution.

Governed tools include:

- **Connector invokes** — any tool whose execution reads or writes tenant data
  through a data-source connector (GitHub, Linear, Google Drive, S3, HTTP API,
  CRM, etc.).
- **Write / egress tools** — tools that mutate state, emit data, or reach
  external systems (send email, create ticket, deploy, invoke webhook).
- **Tools touching credentials** — any tool whose arguments or results contain
  secrets, tokens, or keys.
- **Any tool the operator chooses to govern** — the boundary is configurable
  per deployment.

### Non-governed (local only)

A non-governed call uses a local `PolicyEvaluator` (e.g. `SimplePolicyEvaluator`
or the explicit deny-all evaluator) or no evaluator at all. It never reaches
kei-proxy. The local middleware chain still applies rate limits, conditions,
and rules, and still audits the decision.

Non-governed tools include:

- **Local computation** — pure functions, math, string manipulation.
- **Deterministic transforms** — format conversion, data reshaping.
- **Harmless reads of already-loaded data** — tools that operate on context
  the agent already holds.
- **Tools running before the proxy is available** — startup, health check,
  configuration.

The same `AuditedToolClient` wrapper records every decision either way,
so the audit trail is contiguous regardless of the governance path.

### Why the distinction matters

Governed calls place a hard policy boundary between the agent and every
external system. If kei-proxy is unreachable, misconfigured, or returns a
surprising result, the call is denied — never allowed. Non-governed calls
let a local agent loop operate without a Kei dependency, but they must never
touch tenant data, external systems, or credentials.

A harness that routes all tools through `KeiProxyEvaluator` is maximally
secure; one that routes none is fully local and never requires Kei. Most
deployments govern connector and write tools while keeping local computation
non-governed.

## Testing: injecting a fake evaluator

Tests that need a controlled allow or deny inject a fake `AuthorizationClient`
behind the evaluator, not a fake proxy subprocess. Each language port defines
the seam differently; the snippets below show the pattern using real types.

### Go

The `evaluator.Client` interface has one method:

```go
type Client interface {
    Authorize(ctx context.Context, req AuthorizeRequest) (map[string]any, error)
}
```

A test adapter wraps a function literal:

```go
type clientFunc func(context.Context, AuthorizeRequest) (map[string]any, error)

func (f clientFunc) Authorize(ctx context.Context, req AuthorizeRequest) (map[string]any, error) {
    return f(ctx, req)
}

// In a test:
eval := evaluator.NewKeiProxyEvaluator(
    clientFunc(func(ctx context.Context, req evaluator.AuthorizeRequest) (map[string]any, error) {
        return map[string]any{"decision": "allow"}, nil
    }),
)
decision := eval.Evaluate("github.read", nil, callerCtx)
// decision.Action == middleware.ActionAllow
```

Return an error to simulate a proxy failure:

```go
clientFunc(func(ctx context.Context, req evaluator.AuthorizeRequest) (map[string]any, error) {
    return nil, errors.New("proxy unreachable")
})
```

### Python

The `AuthorizationClient` protocol accepts four positional arguments plus
keyword context:

```python
class FakeProxy:
    def authorize(self, user_id: str, tool: str, action: str, resource: str, **context):
        return {"decision": "allow"}

evaluator = KeiProxyEvaluator(FakeProxy())
decision = evaluator.evaluate("github.read", {"owner": "acme"}, caller)
# decision.action == Action.ALLOW
```

Raise an exception to simulate failure:

```python
class FailingProxy:
    def authorize(self, user_id, tool, action, resource, **context):
        raise ConnectionError("proxy unreachable")

evaluator = KeiProxyEvaluator(FailingProxy())
decision = evaluator.evaluate("github.read", {}, caller)
# decision.action == Action.DENY
```

### TypeScript

The `KeiProxyAuthorizationClient` interface has one async method:

```typescript
const client: KeiProxyAuthorizationClient = {
    authorize(request) {
        return { decision: "allow" };
    },
};

const evaluator = new KeiProxyEvaluator(client);
const decision = await evaluator.evaluate("github.read", {}, caller);
// decision.action === Action.ALLOW
```

Throw or return a rejected promise to simulate failure:

```typescript
const failingClient: KeiProxyAuthorizationClient = {
    authorize() {
        throw new Error("proxy unreachable");
    },
};
```

## Local development with a local Kei stack

During local development the `kei-proxy` binary may not be installed or the
runtime token may not be set. A harness can:

1. **Use a local `PolicyEvaluator` instead of `KeiProxyEvaluator`** for
   non-governed tools. The middleware interface accepts any evaluator, so a
   liberal local policy (allow-all, with rate limits) keeps the dev loop fast.
2. **Run the local Kei stack** (`docker compose --profile mock up`) which
   provides a mock kei-proxy endpoint on :8081 and a seeded policy. Set
   `KEI_RUNTIME_TOKEN` and point `KEI_PROXY_REGISTRY` at the mock.
3. **Inject a fake evaluator** (as shown above) in integration tests that
   exercise the governed path without a real proxy.

No development workflow ever needs to disable fail-closed behaviour.
