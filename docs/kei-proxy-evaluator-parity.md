# KeiProxyEvaluator parity

`KeiProxyEvaluator` is the fail-closed `PolicyEvaluator` that asks
`kei-proxy authorize` before a tool call runs. Harnesses in every SDK language
need the same answer for the same proxy output. One table holds them to that.

## The shared table

| File | Role |
| --- | --- |
| `fixtures/kei/policy.v1.json` | Seeded, synthetic policy: one org, one workspace, one agent, three users (linked member, linked admin, unlinked chat user), three GitHub tools and their allow, deny and enrollment rules. |
| `fixtures/kei/authorize-cases.v1.json` | The parity table. Each case gives a tool call, the fake proxy's stdout, stderr and exit code, and the expected action, reason class, rule and enrollment (null means absent). Some cases also pin the exact argv. |
| `fixtures/kei/fake-kei-proxy.sh` | The fake kei-proxy. A test copies it into a temp dir next to the case's `stdout`, `stderr` and `exit_code` files (plus `hang` for the timeout case). It prints the canned answer and records its `argv` and `env`. It makes no network calls. |

Every language reads these files from the repo root; there are no mirrored
copies to drift. A change to any of them is a contract change: update all
three ports in the same stack.

## The contract

The kei-proxy authorize output (kei-connector-runtime `authorize.go`, #41):
stdout is one JSON object, `decision` plus optional `reason`, `service`,
`credential` and `enrollment`. It exits 0 for `allow` and `enrollment_required`,
and exits 1 for `deny`. Every error also exits 1, with nothing on stdout and a
diagnostic on stderr.

| Proxy result | Action | Reason class | Enrollment |
| --- | --- | --- | --- |
| `allow` / `permit` (any case), exit 0 | ALLOW | `allow` | — |
| `deny` | DENY | `deny` | carried when it is a JSON object |
| `enrollment_required` (legacy) | DENY | `enrollment_required` | carried when it is a JSON object |
| `decision` missing, null or `""` | DENY | `no_decision` | — |
| any other decision, or a non-string one | DENY | `unknown_decision` | — |
| `allow` with exit 1 | DENY | `exit_mismatch` | — |
| exit code other than 0 or 1 | DENY | `proxy_error` | — |
| exit 1 with empty stdout | DENY | `proxy_error` | — |
| exit 0 with empty stdout | DENY | `empty_response` | — |
| stdout not JSON, or JSON but not an object | DENY | `malformed_response` | — |
| binary missing or not executable | DENY | `proxy_unavailable` | — |
| no answer within the timeout | DENY | `proxy_timeout` | — |
| no `KEI_RUNTIME_TOKEN` (the proxy is not spawned) | DENY | `missing_token` | — |

Every reason reads `kei-proxy <class>` or `kei-proxy <class>: <detail>`.
`rule` is the output's `policy_id` (or `policy`) when present, else
`kei-proxy`.

Invariants every port asserts on every case:

- **Fail closed.** Only an explicit affirmative with exit 0 allows.
- **No leaks.** The enrollment claim URL, the allow-path credential, stderr
  and the runtime token never appear in a Decision reason or in a log line.
  Stdout and stderr are never logged.
- **Token by env only.** `KEI_RUNTIME_TOKEN` reaches the child through its
  environment and never through argv. The child sees only the allowlisted
  parent variables (`child_env.allowlist`) plus explicit extra env. The allowlist includes
  `KEI_RUNTIME_VERSION`, which the harness sets for the runtime. Secrets
  such as `DISCORD_TOKEN` and inherited `KEI_PROXY_*` identity variables are
  stripped.
- **Identity by flags.** `--user` is the invoking subject (the human). The
  delegation chain (`--parent-span`, `--delegation-depth`), agent version,
  framework, `--tool-args-digest` (SHA-256 of the sorted-key, ASCII-escaped,
  compact JSON of the args) and `--resources` are passed as flags. The argv
  is pinned by the table.
- **No `--agent-id`.** kei-proxy (0.1.11 and later) resolves the agent from the
  runtime installation, so no client sends `--agent-id` (`argv_forbidden`)
  even when the caller carries an agent id. Injected clients still receive
  it on the request.

## Ports

| Language | Evaluator | Subprocess client | Table test |
| --- | --- | --- | --- |
| Python | `pedro_agentware.kei.KeiProxyEvaluator` | `KeiProxyAuthorizeClient` | `python/tests/kei/authorize_cases_test.py` |

Python-only tests (the injected-client seam: legacy four-argument clients,
object-shaped results, arbitrary exceptions) stay in
`python/tests/kei/evaluator_test.py`. The fixture-integrity checks (every
derived case agrees with the seeded policy rule it names) run once, in
Python.
