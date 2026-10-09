import { createServer, type Server } from "node:http";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { KeiProxySocketAuthorizeClient } from "../src/index.js";
import type { KeiProxyAuthorizeRequest } from "../src/index.js";

const request: KeiProxyAuthorizeRequest = {
  userId: "ignored-not-session-identity", tool: "github.read", action: "read", resource: "repo:x/y",
  spanId: "", invokingSubject: "", parentSpan: "", delegationDepth: 0, agentId: "", agentVersion: "",
  framework: "", toolArgsDigest: "", resources: [], workspaceId: "",
};
const token = "socket-test-token";

describe("KeiProxySocketAuthorizeClient", () => {
  let server: Server;
  let dir: string;
  let socketPath: string;
  let client: KeiProxySocketAuthorizeClient;
  let sessions = 0;
  let authorizations = 0;
  let failStatus = 0;
  let malformed = false;
  let unauthorized = false;
  let decision = "permit";
  let sessionMissingOnce = false;
  let stall = false;
  let onAuthorize: ((req: any) => void) | undefined;

  beforeEach(async () => {
    dir = mkdtempSync(join(tmpdir(), "kei-socket-test-"));
    socketPath = join(dir, "runtime.sock");
    sessions = 0; authorizations = 0; failStatus = 0; malformed = false; unauthorized = false; decision = "permit"; sessionMissingOnce = false; stall = false; onAuthorize = undefined;
    server = createServer((req, res) => {
      if (unauthorized || req.headers.authorization !== `Bearer ${token}`) { res.writeHead(401).end(); return; }
      const chunks: Buffer[] = [];
      req.on("data", (chunk: Buffer) => chunks.push(chunk));
      req.on("end", () => {
        const body = JSON.parse(Buffer.concat(chunks).toString());
        if (req.url === "/v1/session") {
          sessions++;
          expect(req.method).toBe("PUT");
          expect(body).toMatchObject({ schema: "kei.session/v2", source: "teams", external_id: expect.any(String) });
          res.writeHead(201, { "Content-Type": "application/json" }).end(JSON.stringify({ schema: "kei.session/v2", session_id: `s${sessions}` }));
          return;
        }
        authorizations++;
        expect(req.headers["x-kei-session"]).toBeTruthy();
        if (stall) return;
        onAuthorize?.(req);
        if (failStatus) { res.writeHead(failStatus, { "Content-Type": "application/json" }).end(JSON.stringify({ error: { code: "session_not_found" } })); return; }
        if (sessionMissingOnce) { sessionMissingOnce = false; res.writeHead(200, { "Content-Type": "application/json" }).end(JSON.stringify({ schema: "kei.authorize-response/v1", decision: "deny", reason_code: "session_not_found" })); return; }
        res.writeHead(200, { "Content-Type": "application/json" }).end(malformed ? "not-json" : JSON.stringify({ schema: "kei.authorize-response/v1", decision }));
      });
    });
    await new Promise<void>((resolve) => server.listen(socketPath, resolve));
    client = new KeiProxySocketAuthorizeClient({ socketPath, env: { KEI_RUNTIME_TOKEN: token } });
  });
  afterEach(async () => { await new Promise<void>((resolve) => server.close(() => resolve())); rmSync(dir, { recursive: true, force: true }); });

  test("creates/reuses named session and sends its header; explicit permit passes through", async () => {
    const session = await client.openSession({ source: "teams", externalId: "aad-id" });
    expect(await client.openSession({ source: "teams", externalId: "aad-id" })).toBe(session);
    onAuthorize = (req) => expect(req.headers["x-kei-session"]).toBe(session.sessionId);
    expect((await client.authorize(session, request)).decision).toBe("permit");
    expect(sessions).toBe(1);
  });

  test("does not infer identity and denies missing identity/token", async () => {
    await expect(client.openSession({ source: "", externalId: "x" })).rejects.toThrow();
    const noToken = new KeiProxySocketAuthorizeClient({ socketPath, env: {} });
    await expect(noToken.openSession({ source: "teams", externalId: "x" })).rejects.toThrow(/KEI_RUNTIME_TOKEN/);
    expect(sessions).toBe(0);
  });

  test("keeps denials explicit", async () => {
    const session = await client.openSession({ source: "teams", externalId: "aad-id" });
    decision = "deny";
    expect((await client.authorize(session, request)).decision).toBe("deny");
  });

  test("reopens once when daemon reports a missing session", async () => {
    const session = await client.openSession({ source: "teams", externalId: "aad-id" });
    sessionMissingOnce = true;
    expect((await client.authorize(session, request)).decision).toBe("permit");
    expect(sessions).toBe(2);
    expect(authorizations).toBe(2);
  });

  test("fails closed on unauthorized, malformed response, and unavailable socket", async () => {
    unauthorized = true;
    await expect(client.openSession({ source: "teams", externalId: "x" })).rejects.toThrow();
    unauthorized = false;
    failStatus = 0; malformed = true;
    const session = await client.openSession({ source: "teams", externalId: "x" });
    await expect(client.authorize(session, request)).rejects.toThrow(/malformed/);
    const down = new KeiProxySocketAuthorizeClient({ socketPath: join(dir, "missing.sock"), timeoutMs: 100, env: { KEI_RUNTIME_TOKEN: token } });
    const downError = await down.openSession({ source: "teams", externalId: "x" }).catch((error: unknown) => error instanceof Error ? error : new Error("unexpected success"));
    expect((downError as Error).message).toMatch(/unavailable/);
    expect((downError as Error).message).not.toContain(token);
    stall = true;
    const fast = new KeiProxySocketAuthorizeClient({ socketPath, timeoutMs: 20, env: { KEI_RUNTIME_TOKEN: token } });
    const fastSession = await fast.openSession({ source: "teams", externalId: "timeout" });
    const timeoutError = await fast.authorize(fastSession, request).catch((error: unknown) => error instanceof Error ? error : new Error("unexpected success"));
    expect(timeoutError.message).toMatch(/unavailable or timed out/);
    expect(timeoutError.message).not.toContain(token);
  });
});
