import { request as httpRequest, type IncomingMessage, type OutgoingHttpHeaders } from "node:http";
import { request as httpsRequest } from "node:https";
import { lstatSync } from "node:fs";

export const DEFAULT_SOCKET_PATH = "/run/kei-proxy/runtime.sock";
export const SOCKET_PATH_ENV = "KEI_RUNTIME_SOCKET_PATH";
export const ROUTE_KEY_ENV = "KEI_MCP_ROUTE_KEY";
export const DEFAULT_LOOPBACK_PORT = "8080";
export const MCP_ROUTE_PREFIX = "/mcp/";
export const DEFAULT_TIMEOUT_MS = 10_000;
export const DEFAULT_SSE_TIMEOUT_MS = 120_000;
export const MAX_RESPONSE_BYTES = 8_388_608;

export const CODE_UNAUTHENTICATED = -31401;
export const CODE_DENIED = -31403;
export const CODE_MANIFEST = -31409;
export const CODE_UPSTREAM = -31502;
export const CODE_HEADER_MISMATCH = -32020;

const S_IFMT = 0o170000;
const S_IFSOCK = 0o140000;

export class MCPRouteError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "MCPRouteError";
  }
}

export class RouteNotLocalError extends MCPRouteError {
  constructor(message?: string) {
    super(message ?? "mcp route: the route host is not a loopback address");
    this.name = "RouteNotLocalError";
  }
}

export class RouteError extends MCPRouteError {
  readonly code: number;
  readonly reason: string;
  readonly decisionId: string;

  constructor(code: number, message: string, reason = "", decisionId = "") {
    const parts = [`mcp route error ${code} (${message})`];
    if (reason) {
      parts.push(`: ${reason}`);
      if (decisionId) parts.push(` [${decisionId}]`);
    }
    super(parts.join(""));
    this.name = "RouteError";
    this.code = code;
    this.message = message;
    this.reason = reason;
    this.decisionId = decisionId;
  }
}

export class SessionEndedError extends RouteError {
  constructor(message = "session ended — the route key expired or was revoked", reason = "", decisionId = "") {
    super(CODE_UNAUTHENTICATED, message, reason, decisionId);
    this.name = "SessionEndedError";
  }
}

export class PolicyDeniedError extends RouteError {
  constructor(message = "policy_denied", reason = "", decisionId = "") {
    super(CODE_DENIED, message, reason, decisionId);
    this.name = "PolicyDeniedError";
  }
}

export class ManifestDriftError extends RouteError {
  constructor(message = "mcp_manifest_drift", reason = "", decisionId = "") {
    super(CODE_MANIFEST, message, reason, decisionId);
    this.name = "ManifestDriftError";
  }
}

export class UpstreamError extends RouteError {
  constructor(message = "upstream_unreachable", reason = "", decisionId = "") {
    super(CODE_UPSTREAM, message, reason, decisionId);
    this.name = "UpstreamError";
  }
}

export class HeaderMismatchError extends RouteError {
  constructor(message = "HeaderMismatch", reason = "", decisionId = "") {
    super(CODE_HEADER_MISMATCH, message, reason, decisionId);
    this.name = "HeaderMismatchError";
  }
}

export type RouteErrorSubclass = new (message: string, reason?: string, decisionId?: string) => RouteError;

const CODE_TO_ERROR_CLASS: Record<number, RouteErrorSubclass> = {
  [CODE_UNAUTHENTICATED]: SessionEndedError,
  [CODE_DENIED]: PolicyDeniedError,
  [CODE_MANIFEST]: ManifestDriftError,
  [CODE_UPSTREAM]: UpstreamError,
  [CODE_HEADER_MISMATCH]: HeaderMismatchError,
};

export function routeErrorFromCode(code: number, message: string, reason = "", decisionId = ""): RouteError {
  const cls = CODE_TO_ERROR_CLASS[code];
  if (cls) {
    return new cls(message, reason, decisionId);
  }
  return new RouteError(code, message, reason, decisionId);
}

export interface ToolDescriptor {
  name: string;
  description?: string;
  inputSchema?: Record<string, unknown>;
}

export interface ContentBlock {
  type: string;
  text: string;
}

export interface ToolResult {
  content: ContentBlock[];
  isError: boolean;
}

export interface ClientOptions {
  baseUrl?: string;
  socketPath?: string;
  timeoutMs?: number;
  sseTimeoutMs?: number;
  env?: Record<string, string | undefined>;
}

export function validateSocket(path: string): boolean {
  try {
    const stats = lstatSync(path);
    if ((stats.mode & S_IFMT) !== S_IFSOCK) return false;
    const uid = process.getuid?.();
    if (uid !== undefined && stats.uid === uid) return true;
    if (stats.mode & 0o022) return false;
    return true;
  } catch {
    return false;
  }
}

function isLoopback(host: string): boolean {
  if (host === "127.0.0.1" || host === "::1" || host === "localhost") return true;
  if (host.startsWith("[") && host.endsWith("]")) {
    const inner = host.slice(1, -1);
    if (inner === "::1" || inner === "0:0:0:0:0:0:0:1") return true;
  }
  return false;
}

function headerValue(headers: Record<string, string | string[] | undefined>, name: string): string {
  const val = headers[name.toLowerCase()];
  if (Array.isArray(val)) return val[0] ?? "";
  return val ?? "";
}

interface Answer {
  status: number;
  headers: Record<string, string>;
  body: string;
  rawBody: Buffer;
}

function sendRequest(
  options: {
    hostname?: string;
    port?: string | number;
    socketPath?: string;
    path: string;
    method: string;
    headers: OutgoingHttpHeaders;
    body: string;
    timeoutMs: number;
  },
): Promise<Answer> {
  const { hostname, port, socketPath, path, method, headers, body, timeoutMs } = options;
  const reqOpts: Record<string, unknown> = {
    method,
    headers,
    timeout: timeoutMs,
  };
  if (socketPath) {
    reqOpts.socketPath = socketPath;
    reqOpts.path = path;
  } else {
    reqOpts.hostname = hostname;
    reqOpts.port = port;
    reqOpts.path = path;
  }

  const isTls = (socketPath == null && port === "443") || (hostname != null && port === "443");

  return new Promise((resolve, reject) => {
    const req = (isTls ? httpsRequest : httpRequest)(reqOpts, (res: IncomingMessage) => {
      const chunks: Buffer[] = [];
      let size = 0;
      res.on("data", (chunk: Buffer) => {
        size += chunk.length;
        if (size <= MAX_RESPONSE_BYTES) chunks.push(chunk);
      });
      res.on("end", () => {
        const rawBody = Buffer.concat(chunks);
        const hdrs: Record<string, string> = {};
        for (const [k, v] of Object.entries(res.headers)) {
          if (v !== undefined) hdrs[k] = Array.isArray(v) ? v.join(", ") : v;
        }
        resolve({
          status: res.statusCode ?? 0,
          headers: hdrs,
          body: rawBody.toString("utf8"),
          rawBody,
        });
      });
      res.on("error", reject);
    });
    req.on("error", (err: Error) => reject(new MCPRouteError(`mcp route: transport: ${err.message}`)));
    req.on("timeout", () => {
      req.destroy();
      reject(new MCPRouteError("mcp route: transport: timeout"));
    });
    req.end(body);
  });
}

export function parseRouteError(raw: unknown): RouteError | null {
  try {
    const body = typeof raw === "string" ? JSON.parse(raw) : raw instanceof Buffer ? JSON.parse(raw.toString("utf8")) : raw;
    if (typeof body !== "object" || body === null) return null;
    const err = (body as Record<string, unknown>).error;
    if (typeof err !== "object" || err === null) return null;
    const e = err as Record<string, unknown>;
    const code = typeof e.code === "number" ? e.code : 0;
    const message = typeof e.message === "string" ? e.message : "";
    const data = typeof e.data === "object" && e.data !== null ? (e.data as Record<string, string>) : {};
    const reason = typeof data.reason === "string" ? data.reason : "";
    const decisionId = typeof data.decision_id === "string" ? data.decision_id : "";
    return routeErrorFromCode(code, message, reason, decisionId);
  } catch {
    return null;
  }
}

export class Client {
  readonly slug: string;
  readonly #baseUrl: string;
  readonly #socketPath: string;
  readonly #timeoutMs: number;
  readonly #sseTimeoutMs: number;
  readonly #env: Record<string, string | undefined>;
  #baseUrlLoopbackValidated = false;
  #baseUrlIsValid = false;

  constructor(slug: string, options: ClientOptions = {}) {
    if (!slug) throw new MCPRouteError("slug is required");
    this.slug = slug;
    this.#baseUrl = options.baseUrl ?? "";
    this.#socketPath = options.socketPath ?? "";
    this.#timeoutMs = options.timeoutMs ?? DEFAULT_TIMEOUT_MS;
    this.#sseTimeoutMs = options.sseTimeoutMs ?? DEFAULT_SSE_TIMEOUT_MS;
    this.#env = options.env ?? process.env;
  }

  #lookup(key: string): string {
    return this.#env[key] ?? "";
  }

  get resolvedSocketPath(): string {
    return this.#socketPath || this.#lookup(SOCKET_PATH_ENV) || DEFAULT_SOCKET_PATH;
  }

  #buildUrlPath(): string {
    return `${MCP_ROUTE_PREFIX}${encodeURIComponent(this.slug)}`;
  }

  #assertBaseUrlLoopback(): void {
    if (!this.#baseUrlLoopbackValidated) {
      this.#baseUrlLoopbackValidated = true;
      try {
        const parsed = new URL(this.#baseUrl);
        const host = parsed.hostname;
        if (host && !isLoopback(host)) {
          this.#baseUrlIsValid = false;
          return;
        }
        this.#baseUrlIsValid = true;
      } catch {
        this.#baseUrlIsValid = false;
      }
    }
  }

  async #send(urlPath: string, body: string, headers: OutgoingHttpHeaders): Promise<Answer> {
    if (this.#baseUrl) {
      this.#assertBaseUrlLoopback();
      if (!this.#baseUrlIsValid) throw new RouteNotLocalError();
      const parsed = new URL(this.#baseUrl);
      const host = parsed.hostname;
      const port = parsed.port || "80";
      return sendRequest({ hostname: host, port, path: urlPath, method: "POST", headers, body, timeoutMs: this.#timeoutMs });
    }

    let lastError: Error | undefined;

    const socketPath = this.resolvedSocketPath;
    if (socketPath && validateSocket(socketPath)) {
      try {
        return await sendRequest({ socketPath, path: urlPath, method: "POST", headers, body, timeoutMs: this.#timeoutMs });
      } catch (err) {
        lastError = err instanceof Error ? err : new Error(String(err));
      }
    }

    for (const host of ["127.0.0.1", "::1"]) {
      try {
        return await sendRequest({ hostname: host, port: DEFAULT_LOOPBACK_PORT, path: urlPath, method: "POST", headers, body, timeoutMs: this.#timeoutMs });
      } catch (err) {
        lastError = err instanceof Error ? err : new Error(String(err));
      }
    }

    if (lastError) throw lastError;
    throw new MCPRouteError("mcp route: no transport available");
  }

  async call(method: string, toolName = "", params?: Record<string, unknown>): Promise<unknown> {
    const routeKey = this.#lookup(ROUTE_KEY_ENV);
    if (!routeKey) {
      throw new MCPRouteError(`mcp route: ${ROUTE_KEY_ENV} is not set`);
    }

    const id = Date.now();
    const requestBody = JSON.stringify({
      jsonrpc: "2.0",
      id,
      method,
      params: params ?? null,
    });

    const urlPath = this.#buildUrlPath();
    const headers: OutgoingHttpHeaders = {
      Authorization: `Bearer ${routeKey}`,
      "Content-Type": "application/json",
      "MCP-Protocol-Version": "2026-07-28",
      "Mcp-Method": method,
    };
    if (toolName) {
      headers["Mcp-Name"] = toolName;
    }

    const response = await this.#send(urlPath, requestBody, headers);

    if (response.status === 401) {
      const re = parseRouteError(response.rawBody);
      if (re) throw re;
      throw new SessionEndedError();
    }

    const contentType = headerValue(response.headers, "content-type");

    if (contentType.includes("text/event-stream")) {
      return this.#readSSE(response.body, id);
    }

    if (response.status < 200 || response.status > 299) {
      const re = parseRouteError(response.rawBody);
      if (re) throw re;
      throw new MCPRouteError(`mcp route: HTTP ${response.status}`);
    }

    let rpc: Record<string, unknown>;
    try {
      rpc = JSON.parse(response.body);
    } catch {
      throw new MCPRouteError("mcp route: decode response: invalid JSON");
    }

    if (typeof rpc !== "object" || rpc === null || Array.isArray(rpc)) {
      throw new MCPRouteError("mcp route: decode response: not a JSON object");
    }

    if (rpc.error) {
      const err = rpc.error as Record<string, unknown>;
      if (typeof err === "object" && err !== null) {
        const code = typeof err.code === "number" ? err.code : 0;
        const msg = typeof err.message === "string" ? err.message : "";
        const data = typeof err.data === "object" && err.data !== null ? (err.data as Record<string, string>) : {};
        const reason = typeof data.reason === "string" ? data.reason : "";
        const decisionId = typeof data.decision_id === "string" ? data.decision_id : "";
        throw routeErrorFromCode(code, msg, reason, decisionId);
      }
      throw new RouteError(0, String(err));
    }

    return rpc.result;
  }

  #readSSE(body: string, _expectedId: number): unknown {
    const dataBuf: string[] = [];
    for (const line of body.split("\n")) {
      const trimmed = line.trim();
      if (trimmed.startsWith("data:")) {
        dataBuf.push(trimmed.slice(5).trim());
      } else if (trimmed === "" && dataBuf.length > 0) {
        const eventData = dataBuf.join("");
        dataBuf.length = 0;
        try {
          const msg = JSON.parse(eventData);
          if (typeof msg === "object" && msg !== null && msg.id !== undefined) {
            return msg.result;
          }
        } catch {
          // skip malformed events
        }
      }
    }
    throw new MCPRouteError("mcp route: sse stream ended without a response");
  }

  async listTools(): Promise<ToolDescriptor[]> {
    const result = await this.call("tools/list");
    if (typeof result !== "object" || result === null) {
      throw new MCPRouteError("mcp route: decode tools/list result: expected an object");
    }
    const toolsRaw = (result as Record<string, unknown>).tools;
    if (!Array.isArray(toolsRaw)) {
      throw new MCPRouteError("mcp route: decode tools/list result: expected a tools array");
    }
    return toolsRaw.map((t: Record<string, unknown>) => ({
      name: String(t.name ?? ""),
      description: t.description !== undefined ? String(t.description) : undefined,
      inputSchema: t.inputSchema !== undefined ? (t.inputSchema as Record<string, unknown>) : undefined,
    }));
  }

  async callTool(name: string, args?: Record<string, unknown>): Promise<ToolResult> {
    const params: Record<string, unknown> = { name };
    if (args) params.arguments = args;
    const result = await this.call("tools/call", name, params);
    if (typeof result !== "object" || result === null) {
      throw new MCPRouteError("mcp route: decode tools/call result: expected an object");
    }
    const r = result as Record<string, unknown>;
    const rawContent = r.content;
    let content: ContentBlock[] = [];
    if (Array.isArray(rawContent)) {
      content = rawContent.map((c: Record<string, unknown>) => ({
        type: String(c.type ?? ""),
        text: String(c.text ?? ""),
      }));
    }
    return { content, isError: r.isError === true };
  }
}
