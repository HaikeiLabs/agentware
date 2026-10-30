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

**Tier 2: trusted events only, self-hosted EKS runners.** Work that needs a
live model, the real `kei-proxy`, or more time than a PR should wait for:
live model re-captures (opening a PR when a model's output drifts from the
fixtures), long fuzz runs, the real-proxy integration with a governed-call
audit assertion, and the agent evals. These jobs run on `schedule`,
`workflow_dispatch` or `push` to `main`, never on fork PRs and never through
`pull_request_target`. They are not part of this page's gate; see
`agent-evals.yaml` and `real-proxy-integration.yml` for the existing ones.

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
