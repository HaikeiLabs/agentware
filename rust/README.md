# Agentware for Rust

`haikeilabs-agentware` is the Rust crate of [Agentware](https://github.com/HaikeiLabs/agentware),
the open-source SDK Haikei Labs builds its own agents with. It shares one
version with the Go, Python and TypeScript SDKs and replays the same JSON
contracts in `testing/contracts/`.

It covers the Kei integration surface:

- the `kei-proxy serve` runtime socket client: sessions, authorize, models,
  model profiles, chat completions and credential release
- `KeiProxyEvaluator`, which asks Kei to allow or deny each tool call over the
  socket, or through the one-shot `kei-proxy authorize` CLI when no socket is
  configured, and fails closed
- tool manifest sync
- local middleware: policy, rate limits, guardrails and audit

```sh
cargo add haikeilabs-agentware
```

Import it as `haikeilabs_agentware`. See the
[docs](https://docs.haikeilabs.com/agentware/reference/languages) for what each
language supports.

## Development

The workspace MSRV is Rust 1.85.0. From this directory, run:

```sh
cargo fmt --all -- --check
cargo clippy --workspace --all-targets -- -D warnings
cargo test --workspace
```

The `haikeilabs_agentware::fixture::load_contract` helper resolves paths relative to
`testing/contracts/` at the repository root, so tests can consume the shared
fixtures without copying them into this crate.

## Run against `kei-proxy serve`

To try it against a non-production Kei, start `kei-proxy serve` with its
runtime credential configured in the proxy's environment or deployment. This
crate does not load or print that credential; it sends requests only to the
Unix socket. Then run the example with a socket path and opaque `source` and
`external_id` values supplied by the harness:

```sh
KEI_RUNTIME_SOCKET_PATH=/run/kei-proxy/runtime.sock \
  cargo run -p haikeilabs-agentware --example governed_call -- \
  --source example --external-id user-1
```

The SDK passes `source` and `external_id` through as opaque strings, requests
one permitted and one denied tool call, and registers a small tool manifest
through the runtime socket. Mapping an identity provider (for example, Entra
`<tid>/<oid>`) to `source` and `external_id` is the harness's job and belongs
in the harness, not this crate. The example log shows a shortened session ID
and a metadata-only audit count, and never logs call arguments, tokens, or tool
results.
