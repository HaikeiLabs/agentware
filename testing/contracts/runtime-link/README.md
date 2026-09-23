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
| `lifecycle-redaction.json` | `runtime.link.*` lifecycle records serialize exactly the allowlisted keys. Tokens, argv, env, stderr, payloads, results, reasoning, content, and subjects are dropped by construction. |

This slice defines types and contracts only. Process supervision, the
`fake-kei-proxy.sh` scenario driver, and `scenarios/*.json` state sequences
arrive with the Go reference implementation (spec §9, A2). Nothing here talks
to the catalog: the only heartbeat path is harness → Agentware →
`kei-connector-runtime`.
