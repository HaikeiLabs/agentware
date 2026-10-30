# Changelog

All notable changes to the `haikeilabs-agentware` Python package (import
`haikeilabs.agentware`) are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the package uses
[semantic versioning](https://semver.org/) in its PEP 440 spelling. The Python
package is versioned independently of the TypeScript SDK; a Python release is
tagged `agentware-py-v<version>`.

## [Unreleased]

### Changed

- Python Unix-socket authorize clients can optionally send the exact
  `connector{connector_id, capability}` target with a non-empty `resource`.
- **Breaking:** removed caller-provided `agent_id` from Kei authorize client
  APIs. Authorization identity is derived from the runtime token (HAI-557).

## [0.1.0]

The first release on PyPI. Earlier versions were installed from the Git
repository only, as `pedro-agentware`.

### Changed

- The distribution is named `haikeilabs-agentware`, matching the npm package
  `@haikeilabs/agentware`.
- The import package is `haikeilabs.agentware`. `haikeilabs` is a PEP 420
  implicit namespace package (no `haikeilabs/__init__.py`), so other
  `haikeilabs.*` distributions can install beside this one.
- The package license metadata now matches the repository: `Apache-2.0`
  (SPDX expression, PEP 639), with `LICENSE` included in the distributions.
  It previously said MIT.
- `haikeilabs.agentware.__version__` comes from the installed package
  metadata, so `pyproject.toml` is the only version source.
- The distributions no longer contain the repository's eval runner
  (`python/src/evals`) as a top-level `evals` module.

### Deprecated

- `pedro_agentware`, the previous import name. It still works: it and every
  `pedro_agentware.<submodule>` resolve to the same module objects as
  `haikeilabs.agentware.<submodule>`, and importing it emits a
  `DeprecationWarning`. Replace `pedro_agentware` with `haikeilabs.agentware`
  in imports; the alias will be removed in a future release.

### Added

- `haikeilabs.agentware.kei.runtime_socket`: an async client for the
  kei-proxy daemon's Unix socket (named sessions, fail-closed `authorize`,
  model routes, policy bundle status), and `KeiProxySocketAuthorizationClient`
  so `KeiProxyEvaluator` can authorize over the socket.
  `select_authorization_client` picks the socket client when one is
  configured, else the `kei-proxy authorize` subprocess client.
- `haikeilabs.agentware.kei.tool_sync`: `sync_tool_manifest` replaces the
  runtime's tool manifest (`PUT /api/v1/runtime/tools`), refuses an empty
  manifest, and honours the egress proxy and `NO_PROXY`.
- PyPI release workflow (`.github/workflows/publish-python.yml`): trusted
  publishing (OIDC, no stored token) with attestations. Pre-releases go to
  TestPyPI.
- PyPI package page (`python/README.md`), project URLs, classifiers and a
  `py.typed` marker.

[Unreleased]: https://github.com/HaikeiLabs/agentware/compare/agentware-py-v0.1.0...HEAD
[0.1.0]: https://github.com/HaikeiLabs/agentware/releases/tag/agentware-py-v0.1.0
