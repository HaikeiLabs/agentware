# TypeScript: governed socket harness

The minimal example authorizes an allow and a deny through the local
`kei-proxy` socket. For package installation and supported versions, see the
repository [README](../../README.md); for the complete source, see
[`examples/typescript/minimal-harness`](../../examples/typescript/minimal-harness).
The example requires Node 22.18 or later.

## Install

```sh
npm install @haikeilabs/agentware
```

Run `kei-proxy serve` beside the harness and set `KEI_RUNTIME_SOCKET_PATH` and
`KEI_RUNTIME_TOKEN` in the harness process environment. The daemon owns the
user session and policy decision. Do not put the token in code, arguments, or
logs.

## Run the allow and deny example

From the repository root:

```sh
cd examples/typescript/minimal-harness
npm ci --prefix ../../../typescript
npm --prefix ../../../typescript run build
npm ci
npm start -- --fake
```

The example starts an in-process fake socket and prints one JSON result per
authorization, including the `reason_code` and `hint` on denial. Only proceed
to the tool handler for an explicit allow. A socket or protocol failure fails
closed. The full `reason_code`/`hint` reference is documented in the
`kei-connector-runtime` README.

For the next layer, use agentware middleware to guard tool execution and its
guardrails to bound your agent loop. See the [middleware guide](../typescript/README.md).
