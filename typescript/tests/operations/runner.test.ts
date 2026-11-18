import { jest } from "@jest/globals";
import { create } from "@bufbuild/protobuf";
import type { HTTP } from "@haikeilabs/connector-contracts/kei/connection/index";
import type { OperationSocketClient } from "../../src/operations/index.js";
import { createServer, type Server } from "node:http";
import {
  OP_GET_ISSUE,
  GithubQueries,
} from "@haikeilabs/connector-contracts/kei/connectors/github/v1/ops";
import { GetIssueRequestSchema } from "@haikeilabs/connector-contracts/kei/connectors/github/v1/issues_pb";
import {
  OperationRunner,
  OperationDenied,
  EnrollmentRequired,
  ConnectRequired,
  UnsupportedConnection,
} from "../../src/operations/index.js";

const session = { source: "test", externalId: "user", sessionId: "s" };
const caller = { trusted: false, parent_span: "p", delegation_depth: 1 };
const request = create(GetIssueRequestSchema, {
  owner: "acme",
  repository: "repo",
  issueNumber: 2n,
});
const secret = "credential-DO-NOT-LEAK";
let server: Server | undefined;
afterEach(async () => {
  if (server) await new Promise<void>((r) => server!.close(() => r()));
  server = undefined;
  jest.restoreAllMocks();
});
async function setup(status = 200) {
  let requests = 0;
  server = createServer((req, res) => {
    requests++;
    expect(req.headers.authorization).toBe(`Bearer ${secret}`);
    res.writeHead(status, { "content-type": "application/json" });
    res.end(
      status === 200 ? JSON.stringify({ number: 2, title: "issue" }) : secret,
    );
  });
  await new Promise<void>((r) => server!.listen(0, "127.0.0.1", r));
  const port = (server.address() as { port: number }).port;
  const client = {
    authorize: jest.fn<OperationSocketClient["authorize"]>().mockResolvedValue({ decision: "allow" }),
    releaseCredential: jest
      .fn<OperationSocketClient["releaseCredential"]>()
      .mockResolvedValue({
        decision: "permit",
        credential: { access_token: secret },
      }),
  };
  const audit = jest.fn();
  const build = jest.fn((conn: HTTP) => new GithubQueries(conn));
  const connectors = {
    github: {
      connectorId: "installed-github",
      connectionName: "pat",
      kind: "http_bearer" as const,
      baseURL: `http://127.0.0.1:${port}`,
      operations: {
        [OP_GET_ISSUE.id]: {
          requestSchema: GetIssueRequestSchema,
          queries: build,
        },
      },
    },
  };
  const runner = new OperationRunner(client, session, connectors, audit);
  return { runner, client, audit, build, requests: () => requests, connectors };
}
describe("OperationRunner", () => {
  it("authorizes then releases and calls a generated operation", async () => {
    const x = await setup();
    const result = await x.runner.run<typeof request, { title: string }>(
      caller,
      OP_GET_ISSUE,
      request,
      { tool: "issue" },
    );
    expect(result.title).toBe("issue");
    expect(x.client.authorize).toHaveBeenCalledWith(
      session,
      expect.objectContaining({
        action: "connector:github.issues.get",
        resource: "repos/acme/repo",
        tool: "issue",
        parentSpan: "p",
        delegationDepth: 1,
      }),
    );
    expect(x.client.releaseCredential).toHaveBeenCalledWith(
      session,
      expect.objectContaining({
        connectorId: "installed-github",
        capability: "connector:github.issues.get",
        resource: "repos/acme/repo",
        tool: "issue",
      }),
    );
    expect(x.requests()).toBe(1);
    expect(x.audit).toHaveBeenCalledWith({
      operationId: OP_GET_ISSUE.id,
      decision: "allow",
      outcome: "success",
    });
  });
  it.each(["deny", "enrollment_required", "connect_required"])(
    "%s never builds a connection",
    async (decision) => {
      const x = await setup();
      const enrollment = { url: "claim" };
      const connect = { url: "connect" };
      x.client.authorize.mockResolvedValue({
        decision,
        reason_code: "connector_not_named",
        ...(decision === "enrollment_required"
          ? { enrollment }
          : decision === "connect_required"
            ? { connect }
            : {}),
      });
      const error = await x.runner
        .run(caller, OP_GET_ISSUE, request)
        .catch((e) => e);
      expect(error).toBeInstanceOf(
        decision === "deny"
          ? OperationDenied
          : decision === "enrollment_required"
            ? EnrollmentRequired
            : ConnectRequired,
      );
      if (decision === "deny")
        expect((error as OperationDenied).reasonCode).toBe("connector_not_named");
      if (decision === "enrollment_required")
        expect((error as EnrollmentRequired).enrollment).toBe(enrollment);
      if (decision === "connect_required") expect((error as ConnectRequired).connect).toBe(connect);
      expect(x.client.releaseCredential).not.toHaveBeenCalled();
      expect(x.build).not.toHaveBeenCalled();
      expect(x.requests()).toBe(0);
    },
  );
  it.each(["deny", "timeout", "unavailable"])(
    "release %s fails closed before connection",
    async (failure) => {
      const x = await setup();
      if (failure === "deny")
        x.client.releaseCredential.mockResolvedValue({
          decision: "deny",
          reason_code: "no_credential",
        });
      else
        x.client.releaseCredential.mockRejectedValue(
          new Error(failure + secret),
        );
      await expect(
        x.runner.run(caller, OP_GET_ISSUE, request),
      ).rejects.toBeInstanceOf(OperationDenied);
      expect(x.build).not.toHaveBeenCalled();
      expect(x.requests()).toBe(0);
    },
  );
  it.each([401, 500])(
    "does not expose credential in logs, audit or errors for provider %s",
    async (status) => {
      const x = await setup(status);
      const logs: unknown[] = [];
      jest.spyOn(console, "log").mockImplementation((...a) => {
        logs.push(a);
      });
      jest.spyOn(console, "error").mockImplementation((...a) => {
        logs.push(a);
      });
      jest.spyOn(console, "warn").mockImplementation((...a) => {
        logs.push(a);
      });
      const error = await x.runner
        .run(caller, OP_GET_ISSUE, request)
        .catch((e) => e);
      expect(
        JSON.stringify([String(error), logs, x.audit.mock.calls]),
      ).not.toContain(secret);
      expect(x.requests()).toBe(1);
    },
  );
  it("renders an explicit resource template override", async () => {
    const x = await setup();
    await x.runner.run(caller, OP_GET_ISSUE, request, {
      resourceTemplateOverride: "orgs/{owner}",
    });
    expect(x.client.authorize).toHaveBeenCalledWith(
      session,
      expect.objectContaining({ resource: "orgs/acme" }),
    );
  });
  it.each(["timeout", "unavailable"])(
    "authorization %s fails closed",
    async (kind) => {
      const x = await setup();
      x.client.authorize.mockRejectedValue(new Error(kind));
      await expect(
        x.runner.run(caller, OP_GET_ISSUE, request),
      ).rejects.toBeInstanceOf(OperationDenied);
      expect(x.client.releaseCredential).not.toHaveBeenCalled();
      expect(x.build).not.toHaveBeenCalled();
      expect(x.requests()).toBe(0);
    },
  );
  it("rejects SQL before building or releasing", async () => {
    const x = await setup();
    (x.connectors.github as any).kind = "sql_dsn";
    await expect(
      x.runner.run(caller, OP_GET_ISSUE, request),
    ).rejects.toBeInstanceOf(UnsupportedConnection);
    expect(x.build).not.toHaveBeenCalled();
    expect(x.requests()).toBe(0);
  });
});
