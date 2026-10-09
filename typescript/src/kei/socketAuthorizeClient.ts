import { request as httpRequest, type IncomingMessage } from "node:http";
import type { KeiProxyAuthorizeRequest } from "./evaluator.js";

export const DEFAULT_RUNTIME_SOCKET_PATH = "/run/kei-proxy/runtime.sock";
const SESSION_SCHEMA = "kei.session/v2";
const AUTHORIZE_REQUEST_SCHEMA = "kei.authorize-request/v1";
const AUTHORIZE_RESPONSE_SCHEMA = "kei.authorize-response/v1";

export interface KeiProxySocketIdentity {
  source: string;
  externalId: string;
}

export interface KeiProxySocketSession extends KeiProxySocketIdentity {
  readonly sessionId: string;
}

export interface KeiProxySocketAuthorizeClientOptions {
  socketPath?: string;
  timeoutMs?: number;
  env?: Readonly<Record<string, string | undefined>>;
}

/** Unix-socket authorize client. It is opt-in; the CLI client remains unchanged. */
export class KeiProxySocketAuthorizeClient {
  private readonly socketPath: string;
  private readonly timeoutMs: number;
  private readonly env: Readonly<Record<string, string | undefined>>;
  private readonly sessions = new Map<string, KeiProxySocketSession>();

  constructor(options: KeiProxySocketAuthorizeClientOptions = {}) {
    this.socketPath = options.socketPath ?? (options.env ?? process.env).KEI_RUNTIME_SOCKET_PATH ?? DEFAULT_RUNTIME_SOCKET_PATH;
    this.timeoutMs = options.timeoutMs ?? 10_000;
    this.env = options.env ?? process.env;
  }

  /** Create or reuse a daemon session for an explicit source/external identity. */
  async openSession(identity: KeiProxySocketIdentity): Promise<KeiProxySocketSession> {
    const { source, externalId } = identity;
    if (typeof source !== "string" || !source.trim() || typeof externalId !== "string" || !externalId.trim()) {
      throw new Error("socket session requires non-empty source and externalId");
    }
    const key = JSON.stringify([source, externalId]);
    const cached = this.sessions.get(key);
    if (cached) return cached;
    const body = await this.send("PUT", "/v1/session", undefined, {
      schema: SESSION_SCHEMA,
      source,
      external_id: externalId,
    });
    if (body.schema !== SESSION_SCHEMA || typeof body.session_id !== "string" || !body.session_id) {
      throw new Error("runtime returned an invalid session response");
    }
    const session = Object.freeze({ source, externalId, sessionId: body.session_id });
    this.sessions.set(key, session);
    return session;
  }

  /** Authorize on the named session; recreate it once if the daemon reports it missing. */
  async authorize(session: KeiProxySocketSession, input: KeiProxyAuthorizeRequest): Promise<Record<string, unknown>> {
    if (!session?.source || !session.externalId || !session.sessionId) {
      throw new Error("socket authorize requires an explicit named session");
    }
    for (let attempt = 0; attempt < 2; attempt++) {
      try {
        const response = await this.send("POST", "/v1/authorize", session.sessionId, {
          schema: AUTHORIZE_REQUEST_SCHEMA,
          tool: input.tool,
          action: input.action,
          resource: input.resource,
          span_id: input.spanId,
          parent_span: input.parentSpan,
          delegation_depth: input.delegationDepth,
          agent_id: input.agentId,
          framework: input.framework,
          tool_args_digest: input.toolArgsDigest,
        });
        if (response.schema !== AUTHORIZE_RESPONSE_SCHEMA || typeof response.decision !== "string") {
          throw new Error("runtime returned an invalid authorize response");
        }
        if (attempt === 0 && response.reason_code === "session_not_found") {
          this.sessions.delete(JSON.stringify([session.source, session.externalId]));
          session = await this.openSession({ source: session.source, externalId: session.externalId });
          continue;
        }
        return response;
      } catch (error) {
        if (attempt !== 0 || !(error instanceof SocketHttpError) || error.code !== "session_not_found") throw error;
        this.sessions.delete(JSON.stringify([session.source, session.externalId]));
        session = await this.openSession({ source: session.source, externalId: session.externalId });
      }
    }
    throw new Error("socket authorize failed closed");
  }

  private async send(method: string, path: string, sessionId: string | undefined, body: Record<string, unknown>): Promise<Record<string, unknown>> {
    const token = this.env.KEI_RUNTIME_TOKEN;
    if (!token) throw new Error("KEI_RUNTIME_TOKEN is not set");
    let response: { status: number; body: string };
    try {
      response = await new Promise((resolve, reject) => {
        const req = httpRequest({ socketPath: this.socketPath, path, method, agent: false, headers: {
          Authorization: `Bearer ${token}`, "Content-Type": "application/json", ...(sessionId ? { "X-Kei-Session": sessionId } : {}),
        } }, (res: IncomingMessage) => {
          const chunks: Buffer[] = [];
          res.on("data", (chunk: Buffer) => chunks.push(chunk));
          res.on("end", () => resolve({ status: res.statusCode ?? 0, body: Buffer.concat(chunks).toString("utf8") }));
        });
        req.setTimeout(this.timeoutMs, () => req.destroy(new Error("timeout")));
        req.on("error", reject);
        req.end(JSON.stringify(body));
      });
    } catch {
      throw new Error("runtime socket unavailable or timed out");
    }
    if (response.status < 200 || response.status >= 300) {
      let code = "";
      try { code = String((JSON.parse(response.body) as { error?: { code?: unknown } }).error?.code ?? ""); } catch { /* safe generic failure */ }
      throw new SocketHttpError(response.status === 401 ? "runtime authentication failed" : "runtime request denied", code);
    }
    if (response.status === 204 || !response.body.trim()) return {};
    try {
      const parsed: unknown = JSON.parse(response.body);
      if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) throw new Error();
      return parsed as Record<string, unknown>;
    } catch {
      throw new Error("runtime returned malformed JSON");
    }
  }
}

class SocketHttpError extends Error {
  constructor(message: string, readonly code: string) { super(message); }
}
