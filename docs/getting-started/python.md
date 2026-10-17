# Python: governed socket harness

For a real Kei runtime setup walkthrough, see the
[runtime setup guide](runtime-setup.md) and [deny hints](../deny-hints.md).

The minimal example authorizes an allow and a deny through the local
`kei-proxy` socket. For package installation and supported versions, see the
repository [README](../../README.md); for the complete source, see
[`examples/python/minimal-harness`](../../examples/python/minimal-harness).

## Install

```sh
pip install haikeilabs-agentware
```

Run `kei-proxy serve` beside the harness and set `KEI_RUNTIME_SOCKET_PATH` and
`KEI_RUNTIME_TOKEN` in the harness process environment. The daemon owns the
user session and policy decision. Do not put the token in code, arguments, or
logs.

## Run the allow and deny example

From the repository root:

```sh
cd python
uv run python ../examples/python/minimal-harness/main.py --fake
```

The example starts `fixtures/kei/fake-kei-proxy-socket.py`, then prints:

```text
ALLOW tool=echo policy_id=fake-allow-echo
DENY tool=delete_everything reason_code=no_matching_policy hint=Add a policy for this tool: kei policies create
```

Only proceed to the tool handler for an explicit allow. A denial is returned
with its reason metadata; a socket or protocol failure fails closed. The full
`reason_code`/`hint` reference is documented in the `kei-connector-runtime`
README.

For the next layer, use agentware middleware to guard tool execution and its
guardrails to bound your agent loop. See the [middleware guide](../python/README.md).
