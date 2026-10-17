# Deny reason codes and hints

Every deny response from kei-proxy carries a `reason_code` that explains why
the tool call was denied, and an actionable `hint` describing the next step a
developer should take. The SDK surfaces both fields unchanged from the runtime
response.

| Reason code | Hint |
|---|---|
| `agent_not_bound_to_connector` | Bind the agent to the connector on the console Connectors page (kei-cli has no bind command). |
| `agent_not_assigned` | Assign an agent to this runtime installation on the console Agents page so the runtime identity has a default agent. |
| `credential_unavailable` | Connect the provider on the console Connectors page (or deliver the credential) so the runtime can resolve the connector credential. |
| `descriptor_invalid` | The connector metadata is invalid. Check the connector setup on the console Connectors page. |
| `enrollment_required` | Link the provider identity to a Kei user on the console Users page to enable connector access. |
| `insecure_transport` | OAuth credentials are only returned over TLS. Use HTTPS when connecting to the catalog. |
| `no_matching_policy` | Add a policy for this source and target: kei policies create --name <name> --src-pattern <src> --dst-pattern <dst> --effect permit |
| `oauth_binding_mismatch` | The OAuth connection is bound to another scope. Reconnect the provider in the correct workspace on the console Connectors page. |
| `oauth_bridge_unavailable` | The OAuth bridge is unavailable. Check the bridge configuration and ensure the service is reachable. |
| `oauth_not_connected` | Connect the provider account on the console Connectors page to enable OAuth-based access. |
| `oauth_reconnect_required` | Reconnect the provider account on the console Connectors page; the connection needs to be refreshed. |
| `policy_deny` | Add a permit policy for this subject and connector: kei policies create --name <name> --src-pattern <src> --dst-pattern <dst> --effect permit |
| `subject_missing` | Link the invoking user to this installation on the console Users page so a subject can be resolved. |
| `tool_not_registered` | Register the harness tools: kei harness sync --harness custom --manifest tools.json |

A `reason_code` absent from this table gets no hint, and the response omits the
`hint` field entirely rather than offering a generic or wrong command.
