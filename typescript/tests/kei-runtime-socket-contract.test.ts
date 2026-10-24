/**
 * Shared contract: testing/contracts/runtime-socket, replayed against the
 * TypeScript socket client and tool sync. Python and Go replay the same files.
 */
import {
  createServer as createHttpServer,
  type IncomingMessage,
  type Server,
  type ServerResponse,
} from "node:http";
import { mkdtempSync, readFileSync, rmSync } from "node:fs";
import type { AddressInfo } from "node:net";
import { join, resolve } from "node:path";
import {
  Action,
  AUTHORIZE_REQUEST_SCHEMA,
  AUTHORIZE_RESPONSE_SCHEMA,
  DEFAULT_MODEL_TIMEOUT_MS,
  DEFAULT_RUNTIME_SOCKET_PATH,
  DEFAULT_SOCKET_TIMEOUT_MS,
  DEFAULT_TOOL_SYNC_ATTEMPTS,
  DEFAULT_TOOL_SYNC_BACKOFF_MS,
  DEFAULT_TOOL_SYNC_TIMEOUT_MS,
  EGRESS_PROXY_ENV,
  KeiProxyEvaluator,
  KeiProxySocketAuthorizationClient,
  KeiProxySocketAuthorizeClient,
  KeiProxySocketError,
  MAX_TOOL_SYNC_BACKOFF_MS,
  NO_PROXY_ENV,
  POLICY_BUNDLE_STATUS_SCHEMA,
  RUNTIME_CONTROL_PLANE_URL_ENV,
  RUNTIME_SOCKET_PATH_ENV,
  SESSION_HEADER,
  SESSION_NOT_FOUND,
  SESSION_SCHEMA,
  TOOL_MANIFEST_V3_MEDIA_TYPE,
  TOOLS_PATH,
  ToolManifestSyncConfig,
  ToolManifestSyncError,
  selectAuthorizationClient,
  syncToolManifest,
} from "../src/index.js";

// Jest runs from typescript/, so the shared fixtures are one level up.
const CONTRACTS = resolve(
  process.cwd(),
  "..",
  "testing",
  "contracts",
  "runtime-socket",
);
// eslint-disable-next-line @typescript-eslint/no-explicit-any
const load = (name: string): any =>
  JSON.parse(readFileSync(resolve(CONTRACTS, name), "utf8"));

const CONSTANTS = load("constants.json");
const CLIENT = load("client-cases.json");
const SYNC = load("tool-sync-cases.json");
const TOKEN: string = CLIENT.token;

// eslint-disable-next-line @typescript-eslint/no-explicit-any
type Json = any;

const named = (list: Json[]): [string, Json][] => list.map((c) => [c.name, c]);

describe("runtime socket constants", () => {
  it("match the shared fixture", () => {
    const s = CONSTANTS.socket;
    expect(DEFAULT_RUNTIME_SOCKET_PATH).toBe(s.default_path);
    expect(RUNTIME_SOCKET_PATH_ENV).toBe(s.path_env);
    expect(SESSION_HEADER).toBe(s.session_header);
    expect(SESSION_NOT_FOUND).toBe(s.session_not_found);
    expect(DEFAULT_SOCKET_TIMEOUT_MS).toBe(s.default_timeout_ms);
    expect(DEFAULT_MODEL_TIMEOUT_MS).toBe(s.default_model_timeout_ms);
    expect(SESSION_SCHEMA).toBe(CONSTANTS.schemas.session);
    expect(AUTHORIZE_REQUEST_SCHEMA).toBe(CONSTANTS.schemas.authorize_request);
    expect(AUTHORIZE_RESPONSE_SCHEMA).toBe(
      CONSTANTS.schemas.authorize_response,
    );
    expect(POLICY_BUNDLE_STATUS_SCHEMA).toBe(
      CONSTANTS.schemas.policy_bundle_status,
    );
    const t = CONSTANTS.tool_sync;
    expect(TOOLS_PATH).toBe(t.path);
    expect(TOOL_MANIFEST_V3_MEDIA_TYPE).toBe(t.media_type);
    expect(RUNTIME_CONTROL_PLANE_URL_ENV).toBe(t.control_plane_url_env);
    expect(EGRESS_PROXY_ENV).toBe(t.egress_proxy_env);
    expect(NO_PROXY_ENV).toBe(t.no_proxy_env);
    expect(DEFAULT_TOOL_SYNC_ATTEMPTS).toBe(t.default_attempts);
    expect(DEFAULT_TOOL_SYNC_BACKOFF_MS).toBe(t.default_backoff_ms);
    expect(MAX_TOOL_SYNC_BACKOFF_MS).toBe(t.max_backoff_ms);
    expect(DEFAULT_TOOL_SYNC_TIMEOUT_MS).toBe(t.default_timeout_ms);
  });
});

interface Seen {
  method: string;
  path: string;
  session: string;
  body: Json;
  authorization: string;
}

/** A fake daemon that answers scripted exchanges in order and records each request. */
async function replayDaemon(
  socketPath: string,
  exchanges: Json[],
): Promise<{ server: Server; seen: Seen[] }> {
  const queue = [...exchanges];
  const seen: Seen[] = [];
  const server = createHttpServer((req, res) => {
    const chunks: Buffer[] = [];
    req.on("data", (c: Buffer) => chunks.push(c));
    req.on("end", () => {
      const raw = Buffer.concat(chunks).toString("utf8");
      seen.push({
        method: req.method ?? "",
        path: req.url ?? "",
        session: String(req.headers["x-kei-session"] ?? ""),
        body: raw ? JSON.parse(raw) : null,
        authorization: String(req.headers.authorization ?? ""),
      });
      const answer = queue.shift()?.response ?? { status: 599 };
      if (answer.stall) return;
      let data = Buffer.alloc(0);
      const headers: Record<string, string> = {};
      if ("raw" in answer) {
        data = Buffer.from(answer.raw);
        headers["Content-Type"] = answer.content_type;
      } else if ("body" in answer) {
        data = Buffer.from(JSON.stringify(answer.body));
        headers["Content-Type"] = "application/json";
      }
      headers["Content-Length"] = String(data.length);
      res.writeHead(answer.status, headers).end(data);
    });
  });
  await new Promise<void>((r) => server.listen(socketPath, r));
  return { server, seen };
}

function closeServer(server: Server): Promise<void> {
  server.closeAllConnections();
  return new Promise((r) => server.close(() => r()));
}

async function runStep(
  c: KeiProxySocketAuthorizeClient,
  op: string,
  args: Json,
): Promise<unknown> {
  const sess = (s: Json) => ({
    source: s.source,
    externalId: s.external_id,
    sessionId: s.session_id,
  });
  switch (op) {
    case "open_session": {
      const s = await c.openSession({
        source: args.source,
        externalId: args.external_id,
      });
      return { session_id: s.sessionId };
    }
    case "close_session":
      await c.closeSession(sess(args.session));
      return null;
    case "forget_session":
      c.forgetSession({ source: args.source, externalId: args.external_id });
      return null;
    case "authorize": {
      const r = args.request;
      return c.authorize(sess(args.session), {
        tool: r.tool,
        action: r.action,
        resource: r.resource,
        spanId: r.span_id,
        parentSpan: r.parent_span,
        delegationDepth: r.delegation_depth,
        framework: r.framework,
        toolArgsDigest: r.tool_args_digest,
      });
    }
    case "list_models":
      return c.listModels();
    case "chat_completion":
      return c.chatCompletion(sess(args.session), args.payload);
    case "stream_chat_completion": {
      const lines: string[] = [];
      for await (const line of c.streamChatCompletion(
        sess(args.session),
        args.payload,
      ))
        lines.push(line);
      return lines;
    }
    case "policy_bundle_status":
      return c.policyBundleStatus();
    default:
      throw new Error(`unknown op ${op}`);
  }
}

describe("runtime socket client contract", () => {
  let dir: string;
  beforeEach(() => {
    // macOS caps socket paths near 104 bytes; os.tmpdir() can be long.
    dir = mkdtempSync("/tmp/kei-");
  });
  afterEach(() => rmSync(dir, { recursive: true, force: true }));

  it.each(named(CLIENT.cases))("%s", async (_name: string, kase: Json) => {
    const socketPath = join(dir, "runtime.sock");
    const daemon =
      kase.daemon === "absent"
        ? undefined
        : await replayDaemon(socketPath, kase.exchanges);
    const client = new KeiProxySocketAuthorizeClient({
      socketPath,
      env: kase.env ?? { KEI_RUNTIME_TOKEN: TOKEN },
      timeoutMs: kase.timeout_ms ?? CONSTANTS.socket.default_timeout_ms,
    });
    try {
      for (const step of kase.steps) {
        const outcome = await runStep(client, step.op, step.args).then(
          (result) => ({ result }),
          (error: unknown) => ({ error }),
        );
        if (step.expect.invalid_argument) {
          expect(outcome).toHaveProperty("error");
          expect((outcome as { error: unknown }).error).not.toBeInstanceOf(
            KeiProxySocketError,
          );
        } else if (step.expect.error) {
          const error = (outcome as { error?: unknown }).error;
          expect(error).toBeInstanceOf(KeiProxySocketError);
          const e = error as KeiProxySocketError;
          expect({
            step: step.op,
            reasonClass: e.reasonClass,
            code: e.code,
          }).toEqual({
            step: step.op,
            reasonClass: step.expect.error.reason_class,
            code: step.expect.error.code,
          });
          expect(e.message).not.toContain(TOKEN);
        } else {
          expect(outcome).toEqual({ result: step.expect.result });
        }
      }
    } finally {
      if (daemon) await closeServer(daemon.server);
    }
    const seen = daemon?.seen ?? [];
    expect(seen.map(({ authorization: _a, ...rest }) => rest)).toEqual(
      kase.exchanges.map((e: Json) => e.request),
    );
    for (const s of seen) expect(s.authorization).toBe(`Bearer ${TOKEN}`);
  });
});

describe("KeiProxyEvaluator over the socket", () => {
  let dir: string;
  beforeEach(() => {
    dir = mkdtempSync("/tmp/kei-");
  });
  afterEach(() => rmSync(dir, { recursive: true, force: true }));

  const caller = { user_id: "ada", invoking_subject: "ada", trusted: false };
  const permit = {
    schema: AUTHORIZE_RESPONSE_SCHEMA,
    decision: "permit",
    policy_id: "p-allow",
  };
  const opened = {
    status: 201,
    body: { schema: SESSION_SCHEMA, session_id: "sess-1" },
  };

  it("selects the socket client, allows a permit and denies a deny in one session", async () => {
    const socketPath = join(dir, "runtime.sock");
    const daemon = await replayDaemon(socketPath, [
      { response: opened },
      { response: { status: 200, body: permit } },
      {
        response: {
          status: 200,
          body: {
            schema: AUTHORIZE_RESPONSE_SCHEMA,
            decision: "deny",
            reason_code: "policy_deny",
            hint: "Add the matching policy.",
          },
        },
      },
    ]);
    try {
      const env = {
        [RUNTIME_SOCKET_PATH_ENV]: socketPath,
        KEI_RUNTIME_TOKEN: TOKEN,
      };
      const authorization = selectAuthorizationClient("openwebui", { env });
      expect(authorization).toBeInstanceOf(KeiProxySocketAuthorizationClient);
      const evaluator = new KeiProxyEvaluator(authorization, {
        logger: () => undefined,
      });
      const allow = await evaluator.evaluate(
        "github.get_issue",
        { owner: "o", repo: "r" },
        caller,
      );
      expect(allow.action).toBe(Action.ALLOW);
      expect(allow.rule).toBe("p-allow");
      const deny = await evaluator.evaluate("github.delete_repo", {}, caller);
      expect(deny.action).toBe(Action.DENY);
      expect(deny.reason_code).toBe("policy_deny");
      expect(deny.hint).toBe("Add the matching policy.");
    } finally {
      await closeServer(daemon.server);
    }
    expect(daemon.seen.map((s) => [s.method, s.path])).toEqual([
      ["PUT", "/v1/session"],
      ["POST", "/v1/authorize"],
      ["POST", "/v1/authorize"],
    ]);
    expect(daemon.seen[0].body).toEqual({
      schema: SESSION_SCHEMA,
      source: "openwebui",
      external_id: "ada",
    });
    for (const s of daemon.seen.slice(1)) {
      expect(s.body.user_id).toBeUndefined();
      expect(s.session).toBe("sess-1");
    }
  });

  it("denies with proxy_unavailable when the configured daemon is down", async () => {
    const env = {
      [RUNTIME_SOCKET_PATH_ENV]: join(dir, "missing.sock"),
      KEI_RUNTIME_TOKEN: TOKEN,
    };
    const evaluator = new KeiProxyEvaluator(
      selectAuthorizationClient("openwebui", { env }),
      {
        logger: () => undefined,
      },
    );
    const decision = await evaluator.evaluate("t", {}, caller);
    expect(decision.action).toBe(Action.DENY);
    expect(decision.reason.startsWith("kei-proxy proxy_unavailable")).toBe(
      true,
    );
    expect(decision.reason).toContain(join(dir, "missing.sock"));
    expect(decision.reason).toContain("--proxy-only");
    expect(decision.reason).not.toContain(TOKEN);
  });
});

describe("tool sync contract", () => {
  let server: Server;
  let answers: Json[];
  let seen: {
    method: string;
    path: string;
    headers: IncomingMessage["headers"];
    body: Json;
  }[];
  let url: string;

  beforeEach(async () => {
    answers = [];
    seen = [];
    server = createHttpServer((req: IncomingMessage, res: ServerResponse) => {
      const chunks: Buffer[] = [];
      req.on("data", (c: Buffer) => chunks.push(c));
      req.on("end", () => {
        seen.push({
          method: req.method ?? "",
          path: req.url ?? "",
          headers: req.headers,
          body: JSON.parse(Buffer.concat(chunks).toString("utf8")),
        });
        const answer = answers.shift();
        if (!answer || answer.network_error) {
          req.socket.destroy();
          return;
        }
        const data =
          "raw" in answer
            ? answer.raw
            : "body" in answer
              ? JSON.stringify(answer.body)
              : "";
        res
          .writeHead(answer.status, {
            "Content-Length": String(Buffer.byteLength(data)),
          })
          .end(data);
      });
    });
    await new Promise<void>((r) => server.listen(0, "127.0.0.1", r));
    url = `http://127.0.0.1:${(server.address() as AddressInfo).port}`;
  });
  afterEach(() => closeServer(server));

  it.each(named(SYNC.sync_cases))("%s", async (_name: string, kase: Json) => {
    answers = [...kase.responses];
    const sleeps: number[] = [];
    const config = new ToolManifestSyncConfig({
      controlPlaneUrl: url,
      token: SYNC.token,
      attempts: kase.attempts,
      backoffMs: kase.backoff_ms,
      env: {},
    });
    const manifest =
      "manifest_text" in kase ? kase.manifest_text : kase.manifest;
    const run = syncToolManifest(manifest, config, {
      expectTools: kase.expect_tools,
      sleep: async (ms) => {
        sleeps.push(ms);
      },
    });
    if ("error_contains" in kase.expect) {
      const error = await run.catch((e: unknown) => e);
      expect(error).toBeInstanceOf(ToolManifestSyncError);
      const message = (error as Error).message;
      expect(message).toContain(kase.expect.error_contains);
      expect(message).toContain(kase.expect.error_contains_also ?? "");
      expect(message).not.toContain(SYNC.token);
    } else {
      expect(await run).toEqual({
        sent: kase.expect.sent,
        toolCount: kase.expect.tool_count,
        counts: kase.expect.counts,
      });
    }
    expect(seen).toHaveLength(kase.expect_requests);
    expect(sleeps).toEqual(kase.expect_sleeps_ms);
    const want = SYNC.expected_request;
    for (const s of seen) {
      const body =
        "manifest_text" in kase
          ? JSON.parse(kase.manifest_text)
          : kase.manifest;
      expect([s.method, s.path]).toEqual([want.method, want.path]);
      for (const [k, v] of Object.entries(want.headers))
        expect(s.headers[k.toLowerCase()]).toBe(v);
      expect(s.body).toEqual(body);
    }
  });

  it.each(named(SYNC.config_cases))(
    "config: %s",
    (_name: string, kase: Json) => {
      if ("error_contains" in kase.expect) {
        expect(() => ToolManifestSyncConfig.fromEnv(kase.env)).toThrow(
          kase.expect.error_contains,
        );
        return;
      }
      const config = ToolManifestSyncConfig.fromEnv(kase.env);
      expect([config.toolsUrl, config.egressProxy, config.noProxy]).toEqual([
        kase.expect.tools_url,
        kase.expect.egress_proxy,
        kase.expect.no_proxy,
      ]);
      expect(JSON.stringify(config)).not.toContain(SYNC.token);
    },
  );

  it.each(named(SYNC.proxy_cases))("proxy: %s", (_name: string, kase: Json) => {
    const env = kase.env;
    const config = new ToolManifestSyncConfig({
      controlPlaneUrl: kase.url,
      token: "t",
      egressProxy: env.KEI_PROXY_EGRESS_PROXY ?? "",
      noProxy: env.KEI_PROXY_NO_PROXY ?? "",
      env,
    });
    expect(config.proxy()).toBe(kase.expect_proxy);
  });
});
