# TypeScript minimal harness

This small TypeScript harness authorizes an `echo` tool before it runs. The
copyable `index.ts` reads `KEI_RUNTIME_SOCKET_PATH` and uses the socket client.
The runtime token comes from `KEI_RUNTIME_TOKEN`; the agent ID is discovered
by the runtime installation.

Requires Node 22.18 or later. Run `npm ci` and `npm run typecheck` first. For an offline run, use
`npm start -- --fake`; `cli.ts` starts `fake-proxy.ts` and then runs the
harness. The fake permits `echo`, denies `delete_everything` with separate
reason code and hint fields, and prints one audit line per authorization.

For a registered harness, follow `kei bot init` →
`kei harness add --kind custom --installation INSTALLATION_ID` → tool
registration → `kei-proxy serve` → call. Use the `installation_id` returned by
`kei bot init`; the CLI resolves its assigned agent by default. If none is
assigned, use `kei bot agents add --installation INSTALLATION_ID --agent
AGENT_ID --default` first. See the [runtime setup guide](../../../docs/getting-started/runtime-setup.md)
for ToolSpec registration, group policies, and audit entries.
