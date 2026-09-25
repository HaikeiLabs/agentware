# RuntimeLink contract fixtures

Shared, language-neutral fixtures for the harness → Agentware `RuntimeLink` →
`kei-connector-runtime` heartbeat contract
(`docs/specs/runtime-heartbeat-liveness.md`, HAI-141). Go
(`go/kei/runtimelink`), Python (`pedro_agentware.kei.runtime_link`) and
TypeScript (`typescript/src/kei/runtimeLink.ts`) each load every file here and
must produce identical results. A change to any fixture is a contract change:
update all three languages in the same PR.

| File | Asserts |
| --- | --- |
| `enums.json` | The closed sets: harness kinds, link states, failure classes, beat outcomes, lifecycle event names, SDK languages, config error codes, the child env allowlist, and size bounds. |
| `config-cases.json` | `ConfigFromEnv` defaults, clamps, and fail-closed validation. Each case gives an env map (plus an optional harness-envelope argument) and either the normalized config or an error code. The token value is a canary and must never appear in a config or an error. |
| `backoff-cases.json` | Full-jitter restart backoff math in integer milliseconds. |
| `wire/child-events.json` | The v1 child stdout JSONL parser: accepted events keep only allowlisted, bounded fields; unknown events are ignored; unknown `v` or outcome is `contract_mismatch`; lines over 4 KiB are `line_too_long`. |
| `exit-code-cases.json` | `handleChildExit` maps child exit codes 0–4+ to `FailureClass` and terminal/restart behaviour. Exit 0 is a clean restart; 1 is transient (`runtime_unavailable`); 2 is terminal (`contract_mismatch`); 3 is terminal (`installation_unauthorized`); 4+ is terminal (`config_invalid`). |
| `child-env-allowlist.json` | `buildCommand` passes through only vars in `ChildEnvAllowlist` + the two SDK-injected vars. Canary secrets (9 vars) must be stripped. Maximum env entries: 13. |
| `lifecycle-redaction.json` | `runtime.link.*` lifecycle records serialize exactly the allowlisted keys. Tokens, argv, env, stderr, payloads, results, reasoning, content, and subjects are dropped by construction. |

The Go reference and TypeScript SDK include process supervision; TypeScript
exposes `newRuntimeLink(config, options)` and accepts an injectable child
factory for deterministic integration tests. Python currently exposes the
contract protocol; the RuntimeLink API and fixtures were captured in the shared
wiki for the Python worker to mirror. Changes to the runtime API, child
environment, or wire events must be captured in the wiki and kept aligned
across SDKs. Nothing here talks to the catalog:
the only heartbeat path is harness → Agentware → `kei-connector-runtime`.
