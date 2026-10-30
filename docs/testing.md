# Testing and CI

Agentware ships the same library in Go, Python, TypeScript and Rust. The
languages are held together by shared fixtures under `fixtures/`: every
implementation replays the same cases, and CI fails when one of them quietly
stops doing so. This page covers how that is enforced, how to add a case,
and how the exclusions list works.

## Two tiers

CI is split by what the code is allowed to touch.

**Tier 1: every PR, GitHub-hosted runners.** The repository is public, so
`ubuntu-latest` runners cost nothing. Everything here runs on untrusted PR
code, so it gets no secrets, no self-hosted runners, no
`pull_request_target`, and `permissions: contents: read`. Actions are pinned
to full commit SHAs.

- `conformance.yml`: the fixture conformance matrix (Go, Python, TypeScript,
  Rust) and the equal-coverage gate. It has no path filter, so it reports on
  every PR and can be a required check.
- `go-test.yml`, `python-test.yml`, `ci-python.yaml`, `typescript-test.yml`,
  `rust-test.yml`: each language's full test, lint and build suite. They are
  path-filtered to their own directory and also run on any change under
  `fixtures/**`, so a fixtures-only PR runs every language.
- `fuzz.yml`: the Go fuzz targets, 30 seconds each, on PRs that touch
  `go/toolformat`, `go/toolspec`, `go/reasoning` or `fixtures/**`. The same
  workflow runs each target for 10 minutes nightly and on
  `workflow_dispatch`. That long run needs no secrets or model, so it stays
  on hosted runners (see [Fuzzing](#fuzzing)).

**Tier 2: trusted events only.** Work that needs a live model, the real
`kei-proxy`, or more time than a PR should wait for: the live model captures,
the real-proxy integration with a governed-call audit assertion, and the
agent evals. Anything that needs the model servers runs on the self-hosted
EKS runners `kei-agent-evals` and `kei-agent-evals-qwen` (runner group
"Agent evals"), which reach the servers on the tailnet; there are two of
them, so work that does not need the tailnet stays GitHub-hosted. These jobs
run on `schedule` or `workflow_dispatch` (the agent evals also on labelled
same-repository PRs), never on fork PRs and never through
`pull_request_target`. They are not part of this page's gate.

| Workflow | Job | Trigger | Runner |
|---|---|---|---|
| `live-captures.yml` | `capture` (one per model profile) | weekly (Monday 05:23 UTC), `workflow_dispatch` | `kei-agent-evals` (GLM), `kei-agent-evals-qwen` (Qwen) |
| `live-captures.yml` | `compare` | after `capture` | `ubuntu-latest` |
| `real-proxy-integration.yml` | `examples-vs-real-proxy`, `governed-call-audit` | weekly (Monday 06:00 UTC), `workflow_dispatch`, PRs touching `fixtures/kei/**`, the examples or the kei clients | `ubuntu-latest` (no secrets, model or tailnet) |
| `agent-evals.yaml` | `eval` | nightly, `workflow_dispatch`, same-repository PRs | `kei-agent-evals`, `kei-agent-evals-qwen` |

## The conformance gate

Each leg of `conformance.yml` runs its language's fixture tests with
`AGENTWARE_CONFORMANCE_LOG` set. The fixture tests append one JSON line per
case that passed:

```json
{"language": "go", "fixture": "toolformat/glm-cases.json", "section": "parse_calls", "case": "single call"}
```

`fixture` is the path under `fixtures/`, `section` the fixture's top-level
array and `case` the entry's `name`. The leg prints its counts to the job
summary, and the `Conformance gate` job then runs
`scripts/conformance/gate.py check`, which:

1. lists every case in the fixture families below;
2. fails if a language did not run a case and `fixtures/EXCLUSIONS.md` does
   not list it for that language;
3. fails if a language recorded a case that is not in the fixtures (a
   renamed case or a typo in a test);
4. fails if an exclusion row no longer excludes anything;
5. fails if any leg failed, was cancelled or produced no log.

| Family | Fixture files | Sections that hold cases |
|---|---|---|
| toolformat | `toolformat/*-cases.json` | `format_definitions`, `parse_calls`, `format_result`, `validate_format` |
| reasoning | `reasoning/*-cases.json` | `examples` |
| manifest-v3 loader | `toolspec/manifest-v3/index.json` | `valid`, `invalid` |
| ToolSpec contract | `toolspec/cases.v1.json` | `cases` |

The recorders live next to the tests: `go/internal/conformance`
(`conformance.Record(t, fixture, section, name)`, written at the end of the
test and only if it passed), `python/tests/conformance.py`
(`record(fixture, section, case)`, called after the case's assertions), and
a small `record()` function at the top of each TypeScript fixture test (ts-jest
compiles tests against `rootDir: src`, so tests cannot share a helper module).
The Rust recorder is the small `record()` function at the top of
`rust/agentware-kei/tests/manifest_v3_fixtures.rs` (appending the same JSON
line, with `"language": "rust"`, when the variable is set).

Run it locally the way CI does:

```sh
log=$(mktemp -d)
(cd go && AGENTWARE_CONFORMANCE_LOG=$log/go.jsonl go test -count=1 ./toolformat/... ./toolspec/... ./reasoning/...)
(cd python && AGENTWARE_CONFORMANCE_LOG=$log/python.jsonl uv run --extra dev pytest -q -k "toolformat or reasoning or toolspec or manifest" tests/)
(cd typescript && AGENTWARE_CONFORMANCE_LOG=$log/typescript.jsonl node --experimental-vm-modules node_modules/jest/bin/jest.js toolspec toolformat reasoning manifest)
touch "$log/rust.jsonl" && AGENTWARE_CONFORMANCE_LOG=$log/rust.jsonl cargo test --manifest-path rust/Cargo.toml --workspace
python3 scripts/conformance/gate.py check --logs "$log"
```

Go needs `-count=1`: a cached test result skips the run and records nothing.

## Fuzzing

The Go parsers have native fuzz targets, seeded from the shared fixtures:

| Target | Package |
|---|---|
| `FuzzParseToolCalls` | `go/toolformat` |
| `FuzzParseManifestV3` | `go/toolspec` |
| `FuzzAdapter` | `go/reasoning` |

Run one locally from `go/` (`-run='^$'` skips the unit tests):

```sh
go test -run='^$' -fuzz='^FuzzParseToolCalls$' -fuzztime=30s ./toolformat/
```

A failing input is written to `<package>/testdata/fuzz/<Target>/<file>`. In
CI the job fails, uploads the new files as the `fuzz-crashers-<Target>`
artifact and prints the replay command in the job summary. To reproduce,
put the file in that directory and run:

```sh
go test -run='FuzzParseToolCalls/<file>' ./toolformat/
```

Fix the parser and commit the input as a regression seed: plain `go test`
replays every file under `testdata/fuzz`, so the case stays covered.

## Live model captures

`live-captures.yml` checks that the SDKs agree on what real models send, not
just on the hand-written fixtures. Each week the `capture` job sends the
fixed prompts in `scripts/live-captures/prompts.json` (tool calls, parallel
calls, a declined tool, plain reasoning) to every profile in
`evals/model-profiles.yaml`, with the profile's sampling, seed and thinking
setting, the way the eval runner sends them. It writes, per profile:

- `raw/<profile>/<prompt>.json`: the request (never the endpoint URL) and the
  server's response, verbatim;
- `candidates/toolformat/live-<profile>-cases.json` (and
  `live-<profile>-text-cases.json` when the model put `<tool_call>` markup in
  its content) and `candidates/reasoning/live-<profile>-cases.json`: the
  responses as cases in the shape of `fixtures/toolformat` and
  `fixtures/reasoning`, without expected values. The family each profile is
  parsed as is set in `prompts.json`.

The `compare` job then has Go, the reference implementation, fill in the
expected values (`TestFillLiveCaptures` in `go/toolformat` and
`go/reasoning`, which only runs with `AGENTWARE_LIVE_CAPTURE_DIR` set). A
response Go cannot parse goes to the file's `go_rejected` list, since
`parse_calls` has no error form; a reasoning response Go fails closed on gets
the fail-closed form (both expected values null). The filled candidates are
copied next to the shared fixtures, every language's fixture tests run over
them, and the copies are removed. A language whose tests fail disagrees with
Go on a captured response, and the run fails. Rust replays the
`live-*-cases.json` toolformat files through `live_capture_cases` and skips
the qwen family, which it has no formatter for yet (see the exclusions).

A model server that does not answer makes `capture` exit 2 and the job
fail; the compare job still checks the profiles that answered.

Captures are never committed. The `live-captures-<profile>` artifacts hold
the raw responses and candidates, and `live-captures-compared` holds the
filled candidates, `report.json`, `summary.md` (also the job summary) and a
log per language. To adopt a capture, copy its case into the fixture file in
a PR and say where it came from (next section).

Run it locally against your own endpoint, or check the script against the
stub server in its tests:

```sh
export EVAL_DEEPSEEK_BASE_URL=http://<glm host>:8000/v1   # the profile's base_url_env
uv run --no-project --python 3.11 --with pyyaml==6.0.2 \
  python scripts/live-captures/capture.py capture --profile glm-5.3-flash --out _captures
python3 scripts/live-captures/capture.py compare --captures _captures   # needs go, uv, node (npm ci), cargo
uv run --no-project --python 3.11 --with pyyaml==6.0.2 \
  python -m unittest discover -s scripts/live-captures -p '*_test.py'
```

## Real kei-proxy and the governed-call audit

`real-proxy-integration.yml` runs the released `kei-proxy serve` against the
fake control plane (`fixtures/kei/fake-control-plane.py`) with the policy
bundle that permits `echo` and denies `delete_everything`.

- `examples-vs-real-proxy` runs the Go, Python and TypeScript minimal
  harnesses against `KEI_PROXY_VERSION` (the SDK's compatibility pin) and
  checks each prints `ALLOW tool=echo` and `DENY tool=delete_everything`.
- `governed-call-audit` runs the current release (`AUDIT_PROXY_VERSION` in
  the workflow, overridable from `workflow_dispatch`), makes one ALLOW and one
  DENY call through the Go SDK, and asserts with
  `fixtures/kei/assert-audit-records.py` that both decisions are in the
  proxy's spool (`<state dir>/audit/harness-tool.jsonl`) and in what its
  collector flushed to `POST /api/v1/runtime/audit:flush`, each with a
  non-empty `invoking_subject`. The fake control plane answers that route the
  way the catalog does (exact `upserted`/`received`/`skipped` counts, a record
  with no `invoking_subject` skipped, HAI-928) and writes what it stored to
  `FAKE_CONTROL_PLANE_AUDIT_LOG`. The collector flushes about a minute after
  `serve` starts and then drains the spool, so the job builds the harness
  before starting the proxy and checks the spool straight after the calls.

Every archive the jobs download must have its checksum in
`fixtures/kei/KEI_PROXY_SHA256`; bumping `AUDIT_PROXY_VERSION` means adding
the new `Linux_x86_64` line from the release's checksums file.

## Adding a fixture case

1. Add the entry to the fixture file with a `name` unique within its
   section. For manifest-v3, add the files and list them in `index.json`
   (see `fixtures/toolspec/manifest-v3/README.md`).
2. If a model's real output is the source, say where it came from in the
   fixture (model, server, date), as the GLM-5.3 cases do.
3. Run every language's fixture tests. Each runner reads the whole section,
   so a new case is picked up without test changes; if a language now fails,
   fix that language in the same PR.
4. A new section, or a new fixture family, needs a runner in every language
   and an entry in `FAMILIES` in `scripts/conformance/gate.py`.

## Exclusions

`fixtures/EXCLUSIONS.md` is the only way for a language to skip a case. Each
row names a language, a case pattern (`<fixture>#<section>/<case>`, shell
globbing), the reason, and the PR or task that removes it. Rows are
temporary: the PR that adds the missing runner deletes its row, and the gate
fails while a row matches nothing that language still skips, so a stale row
cannot outlive its runner. Adding a row is a decision for the PR reviewer,
not a way to turn a red check green.
