# Kei tool manifest

Agentware exports and registers one format: `kei.tool-manifest/v3`. The
registry exporter returns v3 by default. Each tool definition has a name,
service, source, operation class, route, description, and enabled flag.

Connector routes use `connector_binding` with explicit `agent_id` and
`connector_id`, plus non-empty `required_capabilities`; `resource_types` are
optional. Harness routes use `harness_executor` with executor and registration
identifiers and omit connector-only fields. Registration metadata is explicit
and does not grant permissions. The distributed proxy remains the connector
runtime and policy enforcement point; Kei receives metadata only.

The shared example is `fixtures/kei/tool-manifest.v3.json`. ToolSpec
registrars send this definition through `POST /v1/tools:register` over the
runtime socket. The HTTP sync helpers accept JSON text or an object, send it as
a whole manifest, and leave schema validation to the proxy.

## Validate the shared fixture

```bash
(cd go && go run ./cmd/lint-tools ../fixtures/kei/tool-manifest.v3.json)
PYTHONPATH=python/src python -m haikeilabs.agentware.lint_tools fixtures/kei/tool-manifest.v3.json
(cd typescript && npm run build && node dist/tools/lint-cli.js ../fixtures/kei/tool-manifest.v3.json)
```
