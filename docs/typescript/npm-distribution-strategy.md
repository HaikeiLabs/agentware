# TypeScript npm distribution strategy

## Purpose and scope

This document defines how the TypeScript implementation of Agentware should be
packaged and released as `@pedro/agentware`. It is a release contract and
rollout plan; it does not add runtime functionality or change the public API.

The package is currently published from `typescript/` and is versioned
independently in `typescript/package.json`. The repository also contains Go and
Python implementations, but this document covers only the TypeScript npm
artifact.

## Current package shape

The current package configuration has these properties:

- Name: `@pedro/agentware`.
- Version: `0.8.0` in the repository at the time of writing.
- Format: ESM (`"type": "module"`), targeting ES2022.
- Runtime entrypoint: `dist/index.js`.
- Type entrypoint: `dist/index.d.ts`.
- Source root: `src/`; compiler output: `dist/`.
- Runtime dependencies: `minimatch` and `zod`.
- Supported Node baseline: Node 18 or newer, as declared by `engines`.
- Existing package scripts: `build`, `test`, `lint`, and `format`.

The package currently exposes its public surface through the root barrel at
`src/index.ts`, which re-exports the tools, middleware, executor, jobs, LLM,
context, prompts, tool-format, and memory barrels. The package does not yet
declare an `exports` map, `files` allowlist, `publishConfig`, or a dedicated
package smoke-test script. Those gaps should be addressed before treating the
artifact as a stable SDK contract.

## Artifact contract

The target publishable artifact should contain only what consumers need to
execute and type-check the library:

```text
package.json
README.md (package-facing usage, to be added under typescript/)
LICENSE (to be added or copied under typescript/)
dist/**/*.js
dist/**/*.d.ts
dist/**/*.js.map
```

The current `typescript/` directory has no package-local README or LICENSE, so
these metadata files are not yet part of the target artifact. It must not
contain `src/`, tests, fixtures, local dependency trees, coverage output, or
repository-level documentation. Until a `files` allowlist is added, inspect
`npm pack --dry-run` and treat any unexpected file as a release issue.

The package should eventually declare an explicit `exports` map. The initial
map should expose `.` and only intentionally supported subpaths, such as
`./middleware`, `./tools`, `./executor`, `./llm`, and `./toolformat`, each with
ESM and type targets that match the generated layout. Do not promise a subpath
until it has a stable barrel, generated declaration, and consumer test. Adding
an exports map is a compatibility change because previously reachable internal
paths may become unavailable.

## Required validation

Every release candidate must be built and inspected from a clean checkout:

```bash
cd typescript
npm ci
npm run lint
npm run build
npm test
npm pack --dry-run
```

Before publishing, validate the tarball rather than importing from the
workspace. A minimal smoke test should install the generated tarball into a
temporary consumer project and verify:

1. `import { ToolRegistry } from '@pedro/agentware'` resolves at runtime.
2. TypeScript can resolve the declaration entrypoint under strict mode.
3. Each documented subpath resolves, once an exports map exists.
4. A tool can be registered and executed without requiring source files.
5. The declared Node engine is enforced by the consumer's package manager (and
   produces a warning by default if that manager is not configured to enforce
   engines).

The smoke test should use the exact Node versions in the support matrix and be
run against the packed `.tgz`, not a registry copy or symlink.

## Versioning and changelog policy

Use Semantic Versioning for the TypeScript package:

- `0.x` indicates an evolving API. A minor release may add capabilities; a
  breaking change must still be called out prominently.
- After a `1.0.0` release, breaking public API or package-resolution changes
  require a major version.
- New backwards-compatible exports and features are minor releases.
- Bug fixes, documentation-only package corrections, and dependency updates
  that do not change the supported API are patch releases.

Each release should have a changelog entry describing public API changes,
package-resolution changes, supported Node versions, and migration steps. A
version must be written once and then used consistently in `package.json`, the
git tag, the GitHub release, and the published npm metadata. Never reuse an
already published version.

The repository currently has a scheduled release workflow that performs date-
based version edits and invokes `npm publish` with `NPM_TOKEN`. That workflow
is existing automation, not the target policy: date-based versions and an
unreviewed scheduled publish should not be the long-term npm release mechanism.
Replace or disable that path as part of the follow-up implementation below.

## Publishing security and provenance

The preferred future path is GitHub Actions trusted publishing using npm
provenance, with the npm package configured to trust the repository and release
workflow. The workflow should:

- run only for an approved release tag or an explicit, protected release
  environment;
- use the minimum `contents: read` and `id-token: write` permissions;
- use the pinned Node/setup action and `registry-url` for npm;
- run the full validation and tarball smoke test before publishing;
- publish with provenance enabled (`npm publish --provenance --access public`);
- avoid printing package contents that could contain secrets;
- record the tag, commit SHA, package version, and npm package URL in the
  release summary.

Until trusted publishing is configured, use a short-lived, automation-scoped
npm token stored as an environment/repository secret. Do not commit tokens,
put them in package metadata, or use a developer's personal token in CI.

## Release and rollback procedure

### Proposed release flow

1. Merge the release-ready change into `main`.
2. Update the TypeScript version and changelog in a reviewed commit.
3. Create an annotated tag matching the package version, for example `v0.2.0`.
4. Let the protected release workflow check out that tag and run the required
   validation and tarball smoke test.
5. Publish exactly once, create the GitHub release, and verify the npm tarball
   and metadata from a clean consumer project.

### Incident handling

Prefer a corrective patch release. Do not delete a published version or reuse
its number. If a package is actively harmful, use npm's current deprecation or
unpublish policy with repository-owner approval, document the incident, and
publish a replacement version as soon as it is safe. If a release workflow
fails after tagging but before publishing, fix the workflow and rerun against
the same immutable tag only after confirming that no package was published.

## Compatibility contract

The initial support matrix is Node 18+ and TypeScript consumers that can use
the emitted ES2022 ESM declarations. The package should keep `engines.node`
accurate and test the lowest supported Node version plus the current LTS in CI.
Changing the module format, minimum Node version, declaration layout, or
documented subpath is a release-note-worthy compatibility change.

## Ownership and follow-up work

The TypeScript maintainers own the package entrypoints, artifact contents, and
consumer smoke test. Release maintainers own npm access, trusted-publishing
configuration, tags, and incident response. Any change to a public barrel or
package metadata should include an update to this document and a release-note
assessment.

The following are proposed follow-ups and are intentionally not implemented by
this documentation PR:

1. Add package-local README/LICENSE files, a `files` allowlist, explicit
   `exports` map, and `publishConfig` after confirming the generated directory
   layout.
2. Add an automated `npm pack --dry-run` assertion and tarball consumer smoke
   test.
3. Replace date-based/scheduled npm publishing with a protected tag-driven
   workflow using npm trusted publishing and provenance.
4. Add a changelog/release-note convention and document who can approve a
   release.
5. Test the lowest supported Node version and current LTS in CI.
