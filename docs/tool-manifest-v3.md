# Tool manifest v3 connector route contract

V3 connector routes require explicit `agent_id` and `connector_id` values:

```json
{
  "route": {
    "connector_binding": {
      "agent_id": "<agent UUID>",
      "connector_id": "<connector UUID>"
    }
  },
  "required_capabilities": ["issue.read"]
}
```

At sync, the catalog uses only the authenticated runtime installation, organization, and workspace scope. It requires the named agent to be assigned to that installation, then checks the exact `(workspace_id, agent_id, connector_id)` row in `abac.agent_connector_bindings`. Every declared `required_capabilities` value must occur in that row's capabilities. It does not use a default agent, the caller's identity, any-agent lookup, or a union across bindings. Missing, malformed, unassigned, unbound, or under-capable routes are rejected before persistence.

Harness-native routes remain `{ "harness_executor": { "executor": "...", "registration": "..." } }` and omit connector-only fields, including `required_capabilities` and `resource_types`. V2 negotiation/decoding remains compatible and unchanged. V3 requires explicit version negotiation; rejection is fail-closed and occurs before tool sync writes.

The shared wire example is `fixtures/kei/tool-manifest.v3.json` in the Agentware SDK repository. The catalog persists route metadata, but this registration is not itself a grant; policy enforcement and provider execution remain separate boundaries.
