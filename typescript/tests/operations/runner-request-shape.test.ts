// The request shape kei-connector-runtime proxy-v0.11.0 decides a
// connector_binding tool with: authorize carries the tool and its arguments
// (decideV2ConnectorPlan runs only when tool_args is set; without them a v2
// bundle denies preflight_required), and the credential release carries the
// bare operation id as its capability (the policy-set capability pattern
// ^[a-z0-9_]+(\.[a-z0-9_]+)*$ rejects "connector:") plus the same arguments,
// from which the runtime re-derives the plan before releasing.
import { jest } from "@jest/globals";
import { create } from "@bufbuild/protobuf";
import type { HTTP } from "@haikeilabs/connector-contracts/kei/connection/index";
import { spawn, type ChildProcess } from "node:child_process";
import { mkdtempSync, rmSync, existsSync } from "node:fs";
import { createServer, type Server } from "node:http";
import { join, resolve } from "node:path";
import { createInterface } from "node:readline";
import {
  OP_GET_ISSUE,
  OP_LIST_PULLS,
  GithubQueries,
} from "@haikeilabs/connector-contracts/kei/connectors/github/v1/ops";
import { GetIssueRequestSchema } from "@haikeilabs/connector-contracts/kei/connectors/github/v1/issues_pb";
import { ListPullsRequestSchema } from "@haikeilabs/connector-contracts/kei/connectors/github/v1/pulls_pb";
import { KeiProxySocketAuthorizeClient } from "../../src/kei/socketAuthorizeClient.js";
import {
  OperationRunner,
  OperationDenied,
  type OperationAudit,
  type OperationSocketClient,
} from "../../src/operations/index.js";

const secret = "credential-DO-NOT-LEAK";
const caller = { trusted: false, parent_span: "p", delegation_depth: 1 };
const toolArgs = { owner: "acme", repository: "repo", issue_number: 2 };
const toolContext = { channel: "c1" };
const issueRequest = create(GetIssueRequestSchema, {
  owner: "acme",
  repository: "repo",
  issueNumber: 2n,
});

const servers: Server[] = [];
const cleanups: (() => void | Promise<void>)[] = [];
afterEach(async () => {
  await Promise.all(servers.splice(0).map((s) => new Promise<void>((r) => s.close(() => r()))));
  for (const cleanup of cleanups.splice(0)) await cleanup();
});

/** A fake provider; records each request's path and Authorization header. */
async function provider(body: unknown) {
  const seen: { path: string; auth: string }[] = [];
  const server = createServer((req, res) => {
    seen.push({ path: req.url ?? "", auth: req.headers.authorization ?? "" });
    res.writeHead(200, { "content-type": "application/json" });
    res.end(JSON.stringify(body));
  });
  servers.push(server);
  await new Promise<void>((r) => server.listen(0, "127.0.0.1", r));
  return { url: `http://127.0.0.1:${(server.address() as { port: number }).port}`, seen };
}

function connectorsFor(baseURL: string, connectorId = "installed-github") {
  return {
    github: {
      connectorId,
      connectionName: "pat",
      kind: "http_bearer" as const,
      baseURL,
      operations: {
        [OP_GET_ISSUE.id]: { requestSchema: GetIssueRequestSchema, queries: (c: HTTP) => new GithubQueries(c) },
        [OP_LIST_PULLS.id]: { requestSchema: ListPullsRequestSchema, queries: (c: HTTP) => new GithubQueries(c) },
      },
    },
  };
}

function fakeClient() {
  return {
    authorize: jest.fn<OperationSocketClient["authorize"]>().mockResolvedValue({ decision: "allow" }),
    releaseCredential: jest
      .fn<OperationSocketClient["releaseCredential"]>()
      .mockResolvedValue({ decision: "permit", credential: { access_token: secret } }),
  };
}

describe("OperationRunner request shape", () => {
  it("authorizes the tool call and releases for the bare operation id", async () => {
    const p = await provider({ number: 2, title: "issue" });
    const client = fakeClient();
    const session = { source: "test", externalId: "user", sessionId: "s" };
    await new OperationRunner(client, session, connectorsFor(p.url)).run(caller, OP_GET_ISSUE, issueRequest, {
      tool: "github.get_issue",
      toolArgs,
      toolContext,
    });
    const authorize = client.authorize.mock.calls[0][1];
    expect(authorize).not.toHaveProperty("action");
    expect(authorize).toMatchObject({ tool: "github.get_issue", toolArgs, toolContext });
    expect(client.releaseCredential.mock.calls[0][1]).toMatchObject({
      connectorId: "installed-github",
      capability: "github.issues.get",
      tool: "github.get_issue",
      toolArgs,
      toolContext,
    });
  });

  it("sends no tool arguments when the caller passes none", async () => {
    const p = await provider({ number: 2, title: "issue" });
    const client = fakeClient();
    const session = { source: "test", externalId: "user", sessionId: "s" };
    await new OperationRunner(client, session, connectorsFor(p.url)).run(caller, OP_GET_ISSUE, issueRequest);
    expect(client.authorize.mock.calls[0][1]).not.toHaveProperty("toolArgs");
    expect(client.authorize.mock.calls[0][1]).not.toHaveProperty("action");
    expect(client.releaseCredential.mock.calls[0][1]).not.toHaveProperty("toolArgs");
    expect(client.releaseCredential.mock.calls[0][1]).toHaveProperty("capability", "github.issues.get");
  });

  it("puts the bare operation id and tool arguments on the wire", async () => {
    // A Unix-socket daemon behind the real socket client records the bodies.
    const dir = mkdtempSync("/tmp/aw-op-");
    cleanups.push(() => rmSync(dir, { recursive: true, force: true }));
    const socketPath = join(dir, "rt.sock");
    const bodies: Record<string, Record<string, unknown>> = {};
    const daemon = createServer((req, res) => {
      let raw = "";
      req.on("data", (c) => (raw += c));
      req.on("end", () => {
        bodies[req.url ?? ""] = raw ? JSON.parse(raw) : {};
        res.writeHead(200, { "content-type": "application/json" });
        if (req.url === "/v1/authorize")
          res.end(JSON.stringify({ schema: "kei.authorize-response/v1", decision: "permit" }));
        else
          res.end(
            JSON.stringify({
              schema: "kei.credential-release-response/v1",
              decision: "permit",
              credential: { access_token: secret },
            }),
          );
      });
    });
    servers.push(daemon);
    await new Promise<void>((r) => daemon.listen(socketPath, r));
    const p = await provider({ number: 2, title: "issue" });
    const client = new KeiProxySocketAuthorizeClient({ socketPath, env: { KEI_RUNTIME_TOKEN: "kh_test" } });
    const session = { source: "test", externalId: "user", sessionId: "s" };
    await new OperationRunner(client, session, connectorsFor(p.url)).run(caller, OP_GET_ISSUE, issueRequest, {
      tool: "github.get_issue",
      toolArgs,
      toolContext,
    });

    const authorize = bodies["/v1/authorize"];
    const release = bodies["/v1/credentials:release"];
    expect(authorize.tool).toBe("github.get_issue");
    expect(authorize).not.toHaveProperty("action");
    expect(authorize).not.toHaveProperty("connector");
    expect(authorize).toMatchObject({ tool_args: toolArgs, tool_context: toolContext });
    expect(release).toMatchObject({
      connector: { connector_id: "installed-github", capability: "github.issues.get" },
      resource: "repos/acme/repo",
      tool: "github.get_issue",
      tool_args: toolArgs,
      tool_context: toolContext,
    });
    expect(JSON.stringify(bodies)).not.toContain("connector:");
  });
});

// End to end against a real `kei-proxy serve` bootstrapped from
// fixtures/kei/fake-control-plane.py's operation-plan bundle. Skipped unless
// KEI_PROXY_TEST_BINARY names a kei-proxy binary (proxy-v0.11.0 or later).
const binary = process.env.KEI_PROXY_TEST_BINARY ?? "";
const realProxy = binary && existsSync(binary) ? describe : describe.skip;
const runtimeToken = "kh_live_ci_dummy_token_0123456789abcdef0123456789abcdef0123456789abcdef";
const connectorToken = "fake-connector-token-e2e";
const fakeControlPlane = resolve(process.cwd(), "..", "fixtures", "kei", "fake-control-plane.py");

async function startRealProxy() {
  const root = mkdtempSync("/tmp/kp-op-");
  const procs: ChildProcess[] = [];
  cleanups.push(async () => {
    // Wait for each process to exit so the daemon stops writing its state.
    await Promise.all(
      procs.map((p) =>
        p.exitCode !== null || p.signalCode !== null
          ? Promise.resolve()
          : new Promise<void>((r) => {
              p.once("exit", () => r());
              p.kill();
            }),
      ),
    );
    rmSync(root, { recursive: true, force: true, maxRetries: 5 });
  });
  const cp = spawn("python3", [fakeControlPlane], {
    env: {
      ...process.env,
      FAKE_CONTROL_PLANE_BUNDLE: "operation-plan",
      FAKE_CONTROL_PLANE_CONNECTOR_TOKEN: connectorToken,
    },
    stdio: ["ignore", "pipe", "ignore"],
  });
  procs.push(cp);
  const port: string = await new Promise((resolve) =>
    createInterface({ input: cp.stdout! }).once("line", resolve),
  );
  const url = `http://127.0.0.1:${port.trim()}`;
  const socketPath = join(root, "rt.sock");
  const env: Record<string, string> = {
    PATH: process.env.PATH ?? "",
    HOME: root,
    KEI_RUNTIME_TOKEN: runtimeToken,
    KEI_RUNTIME_CONTROL_PLANE_URL: url,
    KEI_RUNTIME_VERSION: "e2e",
    KEI_RUNTIME_STATE_DIR: join(root, "state"),
    KEI_RUNTIME_SOCKET_PATH: socketPath,
    KEI_PROXY_LISTEN_ADDR: "127.0.0.1:0",
    KEI_PROXY_AUDIT: join(root, "audit.jsonl"),
    KEI_CREDENTIAL_RELEASE: "identity",
    KEI_CREDENTIAL_RELEASE_URL: url,
    ENV: "development",
  };
  procs.push(spawn(binary, ["serve"], { env, stdio: "ignore" }));
  const client = new KeiProxySocketAuthorizeClient({ socketPath, env });
  const deadline = Date.now() + 30_000;
  while (Date.now() < deadline) {
    const state = await client.policyBundleStatus().then((s) => s.state, () => "");
    if (state === "active") return client;
    await new Promise((r) => setTimeout(r, 100));
  }
  throw new Error("kei-proxy serve did not activate the operation-plan bundle");
}

async function runListPulls(args: Record<string, unknown>) {
  const client = await startRealProxy();
  const session = await client.openSession({ source: "ci", externalId: "ci-user" });
  const p = await provider([{ number: 7, title: "e2e" }]);
  const audit: OperationAudit[] = [];
  const runner = new OperationRunner(client, session, connectorsFor(p.url, "conn-github-ci"), (a) => audit.push(a));
  const run = runner.run(
    caller,
    OP_LIST_PULLS,
    create(ListPullsRequestSchema, { owner: "acme", repository: "widgets" }),
    { tool: "github.list_pulls", toolArgs: args, toolContext: {} },
  );
  return { run, p, audit };
}

realProxy("OperationRunner against a real kei-proxy serve", () => {
  jest.setTimeout(60_000);

  it("permits and releases the credential for a planned tool", async () => {
    const { run, p, audit } = await runListPulls({ owner: "acme", repository: "widgets" });
    await run;
    expect(p.seen).toEqual([{ path: "/repos/acme/widgets/pulls", auth: `Bearer ${connectorToken}` }]);
    expect(audit).toEqual([{ operationId: "github.pulls.list", decision: "allow", outcome: "success" }]);
  });

  it("denies tool arguments that violate the args_schema", async () => {
    const { run, p, audit } = await runListPulls({ owner: "acme", repository: "widgets", extra: "x" });
    const error = await run.catch((e: unknown) => e);
    expect(error).toBeInstanceOf(OperationDenied);
    expect((error as OperationDenied).reasonCode).toBe("operation_plan_invalid");
    expect(String(error)).not.toContain(connectorToken);
    expect(p.seen).toEqual([]);
    expect(audit).toEqual([{ operationId: "github.pulls.list", decision: "deny", outcome: "denied" }]);
  });
});
