import { describe, it, expect } from "@jest/globals";
import { createServer, type Server, type IncomingMessage } from "node:http";
import { readFileSync, writeFileSync, unlinkSync, existsSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";
import {
  Client,
  MCPRouteError,
  RouteNotLocalError,
  RouteError,
  SessionEndedError,
  PolicyDeniedError,
  ManifestDriftError,
  UpstreamError,
  HeaderMismatchError,
  validateSocket,
  parseRouteError,
  routeErrorFromCode,
  RouteErrorSubclass,
  CODE_UNAUTHENTICATED,
  CODE_DENIED,
  CODE_MANIFEST,
  CODE_UPSTREAM,
  CODE_HEADER_MISMATCH,
  DEFAULT_SOCKET_PATH,
} from "../../src/kei/mcproute.js";

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);
const FIXTURE_PATH = resolve(__dirname, "../../../fixtures/kei/mcp-route-cases.v1.json");
const FIXTURES: Record<string, unknown> = JSON.parse(readFileSync(FIXTURE_PATH, "utf8"));
const DEFAULT_ROUTE_KEY = FIXTURES.default_route_key as string;
const CASES = FIXTURES.cases as FixtureCase[];

interface FixtureCase {
  id: string;
  description: string;
  request: {
    route_key: string | null;
    headers: Record<string, string>;
    jsonrpc: string;
    id: number;
    method: string;
    params: Record<string, unknown> | null;
  };
  response: {
    status: number;
    content_type: string;
    body?: Record<string, unknown>;
    body_events?: string[];
  };
  expected: {
    error: Record<string, unknown> | null;
    tools?: string[];
    content?: { type: string; text: string }[];
    isError?: boolean;
  };
}

function listen(server: Server): Promise<string> {
  return new Promise((resolve) =>
    server.listen(0, "127.0.0.1", () => {
      const addr = server.address() as import("node:net").AddressInfo;
      resolve(`127.0.0.1:${addr.port}`);
    }),
  );
}

function close(server: Server): Promise<void> {
  server.closeAllConnections();
  return new Promise((r) => server.close(() => r()));
}

function respond(res: import("http").ServerResponse, status: number, contentType: string, body: string) {
  res.writeHead(status, { "Content-Type": contentType });
  res.end(body);
}

function getCase(id: string): FixtureCase {
  const c = CASES.find((c) => c.id === id);
  if (!c) throw new Error(`fixture case ${id} not found`);
  return c;
}

// ---- Fixture-driven tests ----

describe("MCPRouteClient fixture cases", () => {
  it("tools_list_ok: returns a list of two tools", async () => {
    const tc = getCase("tools_list_ok");
    const server = createServer((_req, res) => {
      respond(res, tc.response.status, tc.response.content_type, JSON.stringify(tc.response.body));
    });
    const addr = await listen(server);
    const client = new Client("test-connector", {
      baseUrl: `http://${addr}`,
      env: { KEI_MCP_ROUTE_KEY: DEFAULT_ROUTE_KEY },
    });
    const tools = await client.listTools();
    expect(tools).toHaveLength(2);
    expect(tools[0].name).toBe("get_weather");
    expect(tools[1].name).toBe("send_email");
    await close(server);
  });

  it("tools_call_ok: returns a content block", async () => {
    const tc = getCase("tools_call_ok");
    const server = createServer((_req, res) => {
      respond(res, tc.response.status, tc.response.content_type, JSON.stringify(tc.response.body));
    });
    const addr = await listen(server);
    const client = new Client("test-connector", {
      baseUrl: `http://${addr}`,
      env: { KEI_MCP_ROUTE_KEY: DEFAULT_ROUTE_KEY },
    });
    const result = await client.callTool("get_weather", { location: "London" });
    expect(result.content).toHaveLength(1);
    expect(result.content[0].type).toBe("text");
    expect(result.content[0].text).toContain("15°C");
    expect(result.isError).toBe(false);
    await close(server);
  });

  it("tools_call_denied_31403: throws PolicyDeniedError", async () => {
    const tc = getCase("tools_call_denied_31403");
    const server = createServer((_req, res) => {
      respond(res, tc.response.status, tc.response.content_type, JSON.stringify(tc.response.body));
    });
    const addr = await listen(server);
    const client = new Client("test-connector", {
      baseUrl: `http://${addr}`,
      env: { KEI_MCP_ROUTE_KEY: DEFAULT_ROUTE_KEY },
    });
    let caught: unknown;
    try {
      await client.callTool("delete_repo", { owner: "acme", repo: "sales-pipeline" });
    } catch (err) {
      caught = err;
    }
    expect(caught).toBeDefined();
    expect(caught).toBeInstanceOf(PolicyDeniedError);
    expect((caught as PolicyDeniedError).code).toBe(CODE_DENIED);
    expect((caught as PolicyDeniedError).reason).toBe("policy_denied");
    expect((caught as PolicyDeniedError).decisionId).toBe("dec-00000000-0000-4000-8000-000000000001");
    await close(server);
  });

  it("tools_list_drift_31409: throws ManifestDriftError", async () => {
    const tc = getCase("tools_list_drift_31409");
    const server = createServer((_req, res) => {
      respond(res, tc.response.status, tc.response.content_type, JSON.stringify(tc.response.body));
    });
    const addr = await listen(server);
    const client = new Client("test-connector", {
      baseUrl: `http://${addr}`,
      env: { KEI_MCP_ROUTE_KEY: DEFAULT_ROUTE_KEY },
    });
    let caught: unknown;
    try {
      await client.listTools();
    } catch (err) {
      caught = err;
    }
    expect(caught).toBeDefined();
    expect(caught).toBeInstanceOf(ManifestDriftError);
    expect((caught as ManifestDriftError).code).toBe(CODE_MANIFEST);
    expect((caught as ManifestDriftError).reason).toBe("mcp_manifest_drift");
    expect((caught as ManifestDriftError).decisionId).toBe("dec-00000000-0000-4000-8000-000000000002");
    await close(server);
  });

  it("tools_call_upstream_31502: throws UpstreamError", async () => {
    const tc = getCase("tools_call_upstream_31502");
    const server = createServer((_req, res) => {
      respond(res, tc.response.status, tc.response.content_type, JSON.stringify(tc.response.body));
    });
    const addr = await listen(server);
    const client = new Client("test-connector", {
      baseUrl: `http://${addr}`,
      env: { KEI_MCP_ROUTE_KEY: DEFAULT_ROUTE_KEY },
    });
    let caught: unknown;
    try {
      await client.callTool("get_weather", { location: "Paris" });
    } catch (err) {
      caught = err;
    }
    expect(caught).toBeDefined();
    expect(caught).toBeInstanceOf(UpstreamError);
    expect((caught as UpstreamError).code).toBe(CODE_UPSTREAM);
    expect((caught as UpstreamError).reason).toBe("upstream_unreachable");
    expect((caught as UpstreamError).decisionId).toBe("dec-00000000-0000-4000-8000-000000000003");
    await close(server);
  });

  it("header_mismatch_32020: throws HeaderMismatchError", async () => {
    const tc = getCase("header_mismatch_32020");
    const server = createServer((_req, res) => {
      respond(res, tc.response.status, tc.response.content_type, JSON.stringify(tc.response.body));
    });
    const addr = await listen(server);
    const client = new Client("test-connector", {
      baseUrl: `http://${addr}`,
      env: { KEI_MCP_ROUTE_KEY: DEFAULT_ROUTE_KEY },
    });
    let caught: unknown;
    try {
      await client.callTool("get_weather", { location: "Tokyo" });
    } catch (err) {
      caught = err;
    }
    expect(caught).toBeDefined();
    expect(caught).toBeInstanceOf(HeaderMismatchError);
    expect((caught as HeaderMismatchError).code).toBe(CODE_HEADER_MISMATCH);
    expect((caught as HeaderMismatchError).reason).toBe("HeaderMismatch");
    expect((caught as HeaderMismatchError).decisionId).toBe("dec-00000000-0000-4000-8000-000000000004");
    await close(server);
  });

  it("invalid_route_key_31401: throws SessionEndedError on 403 with JSON-RPC error", async () => {
    const tc = getCase("invalid_route_key_31401");
    const server = createServer((_req, res) => {
      respond(res, tc.response.status, tc.response.content_type, JSON.stringify(tc.response.body));
    });
    const addr = await listen(server);
    const client = new Client("test-connector", {
      baseUrl: `http://${addr}`,
      env: { KEI_MCP_ROUTE_KEY: DEFAULT_ROUTE_KEY },
    });
    let caught: unknown;
    try {
      await client.listTools();
    } catch (err) {
      caught = err;
    }
    expect(caught).toBeDefined();
    expect(caught).toBeInstanceOf(SessionEndedError);
    expect((caught as SessionEndedError).code).toBe(CODE_UNAUTHENTICATED);
    expect((caught as SessionEndedError).reason).toBe("invalid_route_key");
    expect((caught as SessionEndedError).decisionId).toBe("dec-00000000-0000-4000-8000-000000000005");
    await close(server);
  });

  it("tools_call_sse_stream: reads an SSE stream", async () => {
    const tc = getCase("tools_call_sse_stream");
    const server = createServer((_req, res) => {
      respond(res, tc.response.status, tc.response.content_type, (tc.response.body_events ?? []).join(""));
    });
    const addr = await listen(server);
    const client = new Client("test-connector", {
      baseUrl: `http://${addr}`,
      env: { KEI_MCP_ROUTE_KEY: DEFAULT_ROUTE_KEY },
    });
    const result = await client.callTool("get_weather", { location: "Berlin" });
    expect(result.content).toHaveLength(1);
    expect(result.content[0].type).toBe("text");
    expect(result.content[0].text).toContain("18°C");
    expect(result.isError).toBe(false);
    await close(server);
  });
});

// ---- Typed error mapping ----

describe("routeErrorFromCode", () => {
  const subclassCases: [number, RouteErrorSubclass, string][] = [
    [CODE_UNAUTHENTICATED, SessionEndedError, "SessionEndedError"],
    [CODE_DENIED, PolicyDeniedError, "PolicyDeniedError"],
    [CODE_MANIFEST, ManifestDriftError, "ManifestDriftError"],
    [CODE_UPSTREAM, UpstreamError, "UpstreamError"],
    [CODE_HEADER_MISMATCH, HeaderMismatchError, "HeaderMismatchError"],
  ];

  it.each(subclassCases)("maps code %p to %s", (code, cls, _name) => {
    const err = routeErrorFromCode(code as number, "test_msg", "test_reason", "dec-test");
    expect(err).toBeInstanceOf(cls);
    expect(err.code).toBe(code as number);
  });

  it("maps unknown code 0 to RouteError", () => {
    const err = routeErrorFromCode(0, "test_msg", "test_reason", "dec-test");
    expect(err).toBeInstanceOf(RouteError);
    expect(err.code).toBe(0);
  });

  it("stores message, reason, and decisionId", () => {
    const err = routeErrorFromCode(-31403, "policy_denied", "policy_denied", "dec-0001");
    expect(err.message).toBe("policy_denied");
    expect(err.reason).toBe("policy_denied");
    expect(err.decisionId).toBe("dec-0001");
  });
});

// ---- routeErrorFromCode edge cases ----

describe("parseRouteError", () => {
  it("returns null for non-object input", () => {
    expect(parseRouteError("not json")).toBeNull();
    expect(parseRouteError(null)).toBeNull();
    expect(parseRouteError(undefined)).toBeNull();
    expect(parseRouteError('{"no_error": true}')).toBeNull();
  });

  it("returns null for empty buffer", () => {
    expect(parseRouteError(Buffer.alloc(0))).toBeNull();
  });

  it("extracts a RouteError from a valid JSON-RPC error object", () => {
    const raw = JSON.stringify({
      jsonrpc: "2.0",
      id: 1,
      error: { code: -31403, message: "policy_denied", data: { reason: "policy_denied", decision_id: "dec-1" } },
    });
    const err = parseRouteError(raw);
    expect(err).not.toBeNull();
    expect(err).toBeInstanceOf(PolicyDeniedError);
    expect(err!.code).toBe(-31403);
    expect(err!.reason).toBe("policy_denied");
    expect(err!.decisionId).toBe("dec-1");
  });

  it("returns null for a plain 401 body without JSON-RPC error structure", () => {
    const raw = Buffer.from("Unauthorized", "utf8");
    expect(parseRouteError(raw)).toBeNull();
  });
});

// ---- RouteNotLocalError ----

describe("RouteNotLocalError", () => {
  it("throws when baseUrl points to a non-loopback address", async () => {
    const client = new Client("test", {
      baseUrl: "http://10.0.0.1:8080",
      env: { KEI_MCP_ROUTE_KEY: DEFAULT_ROUTE_KEY },
    });
    await expect(client.listTools()).rejects.toThrow(RouteNotLocalError);
  });

  it("allows 127.0.0.1", async () => {
    const server = createServer((_req, res) => {
      respond(res, 200, "application/json", JSON.stringify({ jsonrpc: "2.0", id: 1, result: { tools: [] } }));
    });
    const addr = await listen(server);
    const client = new Client("test", {
      baseUrl: `http://${addr}`,
      env: { KEI_MCP_ROUTE_KEY: DEFAULT_ROUTE_KEY },
    });
    const tools = await client.listTools();
    expect(tools).toEqual([]);
    await close(server);
  });
});

// ---- Missing route key ----

describe("missing route key", () => {
  it("throws a descriptive error when KEI_MCP_ROUTE_KEY is not set", async () => {
    const server = createServer((_req, res) => res.end());
    const addr = await listen(server);
    const client = new Client("test", {
      baseUrl: `http://${addr}`,
      env: {},
    });
    await expect(client.listTools()).rejects.toThrow(/KEI_MCP_ROUTE_KEY is not set/);
    await close(server);
  });
});

// ---- Plain 401 with no body ----

describe("plain 401", () => {
  it("throws SessionEndedError when server returns 401 with no body", async () => {
    let requestCount = 0;
    const server = createServer((_req, res) => {
      requestCount++;
      res.writeHead(401, { "Content-Type": "text/plain" });
      res.end();
    });
    const addr = await listen(server);
    const client = new Client("test", {
      baseUrl: `http://${addr}`,
      env: { KEI_MCP_ROUTE_KEY: DEFAULT_ROUTE_KEY },
    });
    let caught: unknown;
    try {
      await client.listTools();
    } catch (err) {
      caught = err;
    }
    expect(caught).toBeDefined();
    expect(caught).toBeInstanceOf(SessionEndedError);
    expect((caught as SessionEndedError).code).toBe(CODE_UNAUTHENTICATED);
    expect((caught as SessionEndedError).message).toBe("session ended — the route key expired or was revoked");
    expect(requestCount).toBe(1);
    await close(server);
  });
});

// ---- Request headers verification ----

describe("request headers", () => {
  it("sends Authorization, MCP-Protocol-Version, Mcp-Method, and Mcp-Name headers", async () => {
    let received: IncomingMessage | null = null;
    let body = "";
    const server = createServer((req, res) => {
      received = req;
      req.on("data", (chunk: string) => { body += chunk; });
      req.on("end", () => {
        respond(res, 200, "application/json", JSON.stringify({ jsonrpc: "2.0", id: 1, result: { tools: [] } }));
      });
    });
    const addr = await listen(server);
    const client = new Client("test-connector", {
      baseUrl: `http://${addr}`,
      env: { KEI_MCP_ROUTE_KEY: DEFAULT_ROUTE_KEY },
    });
    await client.listTools();
    expect(received).not.toBeNull();
    expect(received!.headers.authorization).toBe(`Bearer ${DEFAULT_ROUTE_KEY}`);
    expect(received!.headers["mcp-protocol-version"]).toBe("2026-07-28");
    expect(received!.headers["mcp-method"]).toBe("tools/list");
    expect(received!.headers["mcp-name"]).toBeUndefined();
    await close(server);
  });

  it("sends Mcp-Name for tools/call", async () => {
    let received: IncomingMessage | null = null;
    const server = createServer((req, res) => {
      received = req;
      req.resume();
      req.on("end", () => {
        respond(res, 200, "application/json", JSON.stringify({
          jsonrpc: "2.0", id: 2, result: { content: [{ type: "text", text: "ok" }], isError: false },
        }));
      });
    });
    const addr = await listen(server);
    const client = new Client("test-connector", {
      baseUrl: `http://${addr}`,
      env: { KEI_MCP_ROUTE_KEY: DEFAULT_ROUTE_KEY },
    });
    await client.callTool("get_weather", { location: "Paris" });
    expect(received).not.toBeNull();
    expect(received!.headers["mcp-method"]).toBe("tools/call");
    expect(received!.headers["mcp-name"]).toBe("get_weather");
    await close(server);
  });
});

// ---- Route key leak prevention ----

describe("route key leak prevention", () => {
  it("does not leak route key in error messages", async () => {
    const server = createServer((_req, res) => {
      res.writeHead(500, { "Content-Type": "text/plain" });
      res.end("server error");
    });
    const addr = await listen(server);
    const client = new Client("test", {
      baseUrl: `http://${addr}`,
      env: { KEI_MCP_ROUTE_KEY: DEFAULT_ROUTE_KEY },
    });
    let caught: unknown;
    try {
      await client.listTools();
    } catch (err) {
      caught = err;
    }
    expect(caught).toBeDefined();
    const msg = String(caught);
    expect(msg).not.toContain(DEFAULT_ROUTE_KEY);
    expect(msg).not.toContain("kmr_");
    await close(server);
  });
});

// ---- Socket validation ----

describe("validateSocket", () => {
  it("returns false for a non-existent path", () => {
    expect(validateSocket("/nonexistent/path.sock")).toBe(false);
  });

  it("returns false for a regular file", () => {
    const tmp = resolve(__dirname, "__test_validate_socket_tmp__");
    try {
      writeFileSync(tmp, "not a socket");
      expect(validateSocket(tmp)).toBe(false);
    } finally {
      if (existsSync(tmp)) unlinkSync(tmp);
    }
  });

  it("returns false for an empty string path", () => {
    expect(validateSocket("")).toBe(false);
  });
});

// ---- Default constants ----

describe("constants", () => {
  it("DEFAULT_SOCKET_PATH is /run/kei-proxy/runtime.sock", () => {
    expect(DEFAULT_SOCKET_PATH).toBe("/run/kei-proxy/runtime.sock");
  });
});
