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
