import { jest } from "@jest/globals";
import { create } from "@bufbuild/protobuf";
import { createServer, type Server } from "node:http";
import {
  OP_GET_ISSUE,
  GithubQueries,
} from "@haikeilabs/connector-contracts/kei/connectors/github/v1/ops";
import { GetIssueRequestSchema } from "@haikeilabs/connector-contracts/kei/connectors/github/v1/issues_pb";
import { ListPullsRequestSchema } from "@haikeilabs/connector-contracts/kei/connectors/github/v1/pulls_pb";
import {
  connectorConfig,
  ConnectorsUnavailable,
  OperationDenied,
  OperationRunner,
  type AgentConfigSource,
  type OperationSocketClient,
  type ToolBindings,
} from "../../src/operations/index.js";
import {
  KeiProxySocketError,
  type KeiProxyAgentConfig,
  type KeiProxyAgentConnector,
} from "../../src/kei/socketAuthorizeClient.js";

const TOOL = "github.get_issue";
const toolArgs = { owner: "acme", repository: "repo", issue_number: 2 };
const session = { source: "test", externalId: "user", sessionId: "s" };
const caller = { trusted: false, parent_span: "p", delegation_depth: 1 };
const secret = "credential-DO-NOT-LEAK";
const request = create(GetIssueRequestSchema, { owner: "acme", repository: "repo", issueNumber: 2n });

function agentConfig(edit?: (c: KeiProxyAgentConnector) => void): KeiProxyAgentConfig {
  const connector: KeiProxyAgentConnector = {
    connector_id: "c-1",
    name: "acme-github",
    kind: "native",
    connection: "pat",
    base_url: "https://ghe.example",
    tools: [{ tool: TOOL, operations: ["get"], binding_version: "v3" }],
  };
  edit?.(connector);
  return { schema: "kei.agent-config/v1", connectors: [connector] };
}

function source(result: KeiProxyAgentConfig | Error = agentConfig()): AgentConfigSource {
  return {
    agentConfig: async () => {
      if (result instanceof Error) throw result;
      return result;
    },
  };
}

function bindings(): ToolBindings {
  return {
    [TOOL]: {
      [OP_GET_ISSUE.id]: { requestSchema: GetIssueRequestSchema, queries: (c) => new GithubQueries(c) },
    },
  };
}

let server: Server | undefined;
afterEach(async () => {
  if (server) await new Promise<void>((r) => server!.close(() => r()));
  server = undefined;
});

describe("connectorConfig", () => {
  it("builds the entry for a bound tool from the platform", async () => {
    const config = await connectorConfig(source(), bindings());
    expect(Object.keys(config)).toEqual([TOOL]);
    const entry = config[TOOL];
    expect(entry.connectorId).toBe("");
    expect(entry.connectionName).toBe("pat");
    expect(entry.kind).toBe("http_bearer");
    expect(entry.headerName).toBeUndefined();
    expect(entry.baseURL).toBe("https://ghe.example");
    expect(entry.operations[OP_GET_ISSUE.id].requestSchema).toBe(GetIssueRequestSchema);
    expect(entry.operations[OP_GET_ISSUE.id].bindingVersion).toBe("v3");
  });

  it("falls back to the contract connector's base URL", async () => {
    const config = await connectorConfig(source(agentConfig((c) => delete c.base_url)), bindings());
    expect(config[TOOL].baseURL).toBe("https://api.github.com");
  });

  it.each<[string, (c: KeiProxyAgentConnector) => void]>([
    ["no connection yet", (c) => delete c.connection],
    ["an unknown connection", (c) => (c.connection = "user_token")],
    ["the tool is not bound", (c) => (c.tools = [])],
    ["another tool is bound", (c) => (c.tools[0].tool = "other.tool")],
  ])("leaves out a tool when %s", async (_name, edit) => {
    expect(await connectorConfig(source(agentConfig(edit)), bindings())).toEqual({});
  });

  it("rejects bindings that span contract connectors", async () => {
    const mixed = bindings();
    mixed[TOOL]["slack.chat.post"] = { requestSchema: ListPullsRequestSchema, queries: (c) => new GithubQueries(c) };
    await expect(connectorConfig(source(), mixed)).rejects.toBeInstanceOf(TypeError);
  });

  it.each([
    new KeiProxySocketError("proxy_error", "503", "agent_config_unavailable"),
    new KeiProxySocketError("proxy_error", "404"),
    new KeiProxySocketError("proxy_unavailable", "down"),
    new KeiProxySocketError("malformed_response", "bad"),
  ])("fails closed when the agent config is unavailable (%s)", async (error) => {
    const rejected = await connectorConfig(source(error), bindings()).catch((e) => e);
    expect(rejected).toBeInstanceOf(ConnectorsUnavailable);
    expect((rejected as ConnectorsUnavailable).reasonCode).toBe("connectors_unavailable");
    expect((rejected as ConnectorsUnavailable).reasonClass).toBe(error.reasonClass);
    expect((rejected as ConnectorsUnavailable).code).toBe(error.code);
  });

  it("fails closed on a malformed agent config", async () => {
    const malformed = { schema: "kei.agent-config/v1" } as unknown as KeiProxyAgentConfig;
    await expect(connectorConfig(source(malformed), bindings())).rejects.toBeInstanceOf(ConnectorsUnavailable);
  });
});

function socket() {
  return {
    authorize: jest.fn<OperationSocketClient["authorize"]>().mockResolvedValue({ decision: "permit" }),
    releaseCredential: jest
      .fn<OperationSocketClient["releaseCredential"]>()
      .mockResolvedValue({ decision: "permit", credential: { access_token: secret } }),
  };
}

describe("OperationRunner with a platform config", () => {
  it("releases without a connector for a platform-configured tool", async () => {
    server = createServer((req, res) => {
      expect(req.headers.authorization).toBe(`Bearer ${secret}`);
      res.writeHead(200, { "content-type": "application/json" });
      res.end(JSON.stringify({ number: 2, title: "issue" }));
    });
    await new Promise<void>((r) => server!.listen(0, "127.0.0.1", r));
    const port = (server.address() as { port: number }).port;
    const config = await connectorConfig(
      source(agentConfig((c) => (c.base_url = `http://127.0.0.1:${port}`))),
      bindings(),
    );
    const client = socket();
    const runner = new OperationRunner(client, session, config);

    const result = await runner.run<typeof request, { title: string }>(caller, OP_GET_ISSUE, request, {
      tool: TOOL,
      toolArgs,
    });

    expect(result.title).toBe("issue");
    expect(client.authorize.mock.calls[0][1]).toHaveProperty("bindingVersion", "v3");
    const release = client.releaseCredential.mock.calls[0][1];
    expect(release).not.toHaveProperty("connectorId");
    expect(release).not.toHaveProperty("capability");
    expect(release).toMatchObject({ tool: TOOL, toolArgs, resource: "repos/acme/repo" });
  });

  it("denies a tool without a connector when its arguments are missing", async () => {
    const config = await connectorConfig(source(), bindings());
    const client = socket();
    const runner = new OperationRunner(client, session, config);
    const error = await runner.run(caller, OP_GET_ISSUE, request, { tool: TOOL }).catch((e) => e);
    expect(error).toBeInstanceOf(OperationDenied);
    expect((error as OperationDenied).reasonCode).toBe("connector_not_named");
    expect(client.authorize).not.toHaveBeenCalled();
    expect(client.releaseCredential).not.toHaveBeenCalled();
  });
});
