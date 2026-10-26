# Agentware Rust SDK

This Cargo workspace is the scaffold for the Agentware Rust port. It mirrors
the Kei integration modules in `go/kei`; product behavior will be added in
follow-up work. Rust must replay the shared JSON contracts in
`testing/contracts/`, just like the Go, Python, and TypeScript SDKs.

The workspace MSRV is Rust 1.85.0. From this directory, run:

```sh
cargo fmt --all -- --check
cargo clippy --workspace --all-targets -- -D warnings
cargo test --workspace
```

The `agentware-kei::fixture::load_contract` helper resolves paths relative to
`testing/contracts/` at the repository root, so tests can consume the shared
fixtures without copying them into this crate.

## Run against `kei-proxy serve`

For a DVL run against a non-production Kei, start `kei-proxy serve` with its
runtime credential configured in the proxy's environment or deployment. This
crate does not load or print that credential; it sends requests only to the
Unix socket. Then run the example with a socket path and opaque `source` and
`external_id` values supplied by the harness:

```sh
KEI_RUNTIME_SOCKET_PATH=/run/kei-proxy/runtime.sock \
  cargo run -p agentware-kei --example governed_call -- \
  --source example --external-id user-1
```

The SDK passes `source` and `external_id` through as opaque strings, requests
one permitted and one denied tool call, and registers a small tool manifest
through the runtime socket. Mapping an identity provider (for example, Entra
`<tid>/<oid>`) to `source` and `external_id` is the harness's job and belongs
in the harness, not this crate. Attach the printed log to the DVL evidence; it
shows a shortened session ID and metadata-only audit count, and never logs call
arguments, tokens, or tool results.
