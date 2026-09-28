# @haikeilabs/agentware

`@haikeilabs/agentware` provides tool and agent-runtime primitives for applications
that already own their agent loop, channel integration, identities, and tool
handlers.

## Async tool wrapper

Use `wrapTool` to make an existing tool callable through a consistent async
boundary. It preserves the host's input, output, and trusted context types;
Agentware does not infer identity or privileges from tool arguments.

```ts
import { wrapTool } from "@haikeilabs/agentware";

type Selection = { queryId: string };
type TrustedContext = { principalId: string; verifiedGrant: string };

const queryModel = wrapTool(
  async (selection: Selection, context: TrustedContext, signal: AbortSignal) => {
    // Keep the application's existing authorization and provider guard here.
    return runExistingTool(selection, context.verifiedGrant, signal);
  },
  { name: "semantic-model-query", timeoutMs: 5_000 },
);

const result = await queryModel(
  { queryId: "commission-summary" },
  trustedContext,
  { signal: request.signal },
);
```

The wrapper propagates host cancellation and deadlines to the handler's
`AbortSignal`. It raises `ToolAbortedError` when the host cancels and
`ToolTimeoutError` when its configured deadline expires. Lifecycle hooks expose
only tool name, outcome, and duration; they intentionally exclude raw inputs
and host context.

## Integration boundary

Register only the wrapped handler with an agent framework or tool registry.
Keep channel ingress, identity verification, delegated credentials, and
domain-specific authorization in the host application. Agentware is not a
policy engine; a policy gate can wrap this same boundary later.

## kei-proxy policy evaluator

`KeiProxyEvaluator` runs `kei-proxy authorize` before each tool call and fails
closed on anything other than an explicit `allow`/`permit` with exit 0.
`evaluate` is asynchronous: the event loop keeps serving other requests while
kei-proxy answers. Await the decision before running the tool.

`KeiProxyAuthorizeClient` spawns the binary by absolute path (a bare name is
resolved once against the parent `PATH`) with an allowlisted environment only:
`KEI_RUNTIME_TOKEN`, `KEI_RUNTIME_CONTROL_PLANE_URL`, `KEI_RUNTIME_VERSION` and
the other `AUTHORIZE_CHILD_ENV_ALLOWLIST` names. No `PATH`, no `HOME`, and the
token never goes in argv. The child runs in its own process group, and a
timeout SIGKILLs the whole group. Set `expectedSha256` to pin the binary: before
every spawn it is opened with `O_NOFOLLOW` and re-hashed, and any drift denies
with `kei-proxy pin_mismatch`.

```typescript
import { Action, KeiProxyAuthorizeClient, KeiProxyEvaluator } from "@haikeilabs/agentware";

const evaluator = new KeiProxyEvaluator(
  new KeiProxyAuthorizeClient({
    executable: "/usr/local/bin/kei-proxy",
    expectedSha256: process.env.KEI_PROXY_SHA256, // optional pin
    timeoutMs: 10_000,
  }),
);

const decision = await evaluator.evaluate(
  "github.get_issue",
  { owner: "acme", repo: "pipe" },
  caller,
);
if (decision.enrollment) {
  // deny + enrollment claim link: reply to the user privately; never log it.
}
if (decision.action !== Action.ALLOW) return;
```

Reasons read `kei-proxy <class>[: <detail>]` with a class from
`KEI_PROXY_REASON_CLASSES`. Because `evaluate` returns a `Promise`, the
evaluator is not a synchronous `PolicyEvaluator` and cannot be passed to
`MiddlewareImpl.withPolicy`. The behaviour matches the Python and Go ports
case for case; see `docs/kei-proxy-evaluator-parity.md`.

## Maintainer releases

Releases are deliberately manual and use npm staged publishing. Do not run
`npm publish` from a workstation or add a registry token to CI.

1. Update `typescript/package.json` and `typescript/package-lock.json` to the
   intended semantic version in a pull request, then merge it to `main`.
2. In GitHub Actions, run **Stage Agentware npm package** from `main`, entering
   that exact version (for example, `0.1.1`). The workflow verifies the version,
   creates the immutable `agentware-v<version>` tag, runs checks, and submits
   the package with `npm stage publish`.
3. Approve the GitHub `release` environment when prompted.
4. Review the staged package in npm and approve it. The approving maintainer
   needs npm publish access and 2FA enabled for write actions; a WebAuthn
   security key is supported.

The final npm approval is intentional: it is the human gate that makes the
staged version publicly available.
