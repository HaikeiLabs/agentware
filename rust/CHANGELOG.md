# Changelog

## Unreleased

- Add the `kei-proxy authorize` CLI fallback (`evaluator::CliClient`), the
  socket-or-CLI selector (`evaluator::select_authorization_client`) and the
  shared authorize-cases fixture replay.
- Fix `tool_args_digest` to sort object keys at every depth like Go, so
  digests match across SDKs; follow Go's decision mapping for enrollment and
  connect (only as JSON objects, only on deny and enrollment_required).
- Rename the crate to `haikeilabs-agentware` (import `haikeilabs_agentware`) and version it with the shared Agentware version (0.16.0).
- Add `list_models`, `model_config`, `list_model_profiles`, `chat_completion`, `stream_chat_completion` and `release_credential` to the runtime socket client, replaying the shared contract cases.
- Add a generic governed-call example and offline Unix socket end-to-end test.
- Add fail-closed Rust governed tool middleware with FILTER argument replacement and metadata-only audit digests.
- Add a fail-closed Rust Kei proxy evaluator with decision metadata, caller context, and deny hints.
- Add Rust tool-manifest v3 types and Kei runtime registration/sync.
- Add a Rust Unix socket client for kei-proxy sessions, authorization, readiness, and policy bundle status.
- Add the Rust Cargo workspace scaffold, Kei integration module stubs, and a
  loader for shared contract fixtures.
- Add RuntimeLink configuration, contract helpers, and supervised heartbeat and audit collector child processes.
