# Kei Tool Manifest

**Tool call** = a function used by an agent.

**Capability** = an operation performed on a data store.

A tool call declares its `required_capabilities`. The catalog compiles a tool call into its required capabilities, and permits the call only when every capability is permitted (AND semantics). A capability is evaluated independently against policy. Decisions use the NIST ABAC terms `permit` and `deny`; `allow` is retired.

## Manifest v2

The current export uses `"schema": "kei.tool-manifest/v2"`. Each governed tool declares:

- `source`: the data source, required for connector-backed tools and a policy dimension.
- `required_capabilities`: a non-empty list of source-declared capability identifiers for governed tools. All listed capabilities must be permitted.
- `resource_types`: resource type identifiers, optionally paired with `parent_type`. These are plain identifiers only: no globs and no parent text embedded inside `type`. Instance and parent constraints belong in policy.
- `operation_class`: `read` or `write`.
- `service`: optional credential lookup key. It is not a policy dimension.

Exports sort tool names, required capabilities, and resource types deterministically. The shared fixture is `fixtures/kei/tool-manifest.v2.json`. The prior schema is available only through an explicit v1 export option for the transition. V2 remains the default for compatibility; v3 is available as an explicit export when the runtime/catalog typed-route contract is deployed.

## Manifest v3: explicit typed dispatch routes

V3 (`kei.tool-manifest/v3`) is exported explicitly with `ManifestV3`, `version=3`, or `exportKeiToolManifest(3)`. Each entry carries non-empty `service` and `source`, `operation_class`, and an explicit `route` attached to the same `ToolRegistry` entry used for dispatch. Registration identity is never inferred from tool names, empty metadata, or runtime calls. A connector route is `{ "connector_binding": { "connector_id": "..." } }` and requires non-empty `required_capabilities`; optional `resource_types` are connector-only. A harness route is `{ "harness_executor": { "executor": "...", "registration": "..." } }` and omits both connector-only fields. Plain tools remain registerable without route metadata and continue working locally; they are omitted from v3 until explicitly registered with typed metadata. No control-plane request occurs per tool decision.

V2 export remains the default and unchanged.

## Exporting

Declare a `KeiScope` on each governed tool, register it, and export it with the SDK's tool registry. Go:

```go
manifest, err := registry.ExportKeiToolManifest() // v2
legacy, err := registry.ExportKeiToolManifest(tools.ManifestV1) // explicit transition export
```

Python:

```python
manifest = registry.export_kei_tool_manifest()  # v2
legacy = registry.export_kei_tool_manifest(version=1)  # explicit transition export
```

TypeScript:

```typescript
const manifest = registry.exportKeiToolManifest(); // v2
const legacy = registry.exportKeiToolManifest(1); // explicit transition export
```

## Loading and runtime boundary

The harness ships the exported file as `KEI_TOOL_MANIFEST`. Loading happens through the **kei-proxy bootstrap sync (HAI-273)**. `kei tools import` does not exist. Agentware does not call the catalog tool API at runtime. The distributed proxy remains the connector/provider runtime and enforcement point; the control plane receives metadata only. Provider payloads/results, customer content, credentials, embeddings, and indexes stay in the tenant runtime.

The harness sends the tool name for authorization. It does not send an instance resource or resolve credentials from `service`; resource constraints are policy decisions, and the proxy performs credential lookup and provider execution.

## Linting manifests

All SDK linters use the same rules and capability snapshot, `fixtures/kei/connector-capabilities.v0.5.0.json`. They reject missing source or capabilities, undeclared source capabilities, duplicate names, unsorted/non-deterministic exports, globs or embedded parents in resource types, `allow`, and approval fields. Output is JSON; errors return exit status 1.

```bash
# From the repository root
PYTHONPATH=python/src python -m pedro_agentware.lint_tools fixtures/kei/tool-manifest.v2.json
(cd go && go run ./cmd/lint-tools ../fixtures/kei/tool-manifest.v2.json)
(cd typescript && npm run build && node dist/tools/lint-cli.js ../fixtures/kei/tool-manifest.v2.json)
# Published TypeScript package: npx agentware-lint-tools KEI_TOOL_MANIFEST
```

### Refreshing the capability table

The table is generated from the `contract.CapabilitiesFor` provider declarations in `kei-connector-contracts` v0.5.0. Check out that exact tag/version and run:

```bash
python scripts/generate-connector-capabilities.py /path/to/kei-connector-contracts-v0.5.0
```

Review the generated diff and update the pinned version in the fixture filename and this document when deliberately moving to a later contract version.

## Harness CI example

Each harness uses its own registry initialization to export the file that it will ship as `KEI_TOOL_MANIFEST`, then runs the Python linter in the same job:

```yaml
steps:
  - uses: actions/checkout@v4
  - uses: actions/setup-python@v5
    with:
      python-version: "3.12"
  - run: python -m pip install ./python
  # Replace this harness-owned exporter with its registry bootstrap module.
  - run: python -m my_harness.export_manifest --output KEI_TOOL_MANIFEST
  - run: python -m pedro_agentware.lint_tools KEI_TOOL_MANIFEST
  - uses: actions/upload-artifact@v4
    with:
      name: kei-tool-manifest
      path: KEI_TOOL_MANIFEST
```

The exporter must write deterministic v2 JSON. The linter's non-zero status blocks the job before an image is built.
