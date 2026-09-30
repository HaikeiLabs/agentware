# Kei Tool Manifest

A governed tool declares its Kei scope (service, action, resource patterns) in
the agentware SDK. The `ToolsRegistry` can then export a JSON manifest that an
admin loads into the Kei policy catalog's tool registry once, offline.
Agentware never calls the catalog tool API at runtime, and harnesses never pass
an authorize resource — the catalog decides resources from the declared scope.

## Flow

```
1. Declare  →  tool implements GovernedTool with a KeiScope
2. Export   →  registry.ExportKeiToolManifest() produces JSON
3. Load     →  admin POSTs the JSON to POST /api/v1/tools
               (via kei CLI, skill, or direct API)
```

## 1. Declare scope on a tool

### Go

```go
import "github.com/soypete/pedro-agentware/go/tools"

type getIssueTool struct{}

func (t *getIssueTool) Name() string                        { return "github.get_issue" }
func (t *getIssueTool) Description() string                 { return "Fetch an issue from a GitHub repository" }
func (t *getIssueTool) Execute(ctx context.Context, args map[string]any) (*tools.Result, error) {
    // execution logic
    return &tools.Result{Success: true, Data: issue}, nil
}
func (t *getIssueTool) KeiScope() tools.KeiScope {
    return tools.KeiScope{
        Service:   "github",
        Action:    "read",
        Resources: []string{"repo:haikeilabs/*", "issue:*"},
    }
}
```

### Python

```python
from pedro_agentware.tools import GovernedTool, KeiScope, Result

class GetIssueTool:
    @property
    def name(self) -> str:
        return "github.get_issue"

    @property
    def description(self) -> str:
        return "Fetch an issue from a GitHub repository"

    def execute(self, args: dict) -> Result:
        # execution logic
        return Result(success=True, data=issue)

    def kei_scope(self) -> KeiScope:
        return KeiScope(
            service="github",
            action="read",
            resources=["repo:haikeilabs/*", "issue:*"],
        )
```

### TypeScript

```typescript
import { GovernedTool, KeiScope, Result } from "@haikeilabs/agentware";

const getIssueTool: GovernedTool = {
  name: "github.get_issue",
  description: "Fetch an issue from a GitHub repository",
  execute(args: Record<string, unknown>): Result {
    // execution logic
    return new Result(true, issue);
  },
  keiScope(): KeiScope {
    return { service: "github", action: "read", resources: ["repo:haikeilabs/*", "issue:*"] };
  },
};
```

## 2. Export the manifest

Register governed tools (and any plain, non-governed tools — they are excluded
from the manifest) on a `ToolRegistry`, then export.

### Go

```go
registry := tools.NewToolRegistry()
registry.Register(&getIssueTool{})
// ... register other tools

manifestJSON, err := registry.ExportKeiToolManifest()
// manifestJSON is indented JSON, sorted by tool name
```

### Python

```python
from pedro_agentware.tools import ToolRegistry

registry = ToolRegistry()
registry.register(get_issue_tool)

manifest_json = registry.export_kei_tool_manifest()
# manifest_json is a pretty-printed JSON string, sorted by tool name
```

### TypeScript

```typescript
import { ToolRegistry } from "@haikeilabs/agentware";

const registry = new ToolRegistry();
registry.register(getIssueTool);

const manifest = registry.exportKeiToolManifest();
const manifestJSON = JSON.stringify(manifest, null, 2);
```

### Output shape

```json
{
  "tools": [
    {
      "name": "github.get_issue",
      "service": "github",
      "description": "Fetch an issue from a GitHub repository",
      "action": "read",
      "resources": ["repo:haikeilabs/*", "issue:*"],
      "enabled": true
    }
  ]
}
```

The fields `id`, `workspace_id`, `org_id`, `version`, `created_at`, and
`updated_at` are server-generated and omitted from the export.

## 3. Admin loads the manifest

The manifest is loaded **once, offline** into the Kei policy catalog via
`POST /api/v1/tools`. An admin typically does this through the `kei` CLI or a
Kei skill.

```bash
kei tools import --file manifest.json
```

After loading, the catalog is the authoritative source for resource patterns
per tool name. The harness never sends an `authorize --resource` flag; it only
sends `--tool <name>`. The catalog resolves the resources from the registered
scope at decision time.

## Runtime boundary

- **Agentware never calls `POST /api/v1/tools` at runtime.** The manifest
  export is a build-time / deployment-time action, not a runtime one.
- **Harnesses never pass an authorize resource.** The `authorize` call from a
  harness sends the tool name only (`--tool`); the catalog is authoritative for
  resource patterns.
- **Non-governed tools are excluded.** Tools that do not implement
  `GovernedTool` / `kei_scope()` / `keiScope()` are silently omitted from the
  manifest. They continue to work locally but are not registered in the catalog.
- **Deterministic output.** Entries are sorted by tool name so the manifest can
  be committed and diffed.
- **Opt-in.** Existing tools compile unchanged. Only tools that explicitly
  declare a `KeiScope` appear in the manifest.

See the shared fixture at `fixtures/kei/tool-manifest.v1.json` for a complete
example with five governed tools across GitHub, Linear, email, and Slack
services.
