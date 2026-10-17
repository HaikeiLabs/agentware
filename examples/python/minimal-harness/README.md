# Minimal governed harness (Python)

The smallest possible agent harness that routes every tool call through
kei-proxy governance: one `echo` tool behind the
`KeiProxySocketAuthorizeClient`. It shows a governed **allow** and a governed
**deny** (with its `reason_code` and `hint`) in under a minute.

## Run it

```bash
cd python
uv run python ../examples/python/minimal-harness/main.py --fake
```

`--fake` starts the shared fake daemon
(`fixtures/kei/fake-kei-proxy-socket.py`) on a temp Unix socket, points
`KEI_RUNTIME_SOCKET_PATH` at it, and runs the demo. Output:

```
ALLOW tool=echo policy_id=fake-allow-echo
DENY tool=delete_everything reason_code=no_matching_policy hint=Add a policy for this tool: kei policies create
```

The fake daemon appends one JSON audit line per authorize to the audit file
(`--audit PATH`, or a temp file by default; its path is printed on stderr).

## Run against a real kei-proxy

Point the socket path at a live `kei-proxy serve` daemon and drop `--fake`:

```bash
export KEI_RUNTIME_SOCKET_PATH=/run/kei-proxy/runtime.sock
export KEI_RUNTIME_TOKEN=<your runtime token>
uv run python ../examples/python/minimal-harness/main.py
```

## How it fits the setup order

This example is the last step of the bring-up sequence:

1. `kei bot init` — create the runtime installation and its credential.
2. `kei harness add --kind custom` — register this harness.
3. `kei harness sync --harness custom --manifest tools.json` — register its tools.
4. `kei-proxy serve` — run the runtime daemon on the socket.
5. Call — this harness authorizes each tool call over the socket.
