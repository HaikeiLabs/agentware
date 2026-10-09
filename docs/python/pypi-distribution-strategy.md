# Python PyPI distribution strategy

## Purpose and scope

This document defines how the Python implementation of Agentware should be
packaged and released as `pedro-agentware` on PyPI. It is a release contract
and rollout plan; it does not add runtime functionality or change the public
API.

The package is built from `python/` and is versioned independently in
`python/pyproject.toml`. The repository also contains Go and TypeScript
implementations; this document covers only the Python PyPI artifacts. It is the
Python counterpart to `docs/typescript/npm-distribution-strategy.md` and follows
the same staged approach: describe the artifact contract, state what must be
validated, and list the packaging gaps as explicit follow-up work rather than
changing packaging in a documentation change.

## Current package shape

The current package configuration has these properties:

- Distribution name: `pedro-agentware`; import name: `pedro_agentware`.
- Version: `0.8.0` in the repository at the time of writing.
- Layout: src-layout, with the package under `python/src/pedro_agentware/`.
- Build backend: setuptools (`setuptools>=61.0`) via `setuptools.build_meta`.
- Requires-Python: `>=3.10`.
- Runtime dependencies: `pydantic>=2.0` and `httpx>=0.27.0`.
- Optional extras: `inference` (`pgmpy>=0.1.26`) and `dev` (pytest, pytest-cov,
  pytest-asyncio, ruff, mypy).
- License declared as MIT in metadata; the only `LICENSE` file lives at the
  repository root, not under `python/`.

The public surface is the root `pedro_agentware/__init__.py` barrel, which
re-exports `Tool`, `ToolRegistry`, `Result`, `Middleware`, `PolicyEvaluator`,
`Auditor`, `Executor`, `Job`, `JobManager`, `Message`, `Backend`,
`ContextManager`, `PromptGenerator`, and `ToolFormatter`. Submodules
(`middleware`, `middleware.guardrails`, `kei`, `toolformat`, `llm`,
`llmcontext`, `tools`, `executor`, `jobs`, `prompts`, `memory`) are importable
by path and are used that way throughout the docs, so subpath imports are part
of the effective contract even though nothing declares them.

### Known packaging gaps

A local build of the current tree (`python -m build`) succeeds but produces
artifacts that are not yet release-quality. These are facts about the tree as it
stands, and each maps to a follow-up item below:

1. **`evals` is published as a second top-level package.** `src/evals/` is
   picked up by setuptools auto-discovery, so the wheel's `top_level.txt` reads
   `evals` and `pedro_agentware`. Installing the package therefore claims the
   generic top-level name `evals` in the consumer's environment. This is a
   namespace collision hazard and must be fixed before the first publish.
2. **No `README.md` under `python/`.** `pyproject.toml` declares
   `readme = "README.md"`, and the build silently emits metadata with
   `Description-Content-Type: text/markdown` and an empty body. The published
   PyPI project page would be blank.
3. **No `LICENSE` file in either artifact.** The license is declared as
   `{text = "MIT"}` metadata only; no license file is included in the wheel or
   sdist.
4. **No `py.typed` marker.** The package is type-checked under `mypy --strict`
   in CI, but consumers get no type information from an installed copy, because
   PEP 561 requires the marker file to be shipped.
5. **The sdist omits tests and packaging metadata.** It contains only
   `pyproject.toml`, `setup.cfg`, `PKG-INFO`, and `src/`. There is no
   `MANIFEST.in`, so `tests/` and any package data are excluded.
6. **No explicit `packages`/`package-data` configuration.** Artifact contents
   are whatever auto-discovery happens to find, which is how gap 1 arose.

The three adapters under `python/adapters/` (`pedro-agentware-hermes`,
`pedro-agentware-kitaru`, `pedro-agentware-pydantic`) each carry their own
`pyproject.toml` and are separate distributions. They are out of scope for the
initial rollout and should not be published until the core package's release
pipeline is proven.

## Artifact contract

The target publishable artifacts are a wheel and an sdist that contain only
what consumers need to import, execute, and type-check the library.

The **wheel** should contain exactly one top-level import package:

```text
pedro_agentware/**/*.py
pedro_agentware/py.typed
pedro_agentware-<version>.dist-info/METADATA
pedro_agentware-<version>.dist-info/WHEEL
pedro_agentware-<version>.dist-info/RECORD
pedro_agentware-<version>.dist-info/licenses/LICENSE
```

It must not contain `evals`, tests, fixtures, evaluation cases, adapters,
coverage output, or repository-level documentation. `top_level.txt` (or the
equivalent record) must list `pedro_agentware` and nothing else. Until an
explicit package configuration is added, inspect the built wheel and treat any
unexpected top-level entry as a release blocker.

The **sdist** should be a buildable source snapshot: `pyproject.toml`,
`README.md`, `LICENSE`, `src/pedro_agentware/`, and `tests/`, so that a
downstream packager can reproduce the wheel and run the test suite from the
sdist alone.

Metadata must additionally carry a project URL pointing at the repository, a
`Summary` that matches the README, and trove classifiers for the supported
Python versions and license. None of these are present today.

### Public import surface

The package should eventually declare which import paths are supported. The
initial set should be the root barrel plus the subpaths already documented in
`docs/python/README.md` and `docs/harness-contract.md`:

- `pedro_agentware` (root barrel)
- `pedro_agentware.middleware`
- `pedro_agentware.middleware.guardrails`
- `pedro_agentware.kei`
- `pedro_agentware.tools`
- `pedro_agentware.toolformat`
- `pedro_agentware.executor`
- `pedro_agentware.llm` and `pedro_agentware.llmcontext`

Python has no `exports` map, so this is enforced by convention, by the smoke
test below, and by documenting anything else as internal. Do not promise a
subpath until it has a stable `__init__.py`, passes `mypy --strict`, and is
covered by a consumer test. Removing or renaming a documented subpath is a
breaking change.

## Build and validation

Every release candidate must be built and inspected from a clean checkout:

```bash
cd python
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
ruff check src/
mypy src/
pytest
pip install build twine
python -m build          # writes dist/*.whl and dist/*.tar.gz
twine check dist/*
```

`twine check` validates metadata and long-description rendering. It is
necessary but not sufficient — it will pass on an artifact that ships the wrong
files, which is exactly the current failure mode.

### Artifact inspection

Assert the contents of both artifacts, not just that they built:

```bash
python -m zipfile -l dist/pedro_agentware-*.whl
tar tzf dist/pedro_agentware-*.tar.gz
```

The wheel must show a single top-level `pedro_agentware/` tree, a `py.typed`
marker, and a bundled license. Any other top-level directory is a release
blocker.

### Wheel smoke test

Before publishing, validate the built wheel rather than importing from the
workspace. The smoke test must install the generated `.whl` into a fresh
virtualenv that does not have the repository on `sys.path`, and verify:

1. `import pedro_agentware` succeeds and `pedro_agentware.__version__` matches
   the version being released.
2. Every documented subpath above imports without error.
3. The names in `__all__` are all importable from the root barrel.
4. A tool can be registered and executed through `Middleware` without any
   repository source file present.
5. `pedro_agentware.__file__` resolves inside `site-packages`, proving the test
   exercised the installed copy and not the source tree.
6. `mypy` in a consumer project resolves the package's types, once `py.typed`
   ships.
7. The `inference` extra installs and imports cleanly, and the base install
   works without it.

```bash
python -m venv /tmp/smoke && /tmp/smoke/bin/pip install dist/pedro_agentware-*.whl
cd /tmp && /tmp/smoke/bin/python -c "
import pedro_agentware, pedro_agentware.middleware, pedro_agentware.kei
print(pedro_agentware.__version__, pedro_agentware.__file__)
"
```

Run the smoke test on every Python version in the support matrix, against the
built wheel, not an editable install and not a registry copy. The `cd /tmp` (or
an equivalent working directory outside the checkout) matters: with a src-layout
package it is the difference between testing the artifact and testing the repo.

The current `python-test.yml` workflow runs lint and tests on Python 3.11 only
and never builds a distribution. The stale `ci-python.yaml` workflow does have a
build-and-import-check job, but it targets the pre-rename `middleware_py/`
directory that no longer exists, so it validates nothing. Both need to be
reconciled as part of the follow-up work.

## Versioning and release notes

Use [Semantic Versioning 2.0.0](https://semver.org/) for the Python package:

- `0.x` indicates an evolving API. A minor release may add capabilities; a
  breaking change must still be called out prominently.
- After `1.0.0`, breaking changes to the public import surface, to the
  documented subpaths, or to the minimum Python version require a major
  version.
- New backwards-compatible exports and features are minor releases.
- Bug fixes, packaging-metadata corrections, and dependency-range updates that
  do not change the supported API are patch releases.

Pre-releases use PEP 440 suffixes: `0.2.0a1`, `0.2.0b1`, `0.2.0rc1`. Note that
PEP 440 and SemVer differ in spelling here (`0.2.0a1` rather than
`0.2.0-alpha.1`); the PEP 440 form is authoritative for the Python artifact,
and the version string must be a valid PEP 440 version or the upload is
rejected.

### Immutability

All releases are immutable. A published version is never overwritten and its
number is never reused. The version must be written once and then used
consistently in `pyproject.toml`, `__init__.__version__`, the git tag, the
GitHub release, and the published PyPI metadata. `pyproject.toml` and
`__init__.py` currently carry the version in two places; a single source of
truth (dynamic version read from the package, or a check in CI that the two
agree) should be established before the first release.

Each release needs a changelog entry describing public API changes, packaging
changes, supported Python versions, and migration steps.

### Existing automation is not the target policy

`.github/workflows/release.yml` currently runs on a weekly schedule, rewrites
`python/pyproject.toml` to a date-based version (`0.1.$(date +%Y%m%d)`), commits
to the default branch, and invokes `twine upload` with a static `PYPI_TOKEN`
secret. This is not the target release mechanism:

- Date-based versions are not SemVer and communicate nothing about
  compatibility.
- An unreviewed scheduled publish can ship an untested commit.
- It uploads only `dist/*.whl`, never the sdist.
- The `twine upload --skip-upload` flag in that workflow is not a real twine
  option, so the step's actual behavior does not match its apparent intent —
  another reason not to treat it as the working release path.
- It uses a long-lived token where trusted publishing is available.

Replace that path as part of the follow-up implementation below.

## Publishing security and provenance

The target is [PyPI Trusted Publishing](https://docs.pypi.org/trusted-publishers/)
via GitHub Actions OIDC, with no long-lived API token stored in repository
secrets.

Required configuration (none of it provisioned yet):

1. **Confirm name ownership.** Verify that `pedro-agentware` is available or
   already owned at `pypi.org/project/pedro-agentware/`. PyPI project ownership
   is separate from GitHub organization ownership; the person who configures
   trusted publishing must be both a PyPI project owner and a repository admin.
   Name normalization follows
   [PEP 503](https://peps.python.org/pep-0503/), so `pedro-agentware`,
   `pedro_agentware`, and `Pedro.Agentware` all resolve to the same project.
2. **Add a pending publisher** at `pypi.org/manage/account/publishing/` naming
   the repository, the publishing workflow filename, and the `release`
   environment. Do the same on TestPyPI for the rehearsal.
3. **Create a protected `release` GitHub environment** restricted to `main` and
   to tag refs, with required reviewers (proposed: two maintainers).

The publish workflow should:

- run only for an approved release tag, in the protected `release` environment;
- request the minimum permissions — `contents: read` and `id-token: write`;
- pin action versions and use `actions/setup-python` with an explicit version;
- run the full validation, artifact inspection, and wheel smoke test before
  publishing;
- upload **both** the wheel and the sdist, in one `pypa/gh-action-pypi-publish`
  step, so the release is atomic;
- avoid echoing artifact contents or environment into logs;
- record the tag, commit SHA, version, and PyPI project URL in the job summary.

```yaml
# .github/workflows/publish-python.yaml (proposed)
name: Publish Python

on:
  push:
    tags: ['python-v*']

jobs:
  publish:
    runs-on: ubuntu-latest
    environment: release
    permissions:
      contents: read
      id-token: write     # required for OIDC trusted publishing
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: '3.11'
      - name: Build
        run: cd python && pip install build twine && python -m build
      - name: Validate
        run: cd python && twine check dist/*
      # ... artifact inspection + wheel smoke test ...
      - uses: pypa/gh-action-pypi-publish@release/v1
        with:
          packages-dir: python/dist
          # no password: OIDC token is used automatically
```

Because the repository publishes three language artifacts from one tree, tags
must be language-scoped (for example `python-v0.2.0`) so the Python and npm
release workflows cannot be triggered by each other's tags.

### Provenance and attestations

`pypa/gh-action-pypi-publish` generates
[PEP 740](https://peps.python.org/pep-0740/) digital attestations by default
when publishing through trusted publishing, and PyPI displays them as verified
provenance on the release page. Keep that default enabled. Do not disable
attestations to work around a publish failure; fix the workflow instead. If a
stronger supply-chain guarantee is later required, add SLSA provenance via
`slsa-framework/slsa-github-generator` on top of, not instead of, the PEP 740
attestations.

Provenance is only meaningful if the artifact is built in the same trusted
workflow that publishes it. Never publish a locally built wheel to production
PyPI.

## TestPyPI rehearsal

The first release must be rehearsed end to end on
[TestPyPI](https://test.pypi.org/) before any production publish:

1. Configure a separate pending publisher for TestPyPI.
2. Tag a pre-release (`python-v0.2.0rc1`) and let the workflow publish to
   `https://test.pypi.org/legacy/`.
3. Install from TestPyPI in a clean environment, with the real index available
   for dependencies, since `pydantic` and `httpx` are not mirrored there:

   ```bash
   pip install \
     --index-url https://test.pypi.org/simple/ \
     --extra-index-url https://pypi.org/simple/ \
     pedro-agentware==0.2.0rc1
   ```

4. Run the wheel smoke test against the TestPyPI-installed copy.
5. Confirm that the project page renders the README and shows the expected
   metadata and attestations.

TestPyPI has its own accounts and its own namespace, and it prunes files, so
treat it strictly as a pipeline rehearsal — never as a distribution channel or
a backup. A version number burned on TestPyPI is still reusable on production
PyPI, but reusing a TestPyPI version number is not possible, so rehearse with
incrementing release-candidate numbers.

## Rollback

Published releases are immutable, so rollback is forward-only:

1. **Yank** the bad release. A yanked version stays installable for anyone who
   pins it exactly, but resolvers skip it for range specifiers
   ([PEP 592](https://peps.python.org/pep-0592/)). Yanking is done by a PyPI
   project owner from the project's release page, with a reason string that is
   visible to consumers.
2. **Publish a fixed version** with an incremented patch or minor number.
3. **Document the incident** in the changelog and release notes.

Deleting a release is an exceptional operation — reserved for cases like an
accidental secret in an artifact — and is not a rollback mechanism, because the
version number is then permanently burned and consumers who cached it see an
inconsistent index. If credentials are exposed, rotate them first, then delete,
then publish a clean version.

If the workflow fails after tagging but before uploading, fix the workflow and
re-run against the same immutable tag, but only after confirming from the PyPI
release history that nothing was actually published. A partial upload — wheel
published, sdist failed — must be treated as a released version: yank it and
publish a new one rather than trying to add the missing file.

## Dependency policy

`pedro-agentware` is a library, not an application:

- **No lockfile is published.** Consumers own their own resolution. A lockfile
  may be used for CI reproducibility, but it is never part of the distribution.
- **Use lower-bound specifiers** (`pydantic>=2.0`, `httpx>=0.27.0`). Add an
  upper bound only for a dependency with a demonstrated breaking-change history,
  and record the reason; a speculative `<3.0` cap propagates into every
  consumer's resolution and is hard to undo.
- **Keep the runtime dependency set minimal.** `pydantic` and `httpx` are the
  current cost of installing the package. Anything heavier — inference,
  provider SDKs, adapters — belongs behind an extra or in a separate
  distribution, as `pgmpy` already is behind `inference`.
- **Never make a dev tool a runtime dependency.** The `dev` extra exists for the
  contributor workflow and must not be implied by the base install.
- Adding a runtime dependency, or raising a lower bound, is at least a minor
  release and needs a release note.

Because the same library ships in three languages, a dependency added to the
Python implementation for shared logic should be checked against how the Go and
TypeScript counterparts solve the same problem.

## Python support policy

- The floor is `>=3.10`, matching `requires-python` and the ruff/mypy
  `target-version`. Raising it is a breaking change requiring a major version
  (or, pre-1.0, a prominent minor-release note).
- CI must test the floor and the current stable release at minimum; the
  intended matrix is 3.10, 3.11, 3.12, and 3.13. `python-test.yml` currently
  tests 3.11 alone, which means the declared floor is unverified.
- The wheel is pure Python and built as `py3-none-any`. If a compiled dependency
  or platform-specific behavior ever appears, this policy needs revisiting
  before the matrix does.
- Drop a Python version only after it reaches upstream end-of-life, and announce
  it one minor release ahead.
- `requires-python` must always match the tested matrix. A mismatch lets pip
  install the package into an environment nobody tested.

## Ownership

| Role | Responsibility |
|------|----------------|
| Python maintainers | Public import surface, artifact contents, packaging config, wheel smoke test, support matrix |
| Release manager | Version bump, changelog, tag creation, monitoring the publish workflow |
| PyPI project owner | Trusted-publishing configuration, environment protection, yanking |
| Security owner | Vulnerability disclosure, emergency yank, credential rotation |

Any change to the root barrel, a documented subpath, `requires-python`, or the
dependency set must update this document and carry a release-note assessment.
Release approval requires the protected `release` environment's reviewers; no
single person should be able to publish unreviewed.

## Phased rollout

### Phase 1 — Fix the artifact

- [ ] Exclude `src/evals/` from distribution, or move it out of `src/`, so the
      wheel has exactly one top-level package.
- [ ] Add `python/README.md` (package-facing usage) and `python/LICENSE`.
- [ ] Add `pedro_agentware/py.typed` and ensure it ships as package data.
- [ ] Add explicit `[tool.setuptools]` package configuration and a `MANIFEST.in`
      so sdist contents are intentional.
- [ ] Add project URLs and trove classifiers to `pyproject.toml`.
- [ ] Make the version single-sourced between `pyproject.toml` and
      `__init__.py`, or assert their agreement in CI.

### Phase 2 — Prove the pipeline

- [ ] Confirm `pedro-agentware` name ownership on PyPI and TestPyPI.
- [ ] Add a build + artifact-inspection + wheel-smoke-test job to Python CI,
      running on every PR that touches `python/`.
- [ ] Expand the CI matrix to 3.10–3.13.
- [ ] Delete or repair the stale `ci-python.yaml` workflow, which still targets
      the removed `middleware_py/` path.
- [ ] Configure the pending publishers and the protected `release` environment.
- [ ] Add `.github/workflows/publish-python.yaml` with language-scoped tags.
- [ ] Publish a release candidate to TestPyPI and validate it end to end.

### Phase 3 — Publish

- [ ] Remove Python publishing from the scheduled `release.yml`, and remove the
      `PYPI_TOKEN` secret once trusted publishing works.
- [ ] Tag and publish the first SemVer release to production PyPI.
- [ ] Verify README rendering, metadata, and attestations on the project page.
- [ ] Add a PyPI badge and `pip install pedro-agentware` instructions to
      `docs/python/README.md`.
- [ ] Rehearse a yank on a throwaway release candidate so the procedure is known
      before it is needed.

### Phase 4 — Adapters (deferred)

- [ ] Decide whether `pedro-agentware-hermes`, `-kitaru`, and `-pydantic` ship
      to PyPI, and whether they pin the core package by range or exact version.
      Do not start this until Phase 3 is complete.

## Risks and mitigations

| Risk | Impact | Mitigation |
|------|--------|------------|
| `evals` published as a top-level package | High — namespace collision in every consumer environment, fixable only by a new release | Phase 1 fix; assert wheel top-level contents in CI |
| Blank PyPI project page from the missing README | Medium — the first impression of the package; metadata-only fix still needs a new version | Phase 1 fix; `twine check` in CI |
| PyPI name unavailable or owned elsewhere | Critical — blocks the whole plan | Verify in Phase 2 before any pipeline work; have an alternate name ready |
| Long-lived `PYPI_TOKEN` leaks | Critical | Move to trusted publishing; delete the secret in Phase 3 |
| Scheduled workflow publishes an untested date-versioned release | High | Remove Python publishing from `release.yml` in Phase 3 |
| Version published without the declared floor being tested | Medium | Expand the CI matrix to the full `requires-python` range in Phase 2 |
| Cross-language tag collision triggers the wrong publish | Medium | Language-scoped tags (`python-v*`) and path-filtered workflows |
| Consumers depend on an undocumented internal module | Medium | Document the supported subpaths; cover them in the smoke test |
| Partial upload leaves wheel and sdist out of sync | Medium | Publish both in one action step; treat a partial upload as released and yank |
| Python and Go/TypeScript versions drift apart | Low | Independent SemVer per language is intended; state the mapping in release notes |

## Acceptance criteria

| Criterion | Definition |
|-----------|------------|
| Name verified | `pedro-agentware` confirmed available or owned on PyPI and TestPyPI |
| Clean wheel | Wheel contains exactly one top-level package, `pedro_agentware`, plus `dist-info` |
| Metadata complete | README, LICENSE, project URLs, and classifiers present; `twine check` passes on wheel and sdist |
| Typed | `py.typed` ships and a consumer project type-checks against the installed package |
| Reproducible sdist | `pip wheel` on the sdist reproduces the published wheel's contents |
| Smoke test passes | Wheel installed in a clean venv outside the checkout; root barrel, every documented subpath, and a real tool execution all work on each supported Python |
| Matrix verified | CI runs lint, type-check, tests, build, and smoke test on 3.10–3.13 |
| Trusted publishing | A tag-triggered publish succeeds with OIDC and no token in secrets |
| Environment protection | Publishing requires the protected `release` environment's reviewers |
| Provenance | PEP 740 attestations generated and shown as verified on the PyPI release page |
| TestPyPI rehearsal | A release candidate published to TestPyPI and installed successfully from it |
| Rollback rehearsed | A yank performed and verified on a throwaway release candidate |
| Legacy path removed | Python publishing removed from the scheduled `release.yml`; `PYPI_TOKEN` deleted |

## References

- [PyPA — Packaging Python projects](https://packaging.python.org/en/latest/tutorials/packaging-projects/)
- [PyPI — Trusted publishers](https://docs.pypi.org/trusted-publishers/)
- [pypa/gh-action-pypi-publish](https://github.com/pypa/gh-action-pypi-publish)
- [PyPA — Using TestPyPI](https://packaging.python.org/en/latest/guides/using-testpypi/)
- [Semantic Versioning 2.0.0](https://semver.org/)
- [PEP 440 — Version identification](https://peps.python.org/pep-0440/)
- [PEP 503 — Simple Repository API (name normalization)](https://peps.python.org/pep-0503/)
- [PEP 561 — Distributing and packaging type information](https://peps.python.org/pep-0561/)
- [PEP 592 — Yanked releases](https://peps.python.org/pep-0592/)
- [PEP 740 — Index attestations](https://peps.python.org/pep-0740/)
- [GitHub — Environment protection rules](https://docs.github.com/en/actions/deployment/targeting-different-environments/using-environments-for-deployment#environment-protection-rules)
- [`docs/typescript/npm-distribution-strategy.md`](../typescript/npm-distribution-strategy.md) — the npm counterpart to this document
