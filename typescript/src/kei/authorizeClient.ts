/**
 * `KeiProxyAuthorizeClient`: runs `kei-proxy authorize` for `KeiProxyEvaluator`.
 *
 * TypeScript port of `pedro_agentware.kei.authorize_client`. The kei-proxy
 * contract (kei-connector-runtime `authorize.go`): stdout is one JSON object;
 * exit 0 for allow / enrollment_required, exit 1 for deny, and exit 1 with
 * nothing on stdout for every error.
 *
 * Proxy stdout can carry a credential or an enrollment claim link and stderr
 * is free-form, so neither is ever logged or copied into an error message.
 */
import { spawnSync } from "node:child_process";
import {
  KEI_PROXY_AFFIRMATIVE_DECISIONS,
  KeiProxyAuthorizeError,
  type KeiProxyAuthorizationClient,
  type KeiProxyAuthorizeRequest,
} from "./evaluator.js";

/**
 * Parent variables the kei-proxy child may inherit. KEI_PROXY_* identity
 * variables are deliberately absent: identity travels as flags.
 */
export const AUTHORIZE_CHILD_ENV_ALLOWLIST = [
  "PATH",
  "HOME",
  "TZ",
  "KEI_RUNTIME_TOKEN",
  "KEI_RUNTIME_CONTROL_PLANE_URL",
  "KEI_API_URL",
  "KEI_PROXY_REGISTRY",
  "KEI_PROXY_AUDIT",
  "KEI_PROXY_AUDIT_MAX_BYTES",
  "KEI_PROXY_PROVIDER_USER",
  "KEI_CREDENTIAL_STORE_INSTALLATION_ID",
  "KEI_AWS_SECRETS_MANAGER_ENDPOINT",
] as const;

export const DEFAULT_AUTHORIZE_TIMEOUT_MS = 10_000;

const RUNTIME_TOKEN_ENV = "KEI_RUNTIME_TOKEN";
export interface KeiProxyAuthorizeClientOptions {
  /** Path or name of the kei-proxy binary. Default `kei-proxy`. */
  executable?: string;
  /** Milliseconds to wait for one authorize call. Default 10s. */
  timeoutMs?: number;
  /** Parent environment to draw allowlisted variables from. Default `process.env`. */
  env?: Readonly<Record<string, string | undefined>>;
  /** Variables passed to the child verbatim, for settings outside the allowlist. */
  extraEnv?: Readonly<Record<string, string>>;
}

/**
 * Runs `kei-proxy authorize` once per call. The runtime token reaches the
 * child only through its environment, never argv.
 *
 * Uses `spawnSync` because `PolicyEvaluator.evaluate` is synchronous; the
 * event loop is blocked for at most `timeoutMs`.
 */
export class KeiProxyAuthorizeClient implements KeiProxyAuthorizationClient {
  private readonly executable: string;
  private readonly timeoutMs: number;
  private readonly env?: Readonly<Record<string, string | undefined>>;
  private readonly extraEnv: Readonly<Record<string, string>>;

  constructor(options: KeiProxyAuthorizeClientOptions = {}) {
    this.executable = options.executable ?? "kei-proxy";
    this.timeoutMs = options.timeoutMs ?? DEFAULT_AUTHORIZE_TIMEOUT_MS;
    this.env = options.env;
    this.extraEnv = options.extraEnv ?? {};
  }

  /** The exact environment the kei-proxy child receives. */
  childEnv(): Record<string, string> {
    const source = this.env ?? process.env;
    const out: Record<string, string> = {};
    for (const key of AUTHORIZE_CHILD_ENV_ALLOWLIST) {
      const value = source[key];
      if (value !== undefined) out[key] = value;
    }
    return { ...out, ...this.extraEnv };
  }

  authorize(request: KeiProxyAuthorizeRequest): Record<string, unknown> {
    const env = this.childEnv();
    if (!env[RUNTIME_TOKEN_ENV]) {
      throw new KeiProxyAuthorizeError(
        "missing_token",
        `${RUNTIME_TOKEN_ENV} is not set`,
      );
    }

    const result = spawnSync(this.executable, keiProxyAuthorizeArgv(request), {
      env,
      stdio: ["ignore", "pipe", "pipe"],
      timeout: this.timeoutMs,
      killSignal: "SIGKILL",
      maxBuffer: 1024 * 1024,
      windowsHide: true,
    });

    if (result.error) {
      const code = (result.error as NodeJS.ErrnoException).code;
      if (code === "ETIMEDOUT") {
        throw new KeiProxyAuthorizeError(
          "proxy_timeout",
          `no answer within ${this.timeoutMs}ms`,
        );
      }
      if (code === "ENOBUFS") {
        throw new KeiProxyAuthorizeError(
          "malformed_response",
          "stdout exceeded the size limit",
        );
      }
      throw new KeiProxyAuthorizeError(
        "proxy_unavailable",
        `could not start kei-proxy (${code ?? "error"})`,
      );
    }
    if (result.status === null) {
      throw new KeiProxyAuthorizeError(
        "proxy_error",
        `kei-proxy terminated by ${result.signal ?? "signal"}`,
      );
    }
    return parseKeiProxyAuthorizeOutput(
      result.stdout.toString("utf8"),
      result.status,
    );
  }
}

/** The argv (after the executable) for one authorize call. */
export function keiProxyAuthorizeArgv(
  request: KeiProxyAuthorizeRequest,
): string[] {
  const argv = [
    "authorize",
    "--user",
    request.userId,
    "--tool",
    request.tool,
    "--action",
    request.action,
    "--resource",
    request.resource,
  ];
  const optional: Array<[string, string]> = [
    ["--span-id", request.spanId],
    ["--invoking-subject", request.invokingSubject],
    ["--parent-span", request.parentSpan],
    [
      "--delegation-depth",
      request.delegationDepth > 0 ? String(request.delegationDepth) : "",
    ],
    ["--agent-id", request.agentId],
    ["--agent-version", request.agentVersion],
    ["--framework", request.framework],
    ["--tool-args-digest", request.toolArgsDigest],
    ["--resources", request.resources.join(",")],
  ];
  for (const [flag, value] of optional) {
    if (value) argv.push(flag, value);
  }
  return argv;
}

/** Validate one authorize result against the exit-code contract. */
export function parseKeiProxyAuthorizeOutput(
  stdout: string,
  exitCode: number,
): Record<string, unknown> {
  if (exitCode !== 0 && exitCode !== 1) {
    throw new KeiProxyAuthorizeError(
      "proxy_error",
      `kei-proxy exited ${exitCode}`,
    );
  }
  const text = stdout.trim();
  if (!text) {
    if (exitCode !== 0) {
      throw new KeiProxyAuthorizeError(
        "proxy_error",
        `kei-proxy exited ${exitCode}`,
      );
    }
    throw new KeiProxyAuthorizeError(
      "empty_response",
      "kei-proxy printed nothing",
    );
  }
  let parsed: unknown;
  try {
    parsed = JSON.parse(text);
  } catch {
    throw new KeiProxyAuthorizeError(
      "malformed_response",
      "stdout is not JSON",
    );
  }
  if (!isPlainObject(parsed)) {
    throw new KeiProxyAuthorizeError(
      "malformed_response",
      "stdout is not a JSON object",
    );
  }
  const decision = parsed.decision;
  if (
    exitCode !== 0 &&
    typeof decision === "string" &&
    KEI_PROXY_AFFIRMATIVE_DECISIONS.has(decision.trim().toLowerCase())
  ) {
    throw new KeiProxyAuthorizeError(
      "exit_mismatch",
      `${decision} with exit ${exitCode}`,
    );
  }
  return parsed;
}

function isPlainObject(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}
