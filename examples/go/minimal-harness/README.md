# Minimal Go Harness

A minimal governed tool harness demonstrating policy enforcement through
the kei-proxy runtime socket.

## Layout

- `main.go` — wiring: selects an auth client, registers the echo tool, calls it
- `harness/harness.go` — logic package (no main-package code)
- `harness/harness_test.go` — unit tests with a fake socket client

## How it works

1. The harness selects an `evaluator.Client` via `runtimesocket.SelectAuthorizationClient`
   (Unix socket if `KEI_RUNTIME_SOCKET_PATH` is set, else `kei-proxy authorize` subprocess).
2. Each tool call goes through `KeiProxyEvaluator.Evaluate`, which consults
   the policy endpoint.
3. `CallerContext.Trusted` defaults to **false** (fail-closed). A deny or
   unknown decision returns an error; the tool never runs.
4. On allow, the registered handler executes.

## Run

```bash
cd examples/go/minimal-harness

# With kei-proxy socket running:
go run .

# Override the source name:
go run . my-agent

# Point to a specific socket (overrides KEI_RUNTIME_SOCKET_PATH):
go run . --socket /run/kei-proxy/runtime.sock
```

For the registered runtime setup, use the `installation_id` returned by
`kei bot init` and run:

```sh
kei harness add --kind custom --installation INSTALLATION_ID
```

The CLI uses the agent assigned to that installation by default. If none is
assigned, add one with `kei bot agents add --installation INSTALLATION_ID
--agent AGENT_ID --default` first. See the [runtime setup guide](../../../docs/getting-started/runtime-setup.md)
for socket tool registration, group policies, and audit entries.

## Tests

```bash
cd examples/go/minimal-harness
GOTOOLCHAIN=go1.26.9 go test ./...
```

The test replay filee clients that return `allow` / `deny` / `unknown`
without needing a real daemon.
