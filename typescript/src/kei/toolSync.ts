/**
 * Sync a harness's governed tool manifest to the Kei control plane.
 *
 * TypeScript counterpart of `pedro_agentware.kei.tool_sync` and Go's
 * `kei/toolsync`, held to `testing/contracts/runtime-socket/tool-sync-cases.json`.
 *
 * Kei's policy bundle only allows tools registered in the installation's tool
 * catalog, so a harness registers its tools before it serves:
 * `PUT $KEI_RUNTIME_CONTROL_PLANE_URL/api/v1/runtime/tools` with the
 * `application/vnd.kei.tool-manifest.v3+json` document, authenticated with
 * `KEI_RUNTIME_TOKEN`.
 *
 * - The PUT *replaces* the installation's tools, so a manifest with no tools
 *   is never sent: it would unregister every tool. When tools are expected
 *   that is an error; when they are not, the sync is skipped.
 * - A network failure or non-2xx answer is retried with exponential backoff;
 *   once the attempts run out `ToolManifestSyncError` is thrown, and the
 *   harness should refuse to serve.
 * - The control plane is reached the way kei-proxy reaches it: through
 *   `KEI_PROXY_EGRESS_PROXY` unless `KEI_PROXY_NO_PROXY` bypasses the host.
 *   Without an egress proxy, the ambient `HTTPS_PROXY` / `HTTP_PROXY` and
 *   `NO_PROXY` apply. Proxies are reached with an HTTP `CONNECT` tunnel.
 * - The token goes in the `Authorization` header and nowhere else: not in a
 *   log line, an error message, `JSON.stringify(config)` or `util.inspect`.
 */
import {
  request as httpRequest,
  type IncomingMessage,
  type OutgoingHttpHeaders,
} from "node:http";
import { request as httpsRequest } from "node:https";
import { connect as tlsConnect } from "node:tls";
import type { Socket } from "node:net";

export const TOOLS_PATH = "/api/v1/runtime/tools";
export const TOOL_MANIFEST_V3_MEDIA_TYPE =
  "application/vnd.kei.tool-manifest.v3+json";
export const RUNTIME_CONTROL_PLANE_URL_ENV = "KEI_RUNTIME_CONTROL_PLANE_URL";
export const EGRESS_PROXY_ENV = "KEI_PROXY_EGRESS_PROXY";
export const NO_PROXY_ENV = "KEI_PROXY_NO_PROXY";
export const DEFAULT_TOOL_SYNC_ATTEMPTS = 5;
export const DEFAULT_TOOL_SYNC_BACKOFF_MS = 1_000;
export const MAX_TOOL_SYNC_BACKOFF_MS = 16_000;
export const DEFAULT_TOOL_SYNC_TIMEOUT_MS = 30_000;

const RUNTIME_TOKEN_ENV = "KEI_RUNTIME_TOKEN";
const ERROR_BODY_LIMIT = 300;
const MAX_RESPONSE_BYTES = 1024 * 1024;
const INSPECT = Symbol.for("nodejs.util.inspect.custom");

type Env = Readonly<Record<string, string | undefined>>;
type JsonObject = Record<string, unknown>;

/** The manifest was not registered with Kei. The harness must not serve. */
export class ToolManifestSyncError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "ToolManifestSyncError";
  }
}

export interface ToolManifestSyncConfigOptions {
  controlPlaneUrl: string;
  token: string;
  /** `KEI_PROXY_EGRESS_PROXY`: when set, it alone decides the route. */
  egressProxy?: string;
  /** `KEI_PROXY_NO_PROXY`: hosts that bypass the egress proxy. */
  noProxy?: string;
  attempts?: number;
  backoffMs?: number;
  timeoutMs?: number;
  /** Where the ambient proxy variables are read when no egress proxy is set. Default `process.env`. */
  env?: Env;
}

/** Where and how to register the manifest. The token never leaves through inspection. */
export class ToolManifestSyncConfig {
  readonly controlPlaneUrl: string;
  readonly egressProxy: string;
  readonly noProxy: string;
  readonly attempts: number;
  readonly backoffMs: number;
  readonly timeoutMs: number;
  readonly #token: string;
  readonly #env: Env;

  constructor(options: ToolManifestSyncConfigOptions) {
    this.controlPlaneUrl = options.controlPlaneUrl;
    this.#token = options.token;
    this.egressProxy = options.egressProxy ?? "";
    this.noProxy = options.noProxy ?? "";
    this.attempts = options.attempts ?? DEFAULT_TOOL_SYNC_ATTEMPTS;
    this.backoffMs = options.backoffMs ?? DEFAULT_TOOL_SYNC_BACKOFF_MS;
    this.timeoutMs = options.timeoutMs ?? DEFAULT_TOOL_SYNC_TIMEOUT_MS;
    this.#env = options.env ?? process.env;
  }

  /** Read the variables kei-proxy reads. Throws `ToolManifestSyncError`. */
  static fromEnv(env: Env = process.env): ToolManifestSyncConfig {
    const url = (env[RUNTIME_CONTROL_PLANE_URL_ENV] ?? "").trim();
    const token = (env[RUNTIME_TOKEN_ENV] ?? "").trim();
    if (!url)
      throw new ToolManifestSyncError(
        `${RUNTIME_CONTROL_PLANE_URL_ENV} is not set`,
      );
    if (!token)
      throw new ToolManifestSyncError(`${RUNTIME_TOKEN_ENV} is not set`);
    let parsed: URL | undefined;
    try {
      parsed = new URL(url);
    } catch {
      parsed = undefined;
    }
    if (
      !parsed ||
      (parsed.protocol !== "https:" && parsed.protocol !== "http:") ||
      !parsed.host
    ) {
      throw new ToolManifestSyncError(
        `${RUNTIME_CONTROL_PLANE_URL_ENV} is not an http(s) URL`,
      );
    }
    return new ToolManifestSyncConfig({
      controlPlaneUrl: url,
      token,
      egressProxy: (env[EGRESS_PROXY_ENV] ?? "").trim(),
      noProxy: (env[NO_PROXY_ENV] ?? "").trim(),
      env,
    });
  }

  get toolsUrl(): string {
    return this.controlPlaneUrl.replace(/\/+$/, "") + TOOLS_PATH;
  }

  /** The proxy URL the sync uses for the control plane, or `null` to go direct. */
  proxy(): string | null {
    const url = new URL(this.controlPlaneUrl);
    const host = url.hostname.replace(/^\[|\]$/g, "");
    if (this.egressProxy) {
      return bypassesProxy(host, this.noProxy) ? null : this.egressProxy;
    }
    const env = this.#env;
    const ambient =
      url.protocol === "https:"
        ? env.HTTPS_PROXY || env.https_proxy
        : env.HTTP_PROXY || env.http_proxy;
    if (!ambient) return null;
    return bypassesProxy(host, env.NO_PROXY || env.no_proxy || "")
      ? null
      : ambient;
  }

  /** @internal The bearer token, for the sync request only. */
  token(): string {
    return this.#token;
  }

  toJSON(): JsonObject {
    return {
      controlPlaneUrl: this.controlPlaneUrl,
      egressProxy: this.egressProxy,
      noProxy: this.noProxy,
      attempts: this.attempts,
      backoffMs: this.backoffMs,
      timeoutMs: this.timeoutMs,
    };
  }

  [INSPECT](): string {
    return `ToolManifestSyncConfig ${JSON.stringify(this.toJSON())}`;
  }
}

/** What a sync did: whether it sent, how many tools, and the counts Kei answered. */
export interface ToolManifestSyncResult {
  sent: boolean;
  toolCount: number;
  counts: JsonObject;
}

export type ToolSyncLogger = (level: "info" | "warn", message: string) => void;

export interface SyncToolManifestOptions {
  /**
   * Whether this harness is configured with tools. An empty manifest is never
   * sent: it throws when tools are expected and resolves `sent: false` when
   * they are not. Default `true`.
   */
  expectTools?: boolean;
  /** Replaces the wait between attempts (tests). */
  sleep?: (ms: number) => Promise<void>;
  /** Receives one line per success and per retry. Never sees the token. Default: silent. */
  logger?: ToolSyncLogger;
}

/** The number of entries in the manifest's `tools` list (0 when absent). */
export function manifestToolCount(manifest: string | JsonObject): number {
  const tools = parseManifest(manifest).tools;
  return Array.isArray(tools) ? tools.length : 0;
}

/** Register `manifest` (a tool manifest v3, as JSON text or an object) with Kei, replacing its tools. */
export async function syncToolManifest(
  manifest: string | JsonObject,
  config: ToolManifestSyncConfig,
  options: SyncToolManifestOptions = {},
): Promise<ToolManifestSyncResult> {
  const { expectTools = true, logger = () => undefined } = options;
  const sleep =
    options.sleep ??
    ((ms: number) => new Promise<void>((r) => setTimeout(r, ms)));
  const document = parseManifest(manifest);
  const count = Array.isArray(document.tools) ? document.tools.length : 0;
  if (count === 0) {
    if (expectTools) {
      throw new ToolManifestSyncError(
        `refusing to PUT an empty manifest to ${TOOLS_PATH}: it would unregister every tool`,
      );
    }
    logger(
      "info",
      `tool sync: no tools configured; nothing sent to ${TOOLS_PATH}`,
    );
    return { sent: false, toolCount: 0, counts: {} };
  }

  const token = config.token();
  const body = JSON.stringify(document);
  const headers: OutgoingHttpHeaders = {
    Authorization: `Bearer ${token}`,
    "Content-Type": TOOL_MANIFEST_V3_MEDIA_TYPE,
    Accept: TOOL_MANIFEST_V3_MEDIA_TYPE,
    "Content-Length": Buffer.byteLength(body),
  };
  const attempts = Math.max(1, config.attempts);
  let last = "no attempt made";
  for (let attempt = 1; attempt <= attempts; attempt++) {
    try {
      const response = await put(config, headers, body);
      if (response.status >= 200 && response.status < 300) {
        const counts = parseCounts(response.body);
        const summary =
          Object.keys(counts)
            .sort()
            .map((k) => `${k}=${String(counts[k])}`)
            .join(", ") || "no counts returned";
        logger(
          "info",
          `tool sync: registered ${count} tool(s) with Kei at ${TOOLS_PATH} (${summary})`,
        );
        return { sent: true, toolCount: count, counts };
      }
      last = `HTTP ${response.status}: ${response.body.slice(0, ERROR_BODY_LIMIT)}`;
    } catch (error) {
      last = `network error: ${errorKind(error)}`;
    }
    last = token ? last.split(token).join("[redacted]") : last;
    if (attempt < attempts) {
      const delay = Math.min(
        config.backoffMs * 2 ** (attempt - 1),
        MAX_TOOL_SYNC_BACKOFF_MS,
      );
      logger(
        "warn",
        `tool sync attempt ${attempt}/${attempts} failed (${last}); retrying in ${delay / 1000}s`,
      );
      await sleep(delay);
    }
  }
  throw new ToolManifestSyncError(
    `PUT ${TOOLS_PATH} failed after ${attempts} attempt(s): ${last}`,
  );
}

/**
 * Whether `host` matches a `NO_PROXY`-style list: comma-separated entries,
 * each matching the host exactly or as a domain suffix (a leading `.` is
 * optional), case-insensitively; `*` matches every host.
 */
export function bypassesProxy(host: string, noProxy: string): boolean {
  const name = host.toLowerCase();
  for (const raw of noProxy.split(",")) {
    let entry = raw.trim().toLowerCase();
    if (!entry) continue;
    if (entry === "*") return true;
    entry = entry.replace(/^\.+/, "").replace(/:\d+$/, "");
    if (name === entry || name.endsWith(`.${entry}`)) return true;
  }
  return false;
}

function parseManifest(manifest: string | JsonObject): JsonObject {
  let parsed: unknown = manifest;
  if (typeof manifest === "string") {
    try {
      parsed = JSON.parse(manifest);
    } catch {
      throw new ToolManifestSyncError("the tool manifest is not JSON");
    }
  }
  if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) {
    throw new ToolManifestSyncError("the tool manifest is not a JSON object");
  }
  return { ...(parsed as JsonObject) };
}

function parseCounts(text: string): JsonObject {
  try {
    const parsed: unknown = JSON.parse(text);
    return typeof parsed === "object" &&
      parsed !== null &&
      !Array.isArray(parsed)
      ? (parsed as JsonObject)
      : {};
  } catch {
    return {};
  }
}

function errorKind(error: unknown): string {
  const code = (error as { code?: unknown })?.code;
  if (typeof code === "string") return code;
  return error instanceof Error ? error.name : "unknown";
}

interface Answer {
  status: number;
  body: string;
}

/** One PUT, direct or through a CONNECT tunnel. No redirects are followed. */
async function put(
  config: ToolManifestSyncConfig,
  headers: OutgoingHttpHeaders,
  body: string,
): Promise<Answer> {
  const target = new URL(config.toolsUrl);
  const proxy = config.proxy();
  const tunnel = proxy
    ? await connectTunnel(new URL(proxy), target, config.timeoutMs)
    : undefined;
  const secure = target.protocol === "https:";
  return new Promise((resolve, reject) => {
    const options = {
      method: "PUT",
      headers,
      agent: false as const,
      ...(tunnel
        ? {
            createConnection: () =>
              secure
                ? tlsConnect({ socket: tunnel, servername: target.hostname })
                : tunnel,
          }
        : {}),
    };
    const onResponse = (res: IncomingMessage) => {
      const chunks: Buffer[] = [];
      let size = 0;
      res.on("data", (chunk: Buffer) => {
        size += chunk.length;
        if (size <= MAX_RESPONSE_BYTES) chunks.push(chunk);
      });
      res.on("end", () =>
        resolve({
          status: res.statusCode ?? 0,
          body: Buffer.concat(chunks).toString("utf8"),
        }),
      );
      res.on("error", reject);
    };
    const req = secure
      ? httpsRequest(target, options, onResponse)
      : httpRequest(target, options, onResponse);
    req.setTimeout(config.timeoutMs, () =>
      req.destroy(Object.assign(new Error("timeout"), { code: "ETIMEDOUT" })),
    );
    req.on("error", reject);
    req.end(body);
  });
}

/** Open a CONNECT tunnel to `target` through an `http://` proxy. */
function connectTunnel(
  proxy: URL,
  target: URL,
  timeoutMs: number,
): Promise<Socket> {
  const port = target.port || (target.protocol === "https:" ? "443" : "80");
  const authority = `${target.hostname}:${port}`;
  const headers: OutgoingHttpHeaders = { Host: authority };
  if (proxy.username) {
    const credentials = `${decodeURIComponent(proxy.username)}:${decodeURIComponent(proxy.password)}`;
    headers["Proxy-Authorization"] =
      `Basic ${Buffer.from(credentials).toString("base64")}`;
  }
  return new Promise((resolve, reject) => {
    const req = httpRequest({
      host: proxy.hostname,
      port: proxy.port || 80,
      method: "CONNECT",
      path: authority,
      headers,
      agent: false,
    });
    req.setTimeout(timeoutMs, () =>
      req.destroy(Object.assign(new Error("timeout"), { code: "ETIMEDOUT" })),
    );
    req.on("connect", (res: IncomingMessage, socket: Socket) => {
      if (res.statusCode !== 200) {
        socket.destroy();
        reject(
          Object.assign(new Error("proxy refused the tunnel"), {
            code: `PROXY_${res.statusCode ?? 0}`,
          }),
        );
        return;
      }
      resolve(socket);
    });
    req.on("error", reject);
    req.end();
  });
}
