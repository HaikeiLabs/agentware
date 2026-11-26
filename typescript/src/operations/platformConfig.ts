import { getOption, hasOption, type DescFile } from "@bufbuild/protobuf";
import {
  connector as connectorOption,
  ConnectionKind as ContractConnectionKind,
  type Connector,
} from "@haikeilabs/connector-contracts/kei/contracts/v1/options_pb";
import {
  KeiProxySocketError,
  type KeiProxyAgentConfig,
  type KeiProxyAgentConnector,
  type KeiProxyAgentTool,
} from "../kei/socketAuthorizeClient.js";
import type { ConnectionKind } from "./connections.js";
import type { ConnectorConfig, OperationBinding } from "./runner.js";

/**
 * The runner's ConnectorConfig, built from the platform.
 *
 * Connectors are set up in the Kei platform and bound to the agent there, so
 * a harness learns them at startup the way it learns its model profile: it
 * asks kei-proxy (`GET /v1/agent-config`, kei-proxy 0.12.0 or later) and names
 * no connector ID itself. Mirrors the Python and Go `connector_config` /
 * `ConnectorConfigs`.
 */

/** The generated operation bindings a harness serves, per registered tool: operation id -> binding. */
export type ToolBindings = Record<string, Record<string, OperationBinding>>;

/** What `connectorConfig` reads; the socket client satisfies it. */
export interface AgentConfigSource {
  agentConfig(): Promise<KeiProxyAgentConfig>;
}

/**
 * kei-proxy could not say which connectors the agent has: a daemon older than
 * 0.12.0 (404), one without a fresh v2 policy bundle (503
 * `agent_config_unavailable`), an unreachable daemon or a malformed answer.
 * Callers fail closed. `reasonClass` and `code` are the socket error's.
 */
export class ConnectorsUnavailable extends Error {
  readonly reasonCode = "connectors_unavailable";
  constructor(
    readonly reasonClass = "",
    readonly code = "",
  ) {
    super(
      `Connectors unavailable${reasonClass ? ` (${reasonClass}${code ? `: ${code}` : ""})` : ""}`,
    );
    this.name = "ConnectorsUnavailable";
  }
}

const CONTRACT_FILE = /^kei\/connectors\/([a-z0-9_]+)\/(v[0-9]+)\/[a-z0-9_]+$/;

/**
 * Build the runner's ConnectorConfig for the tools in `bindings`, keyed by
 * tool name. Each entry has an empty `connectorId`: the runner releases the
 * credential with the tool and its arguments, and kei-proxy derives the
 * connector from the tool's binding. The connection name and base URL come
 * from the agent config; the kind and header from the contracts Connection
 * of that name on the operations' contract connector; the binding version
 * from the tool.
 *
 * A tool is left out (the runner then denies it `connector_not_named`) when
 * the platform does not bind it to this agent, the installation does not name
 * its connection yet, or the connection is not the contract connector's.
 * Rejects with `ConnectorsUnavailable` when the agent config cannot be read,
 * and with a `TypeError` when one tool's bindings span more than one contract
 * connector.
 */
export async function connectorConfig(
  client: AgentConfigSource,
  bindings: ToolBindings,
): Promise<ConnectorConfig> {
  let config: KeiProxyAgentConfig;
  try {
    config = await client.agentConfig();
  } catch (error) {
    if (error instanceof KeiProxySocketError)
      throw new ConnectorsUnavailable(error.reasonClass, error.code);
    throw new ConnectorsUnavailable();
  }
  if (!config || !Array.isArray(config.connectors))
    throw new ConnectorsUnavailable("malformed_response");

  const bound = new Map<string, [KeiProxyAgentConnector, KeiProxyAgentTool]>();
  for (const connector of config.connectors) {
    if (!connector || !Array.isArray(connector.tools)) continue;
    for (const tool of connector.tools)
      if (tool && typeof tool.tool === "string") bound.set(tool.tool, [connector, tool]);
  }

  const entries: ConnectorConfig = {};
  for (const [toolName, operations] of Object.entries(bindings)) {
    const prefix = contractPrefix(toolName, operations);
    const match = bound.get(toolName);
    if (!prefix || !match) continue;
    const [connector, tool] = match;
    const connectionName = connector.connection;
    if (typeof connectionName !== "string" || !connectionName) continue;
    const contract = await contractConnector(prefix, operations);
    const spec = contract?.connections.find((c) => c.name === connectionName);
    if (!contract || !spec) continue;
    const kind = ContractConnectionKind[spec.kind]?.toLowerCase() as ConnectionKind | undefined;
    if (!kind || kind === ("unspecified" as string)) continue;
    const version = typeof tool.binding_version === "string" ? tool.binding_version : "";
    entries[toolName] = {
      connectorId: "",
      connectionName,
      kind,
      baseURL: connector.base_url || spec.baseUrl || contract.baseUrl,
      ...(spec.headerName ? { headerName: spec.headerName } : {}),
      operations: Object.fromEntries(
        Object.entries(operations).map(([id, binding]) => [
          id,
          version ? { ...binding, bindingVersion: version } : binding,
        ]),
      ),
    };
  }
  return entries;
}

/** The one contract connector (`github`) the tool's operation ids share. */
function contractPrefix(
  tool: string,
  operations: Record<string, OperationBinding>,
): string | undefined {
  const prefixes = new Set(Object.keys(operations).map((id) => id.split(".")[0]));
  if (prefixes.size > 1)
    throw new TypeError(`tool ${tool} binds operations of more than one contract connector`);
  return [...prefixes][0];
}

/**
 * The contracts `Connector` declared beside the bindings' request messages:
 * each generated connector package declares it in `connector_pb`, imported
 * from the first binding's package; its id must be the operations' prefix.
 */
async function contractConnector(
  prefix: string,
  operations: Record<string, OperationBinding>,
): Promise<Connector | undefined> {
  const first = Object.values(operations)[0];
  const match = CONTRACT_FILE.exec(first?.requestSchema.file.name ?? "");
  if (!match) return undefined;
  let module: Record<string, unknown>;
  try {
    module = (await import(
      `@haikeilabs/connector-contracts/kei/connectors/${match[1]}/${match[2]}/connector_pb`
    )) as Record<string, unknown>;
  } catch {
    return undefined;
  }
  for (const value of Object.values(module)) {
    const file = value as DescFile;
    if (file?.kind !== "file" || !hasOption(file, connectorOption)) continue;
    const contract = getOption(file, connectorOption);
    return contract.id === prefix ? contract : undefined;
  }
  return undefined;
}
