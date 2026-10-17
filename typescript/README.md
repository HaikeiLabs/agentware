# @haikeilabs/agentware

`@haikeilabs/agentware` provides tool and agent-runtime primitives for applications
that already own their agent loop, channel integration, identities, and tool
handlers.

## Governed harness quick start: socket client first

For an executable allow/deny walkthrough through the `kei-proxy` socket, see
the [TypeScript getting started guide](../docs/getting-started/typescript.md)
and the [`examples/typescript/minimal-harness`](../examples/typescript/minimal-harness)
example. The install lines and compatibility table are in the
[repository README](../README.md). `KeiProxyAuthorizeClient`
(`kei-proxy authorize` subprocess) is deprecated; use the socket client.

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

## Deprecated: subprocess kei-proxy policy evaluator

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
import {
  AuditedToolClient,
  createCallerContext,
  KeiProxyAuthorizeClient,
  KeiProxyEvaluator,
} from "@haikeilabs/agentware";

const evaluator = new KeiProxyEvaluator(
  new KeiProxyAuthorizeClient({
    executable: "/usr/local/bin/kei-proxy",
    expectedSha256: process.env.KEI_PROXY_SHA256, // optional pin
    timeoutMs: 10_000,
  }),
);

const caller = createCallerContext({
  user_id: "user-123",
  invoking_subject: "user-123",
  trusted: false,
});
const client = new AuditedToolClient("my-harness", async (toolName, args) => {
  const output = await runTool(toolName, args);
  return [output, true, ""];
}).withPolicy(evaluator);

const [output, allowed, error] = await client.execute(
  "github.get_issue",
  { owner: "acme", repo: "pipe" },
  caller,
);
if (!allowed) throw new Error(error);

const child = caller.delegate("subagent-1");
// child.invoking_subject remains "user-123" and child.delegation_depth is 1.
```

Reasons read `kei-proxy <class>[: <detail>]` with a class from
`KEI_PROXY_REASON_CLASSES`. Because `evaluate` returns a `Promise`, the
evaluator cannot be passed to `MiddlewareImpl.withPolicy`. Use
`AuditedToolClient.withPolicy(evaluator)` to await it, gate execution, and
record the decision in one audited call. The behavior matches the Python and
Go ports case for case; see `docs/kei-proxy-evaluator-parity.md`.

## MCP route client

Use `Client` from `@haikeilabs/agentware` to call a connector through the local
`kei-proxy` MCP route. The client supports `tools/list` and `tools/call` over
the configured Unix socket or loopback TCP listener:

```typescript
import { Client } from "@haikeilabs/agentware";

const client = new Client("github-connector");
const tools = await client.listTools();
const result = await client.callTool("get_issue", { id: "42" });
```

`KEI_MCP_ROUTE_KEY` is read from the environment for each request and is never
cached or logged. The client rejects non-loopback hosts; provider traffic stays
in the tenant runtime.

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
