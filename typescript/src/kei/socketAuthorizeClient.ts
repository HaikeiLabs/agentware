/**
 * A client for the `kei-proxy serve` daemon over its Unix socket.
 *
 * TypeScript counterpart of `pedro_agentware.kei.runtime_socket` and Go's
 * `kei/runtimesocket`. All three replay the shared cases in
 * `testing/contracts/runtime-socket` (see `docs/kei-runtime-socket-parity.md`).
 *
 * - `PUT` / `DELETE /v1/session`: open and close a named `kei.session/v2`
 *   session for an explicit `source` / `externalId` identity. Requests in the
 *   session carry `X-Kei-Session` and never name the user themselves.
 * - `POST /v1/authorize`: one `kei.authorize-request/v1` decision.
 * - `GET /v1/models`, `POST /v1/chat/completions`: the model routes.
 * - `GET /v1/policy-bundle/status`: which bundle the daemon holds.
 *
 * Fail closed: an unreachable daemon, a timeout, a non-2xx answer, a body that
 * is not a JSON object, or a response with the wrong schema rejects with
 * `KeiProxySocketError`, a `KeiProxyAuthorizeError`, so `KeiProxyEvaluator`
 * turns each into a DENY with the matching reason class. A session the daemon
 * no longer knows (`session_not_found`) is reopened once.
 *
 * The runtime token (`KEI_RUNTIME_TOKEN`) is read per request and sent only in
 * the `Authorization` header. Errors carry the daemon's error code, never its
 * message or a response body.
 */
import { request as httpRequest, type IncomingMessage } from "node:http";
import { createInterface } from "node:readline";
import { KeiProxyAuthorizeClient } from "./authorizeClient.js";
import type { ToolManifestV3 } from "../toolspec/index.js";
import {
  KeiProxyAuthorizeError,
  type KeiProxyAuthorizationClient,
  type KeiProxyAuthorizeRequest,
  type KeiProxyReasonClass,
} from "./evaluator.js";

export const DEFAULT_RUNTIME_SOCKET_PATH = "/run/kei-proxy/runtime.sock";
export const RUNTIME_SOCKET_PATH_ENV = "KEI_RUNTIME_SOCKET_PATH";
export const SESSION_HEADER = "X-Kei-Session";
export const SESSION_SCHEMA = "kei.session/v2";
export const AUTHORIZE_REQUEST_SCHEMA = "kei.authorize-request/v1";
export const AUTHORIZE_RESPONSE_SCHEMA = "kei.authorize-response/v1";
export const POLICY_BUNDLE_STATUS_SCHEMA = "kei.policy-bundle-status/v1";
export const SESSION_NOT_FOUND = "session_not_found";
export const DEFAULT_SOCKET_TIMEOUT_MS = 10_000;
export const DEFAULT_MODEL_TIMEOUT_MS = 120_000;

const RUNTIME_TOKEN_ENV = "KEI_RUNTIME_TOKEN";
const PROXY_INSTALL_COMMAND =
  'curl -fsSL https://haikeilabs.com/install.sh | sh -s -- --proxy-only -d "$HOME/.local/bin"';
/** Upper bound on a buffered (non-streaming) daemon answer. */
const MAX_BODY_BYTES = 8 * 1024 * 1024;

type Env = Readonly<Record<string, string | undefined>>;
type JsonObject = Record<string, unknown>;

/**
 * A daemon call that produced no usable answer. Callers fail closed.
 * `code` is the daemon's error code (`{"error": {"code": ...}}`) when it sent
 * one, e.g. `invalid_runtime_token` or `session_not_found`; otherwise `""`.
 */
export class KeiProxySocketError extends KeiProxyAuthorizeError {
  readonly code: string;

  constructor(reasonClass: KeiProxyReasonClass, detail: string, code = "") {
    super(reasonClass, detail);
    this.name = "KeiProxySocketError";
    this.code = code;
  }
}

export interface KeiProxySocketIdentity {
  source: string;
  externalId: string;
}

export interface KeiProxySocketSession extends KeiProxySocketIdentity {
  readonly sessionId: string;
}

/**
 * The fields a socket authorize sends. Only `tool` is required; empty values
 * are left out. A full `KeiProxyAuthorizeRequest` is accepted: its user,
 * invoking subject, agent version, resources and workspace are not
 * sent, because the daemon takes identity from the authenticated session and
 * the workspace from the runtime token.
 */
export type KeiProxySocketAuthorizeRequest = Pick<
  KeiProxyAuthorizeRequest,
  "tool"
> &
  Partial<
    Pick<
      KeiProxyAuthorizeRequest,
      | "action"
      | "resource"
      | "spanId"
      | "parentSpan"
      | "delegationDepth"
      | "framework"
      | "toolArgsDigest"
    >
  >;

export interface KeiProxySocketAuthorizeClientOptions {
  /** The daemon's socket. Default `$KEI_RUNTIME_SOCKET_PATH`, then `/run/kei-proxy/runtime.sock`. */
  socketPath?: string;
  /** Milliseconds for session, authorize, model-list and status calls. Default 10s. */
  timeoutMs?: number;
  /** Idle milliseconds for chat completions. Default 120s. */
  modelTimeoutMs?: number;
  /** Where `KEI_RUNTIME_TOKEN` and the socket path are read. Default `process.env`. */
  env?: Env;
}

interface SendOptions {
  sessionId?: string;
  body?: JsonObject;
  timeoutMs?: number;
}

interface Buffered {
  status: number;
  body: Buffer;
}

/**
 * Client for the kei-proxy daemon's Unix socket. One session is cached per
 * `(source, externalId)`. Every method rejects with `KeiProxySocketError`
 * rather than resolving to a partial answer.
 */
export class KeiProxySocketAuthorizeClient {
  readonly socketPath: string;
  private readonly timeoutMs: number;
  private readonly modelTimeoutMs: number;
  private readonly env: Env;
  private readonly sessions = new Map<string, KeiProxySocketSession>();

  constructor(options: KeiProxySocketAuthorizeClientOptions = {}) {
    this.env = options.env ?? process.env;
    this.socketPath =
      options.socketPath ||
      this.env[RUNTIME_SOCKET_PATH_ENV] ||
      DEFAULT_RUNTIME_SOCKET_PATH;
    this.timeoutMs = options.timeoutMs ?? DEFAULT_SOCKET_TIMEOUT_MS;
    this.modelTimeoutMs = options.modelTimeoutMs ?? DEFAULT_MODEL_TIMEOUT_MS;
  }

  // Sessions ---------------------------------------------------------------

  /** Create, or reuse, the daemon session for an explicit source/external identity. */
  async openSession(
    identity: KeiProxySocketIdentity,
  ): Promise<KeiProxySocketSession> {
    const { source, externalId } = identity;
    if (typeof source !== "string" || !source.trim()) {
      throw new TypeError("a socket session needs a non-empty source");
    }
    if (typeof externalId !== "string" || !externalId.trim()) {
      throw new TypeError("a socket session needs a non-empty externalId");
    }
    const key = sessionKey(identity);
    const cached = this.sessions.get(key);
    if (cached) return cached;
    const body = jsonObject(
      await this.send("PUT", "/v1/session", {
        body: { schema: SESSION_SCHEMA, source, external_id: externalId },
      }),
    );
    if (
      body.schema !== SESSION_SCHEMA ||
      typeof body.session_id !== "string" ||
      !body.session_id
    ) {
      throw new KeiProxySocketError(
        "malformed_response",
        "the daemon returned an invalid session",
      );
    }
    const session: KeiProxySocketSession = Object.freeze({
      source,
      externalId,
      sessionId: body.session_id,
    });
    this.sessions.set(key, session);
    return session;
  }

  /** End `session` on the daemon. A session the daemon already ended is not an error. */
  async closeSession(session: KeiProxySocketSession): Promise<void> {
    this.forget(session);
    const response = await this.send("DELETE", "/v1/session", {
      sessionId: session.sessionId,
    });
    if (
      response.status === 404 &&
      errorCode(response.body) === SESSION_NOT_FOUND
    )
      return;
    checkStatus(response);
  }

  /** Drop the cached session for `identity`; the next call opens a new one. */
  forgetSession(identity: KeiProxySocketIdentity): void {
    this.sessions.delete(sessionKey(identity));
  }

  // Authorize --------------------------------------------------------------

  /**
   * Ask the daemon for one decision in `session`. Resolves to the
   * `kei.authorize-response/v1` object; only `permit`/`allow` is an allow, and
   * a deny resolves rather than rejects.
   */
  async authorize(
    session: KeiProxySocketSession,
    input: KeiProxySocketAuthorizeRequest,
  ): Promise<JsonObject> {
    if (!session?.source || !session.externalId || !session.sessionId) {
      throw new TypeError("socket authorize needs an explicit named session");
    }
    const request: JsonObject = {
      schema: AUTHORIZE_REQUEST_SCHEMA,
      tool: input.tool,
    };
    const optional: JsonObject = {
      action: input.action,
      resource: input.resource,
      span_id: input.spanId,
      parent_span: input.parentSpan,
      delegation_depth: input.delegationDepth,
      framework: input.framework,
      tool_args_digest: input.toolArgsDigest,
    };
    for (const [key, value] of Object.entries(optional))
      if (value) request[key] = value;

    for (let attempt = 0; attempt < 2; attempt++) {
      let response: JsonObject;
      try {
        response = jsonObject(
          await this.send("POST", "/v1/authorize", {
            sessionId: session.sessionId,
            body: request,
          }),
        );
      } catch (error) {
        if (
          attempt === 0 &&
          error instanceof KeiProxySocketError &&
          error.code === SESSION_NOT_FOUND
        ) {
          session = await this.reopen(session);
          continue;
        }
        throw error;
      }
      if (
        response.schema !== AUTHORIZE_RESPONSE_SCHEMA ||
        typeof response.decision !== "string"
      ) {
        throw new KeiProxySocketError(
          "malformed_response",
          "the daemon returned an invalid authorize response",
        );
      }
      if (attempt === 0 && response.reason_code === SESSION_NOT_FOUND) {
        session = await this.reopen(session);
        continue;
      }
      return response;
    }
    throw new KeiProxySocketError(
      "proxy_error",
      "socket authorize failed closed",
    );
  }

  // Model routes -----------------------------------------------------------

  /** The models the daemon advertises (`GET /v1/models`), each with an `id`. */
  async listModels(): Promise<JsonObject[]> {
    const body = jsonObject(await this.send("GET", "/v1/models"));
    if (!Array.isArray(body.data)) {
      throw new KeiProxySocketError(
        "malformed_response",
        "the daemon returned no model list",
      );
    }
    return body.data.filter(
      (m): m is JsonObject =>
        isObject(m) && typeof m.id === "string" && m.id !== "",
    );
  }

  /** One non-streaming chat completion in `session`. */
  async chatCompletion(
    session: KeiProxySocketSession,
    payload: JsonObject,
  ): Promise<JsonObject> {
    const body = { ...payload, stream: false };
    const call = (sessionId: string) =>
      this.send("POST", "/v1/chat/completions", {
        sessionId,
        body,
        timeoutMs: this.modelTimeoutMs,
      });
    let response = await call(session.sessionId);
    if (isError(response) && errorCode(response.body) === SESSION_NOT_FOUND) {
      session = await this.reopen(session);
      response = await call(session.sessionId);
    }
    return jsonObject(response);
  }

  /**
   * A streaming chat completion in `session`, as its server-sent `data:`
   * lines. Lines pass through unchanged, `data: [DONE]` included; blank lines,
   * comments and other fields are dropped.
   */
  async *streamChatCompletion(
    session: KeiProxySocketSession,
    payload: JsonObject,
  ): AsyncGenerator<string, void, undefined> {
    const body = { ...payload, stream: true };
    for (let attempt = 0; attempt < 2; attempt++) {
      const response = await this.open("POST", "/v1/chat/completions", {
        sessionId: session.sessionId,
        body,
        timeoutMs: this.modelTimeoutMs,
      });
      const status = response.statusCode ?? 0;
      if (status < 200 || status >= 300) {
        const code = errorCode(
          await readBody(response).catch(() => Buffer.alloc(0)),
        );
        if (attempt === 0 && code === SESSION_NOT_FOUND) {
          session = await this.reopen(session);
          continue;
        }
        throw statusError(status, code);
      }
      const lines = createInterface({ input: response, crlfDelay: Infinity });
      try {
        for await (const line of lines) {
          if (line.startsWith("data:")) yield line;
        }
      } catch (error) {
        throw transportError(error, this.socketPath);
      } finally {
        lines.close();
        response.destroy();
      }
      return;
    }
  }

  // Status -----------------------------------------------------------------

  /** The daemon's `kei.policy-bundle-status/v1` (e.g. its `bundle_schema`). */
  async policyBundleStatus(): Promise<JsonObject> {
    const body = jsonObject(await this.send("GET", "/v1/policy-bundle/status"));
    if (body.schema !== POLICY_BUNDLE_STATUS_SCHEMA) {
      throw new KeiProxySocketError(
        "malformed_response",
        "the daemon returned an invalid bundle status",
      );
    }
    return body;
  }

  /** Register an explicit tool manifest with the runtime over this socket. */
  async registerToolManifest(manifest: ToolManifestV3): Promise<JsonObject> {
    return jsonObject(
      await this.send("POST", "/v1/tools:register", { body: manifest as unknown as JsonObject }),
    );
  }

  // Transport --------------------------------------------------------------

  /** One buffered request; transport failures become `KeiProxySocketError`. */
  private async send(
    method: string,
    path: string,
    options: SendOptions = {},
  ): Promise<Buffered> {
    const response = await this.open(method, path, options);
    try {
      return {
        status: response.statusCode ?? 0,
        body: await readBody(response, MAX_BODY_BYTES),
      };
    } catch (error) {
      throw transportError(error, this.socketPath);
    }
  }

  /** Send a request and resolve once the response headers arrive. */
  private open(
    method: string,
    path: string,
    options: SendOptions,
  ): Promise<IncomingMessage> {
    const token = this.env[RUNTIME_TOKEN_ENV];
    if (!token) {
      return Promise.reject(
        new KeiProxySocketError(
          "missing_token",
          `${RUNTIME_TOKEN_ENV} is not set`,
        ),
      );
    }
    const payload =
      options.body === undefined ? undefined : JSON.stringify(options.body);
    const headers: Record<string, string> = {
      Authorization: `Bearer ${token}`,
    };
    if (payload !== undefined) {
      headers["Content-Type"] = "application/json";
      headers["Content-Length"] = String(Buffer.byteLength(payload));
    }
    if (options.sessionId) headers[SESSION_HEADER] = options.sessionId;
    return new Promise((resolve, reject) => {
      const req = httpRequest(
        { socketPath: this.socketPath, path, method, headers, agent: false },
        resolve,
      );
      req.setTimeout(options.timeoutMs ?? this.timeoutMs, () =>
        req.destroy(new SocketTimeout()),
      );
      req.on("error", (error) =>
        reject(transportError(error, this.socketPath)),
      );
      req.end(payload);
    });
  }

  private async reopen(
    stale: KeiProxySocketSession,
  ): Promise<KeiProxySocketSession> {
    this.forget(stale);
    return this.openSession({
      source: stale.source,
      externalId: stale.externalId,
    });
  }

  /** Drop `session` from the cache, unless it was already replaced. */
  private forget(session: KeiProxySocketSession): void {
    const key = sessionKey(session);
    if (this.sessions.get(key)?.sessionId === session.sessionId)
      this.sessions.delete(key);
  }
}

/**
 * The socket client as a `KeiProxyAuthorizationClient` for
 * `KeiProxyEvaluator`. It opens, or reuses, the daemon session for
 * `(source, request.userId)` and authorizes in it.
 */
export class KeiProxySocketAuthorizationClient implements KeiProxyAuthorizationClient {
  readonly source: string;
  readonly client: KeiProxySocketAuthorizeClient;

  /**
   * @param source The session source for every subject, e.g. `teams` or
   *   `openwebui`: the identity namespace the daemon resolves `userId` in.
   * @param client The socket client. Default: one configured from the environment.
   */
  constructor(source: string, client?: KeiProxySocketAuthorizeClient) {
    if (!source.trim()) throw new TypeError("source must be non-empty");
    this.source = source;
    this.client = client ?? new KeiProxySocketAuthorizeClient();
  }

  async authorize(request: KeiProxyAuthorizeRequest): Promise<JsonObject> {
    if (!request.userId) {
      throw new KeiProxySocketError(
        "proxy_error",
        "no invoking subject to open a session for",
      );
    }
    const session = await this.client.openSession({
      source: this.source,
      externalId: request.userId,
    });
    return this.client.authorize(session, request);
  }
}

export interface SelectAuthorizationClientOptions {
  /** The daemon's socket; overrides `KEI_RUNTIME_SOCKET_PATH`. */
  socketPath?: string;
  /** Where the socket path and token are read. Default `process.env`. */
  env?: Env;
  /** Milliseconds for one authorize call, either client. Default 10s. */
  timeoutMs?: number;
  /** The `kei-proxy` binary for the subprocess client. */
  executable?: string;
  /** SHA-256 pin for the subprocess client's binary. */
  expectedSha256?: string;
}

/**
 * The `KeiProxyAuthorizationClient` for `KeiProxyEvaluator`: the socket client
 * when a socket is configured (`socketPath`, or `KEI_RUNTIME_SOCKET_PATH` in
 * `env`), otherwise the `kei-proxy authorize` subprocess client. The choice is
 * made once, here: a configured socket that is down is a refusal, never a
 * silent fall back to the subprocess.
 */
export function selectAuthorizationClient(
  source: string,
  options: SelectAuthorizationClientOptions = {},
): KeiProxyAuthorizationClient {
  const env = options.env ?? process.env;
  const socketPath = options.socketPath || env[RUNTIME_SOCKET_PATH_ENV];
  if (socketPath) {
    return new KeiProxySocketAuthorizationClient(
      source,
      new KeiProxySocketAuthorizeClient({
        socketPath,
        env,
        timeoutMs: options.timeoutMs,
      }),
    );
  }
  return new KeiProxyAuthorizeClient({
    executable: options.executable,
    expectedSha256: options.expectedSha256,
    timeoutMs: options.timeoutMs,
    env: options.env,
  });
}

class SocketTimeout extends Error {}

class BodyTooLarge extends Error {}

function sessionKey(identity: KeiProxySocketIdentity): string {
  return JSON.stringify([identity.source, identity.externalId]);
}

function readBody(
  response: IncomingMessage,
  limit = MAX_BODY_BYTES,
): Promise<Buffer> {
  return new Promise((resolve, reject) => {
    const chunks: Buffer[] = [];
    let size = 0;
    response.on("data", (chunk: Buffer) => {
      size += chunk.length;
      if (size > limit) {
        response.destroy(new BodyTooLarge());
        return;
      }
      chunks.push(chunk);
    });
    response.on("end", () => resolve(Buffer.concat(chunks)));
    response.on("error", reject);
    // A connection that drops mid-body, without an error event, still fails closed.
    response.on("close", () => {
      if (!response.complete)
        reject(Object.assign(new Error("aborted"), { code: "ECONNRESET" }));
    });
  });
}

function transportError(
  error: unknown,
  socketPath: string,
): KeiProxySocketError {
  if (error instanceof KeiProxySocketError) return error;
  if (error instanceof SocketTimeout) {
    return new KeiProxySocketError(
      "proxy_timeout",
      `the kei-proxy daemon did not answer in time at socket ${socketPath}`,
    );
  }
  if (error instanceof BodyTooLarge) {
    return new KeiProxySocketError(
      "malformed_response",
      "the daemon's answer exceeds the size limit",
    );
  }
  const code = (error as { code?: unknown })?.code;
  const kind =
    typeof code === "string"
      ? code
      : error instanceof Error
        ? error.name
        : "unknown";
  return new KeiProxySocketError(
    "proxy_unavailable",
    `the kei-proxy daemon is unreachable at socket ${socketPath} (${kind}); install with: ${PROXY_INSTALL_COMMAND}`,
  );
}

function statusError(status: number, code: string): KeiProxySocketError {
  const what =
    status === 401
      ? "runtime authentication failed"
      : `the daemon answered ${status}`;
  return new KeiProxySocketError(
    "proxy_error",
    code ? `${what} (${code})` : what,
    code,
  );
}

/** The daemon's `{"error": {"code": ...}}`, or `""`. */
function errorCode(raw: Buffer): string {
  let body: unknown;
  try {
    body = JSON.parse(raw.toString("utf8"));
  } catch {
    return "";
  }
  if (!isObject(body)) return "";
  const error = body.error;
  if (isObject(error)) return error.code ? String(error.code) : "";
  if (typeof error === "string") return error;
  return body.code ? String(body.code) : "";
}

function isError(response: Buffered): boolean {
  return response.status < 200 || response.status >= 300;
}

function checkStatus(response: Buffered): void {
  if (isError(response))
    throw statusError(response.status, errorCode(response.body));
}

/** A 2xx answer's JSON object body (`{}` when empty). */
function jsonObject(response: Buffered): JsonObject {
  checkStatus(response);
  const text = response.body.toString("utf8");
  if (response.status === 204 || !text.trim()) return {};
  let parsed: unknown;
  try {
    parsed = JSON.parse(text);
  } catch {
    throw new KeiProxySocketError(
      "malformed_response",
      "the daemon's answer is not JSON",
    );
  }
  if (!isObject(parsed)) {
    throw new KeiProxySocketError(
      "malformed_response",
      "the daemon's answer is not a JSON object",
    );
  }
  return parsed;
}

function isObject(value: unknown): value is JsonObject {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}
