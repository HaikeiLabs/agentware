/**
 * `KeiProxyAuthorizeClient`: runs `kei-proxy authorize` for `KeiProxyEvaluator`.
 *
 * TypeScript port of `pedro_agentware.kei.authorize_client`. The kei-proxy
 * contract (kei-connector-runtime `authorize.go`): stdout is one JSON object;
 * exit 0 for allow / enrollment_required, exit 1 for deny, and exit 1 with
 * nothing on stdout for every error.
 *
 * Security contract (`fixtures/kei/authorize-cases.v1.json`):
 *
 * - The child is spawned asynchronously, so the harness's event loop keeps
 *   running while kei-proxy answers.
 * - The child environment is exactly the allowlisted parent variables plus
 *   explicit `extraEnv`: no PATH, no HOME. The executable is resolved to an
 *   absolute path in the parent, once, at construction.
 * - With `expectedSha256` the binary is re-proved before every spawn: the
 *   path must equal its realpath, it is opened with O_NOFOLLOW, must be a
 *   regular file, and its SHA-256 is streamed from that descriptor and compared
 *   in constant time. Any drift denies (`pin_mismatch`) without spawning. What
 *   remains is the hash-then-exec race; close it by making the binary's
 *   directory and its ancestors writable only by the deploy owner.
 * - The child runs in its own process group; a timeout or oversized stdout
 *   SIGKILLs the whole group.
 * - Proxy stdout can carry a credential or an enrollment claim link and stderr
 *   is free-form, so stderr is never read and neither is ever logged or copied
 *   into an error message.
 */
import { spawn, type ChildProcess } from "node:child_process";
import { createHash, timingSafeEqual } from "node:crypto";
import { accessSync, constants as fsConstants, statSync } from "node:fs";
import { open, realpath, type FileHandle } from "node:fs/promises";
import { delimiter, isAbsolute, join, resolve } from "node:path";
import {
  KEI_PROXY_AFFIRMATIVE_DECISIONS,
  KeiProxyAuthorizeError,
  type KeiProxyAuthorizationClient,
  type KeiProxyAuthorizeRequest,
} from "./evaluator.js";

/**
 * Parent variables the kei-proxy child may inherit. PATH and HOME are
 * deliberately absent, as are KEI_PROXY_* identity variables: identity
 * travels as flags.
 */
export const AUTHORIZE_CHILD_ENV_ALLOWLIST = [
  "TZ",
  "KEI_RUNTIME_TOKEN",
  "KEI_RUNTIME_CONTROL_PLANE_URL",
  "KEI_RUNTIME_VERSION",
  "KEI_API_URL",
  "KEI_PROXY_REGISTRY",
  "KEI_PROXY_AUDIT",
  "KEI_PROXY_AUDIT_MAX_BYTES",
  "KEI_PROXY_PROVIDER_USER",
  "KEI_CREDENTIAL_STORE_INSTALLATION_ID",
  "KEI_AWS_SECRETS_MANAGER_ENDPOINT",
] as const;

export const DEFAULT_AUTHORIZE_TIMEOUT_MS = 10_000;

/** Upper bound on captured stdout. */
const MAX_STDOUT_BYTES = 1024 * 1024;
/** Upper bound on the executable's size when digesting it. */
const MAX_EXECUTABLE_BYTES = 256 * 1024 * 1024;
const HASH_CHUNK_BYTES = 64 * 1024;
const SHA256_HEX = /^[0-9a-fA-F]{64}$/;

const RUNTIME_TOKEN_ENV = "KEI_RUNTIME_TOKEN";

export interface KeiProxyAuthorizeClientOptions {
  /**
   * Path or name of the kei-proxy binary. Default `kei-proxy`. A bare name is
   * resolved once against the parent PATH (`env.PATH`, else `process.env.PATH`);
   * the child is always spawned by absolute path.
   */
  executable?: string;
  /**
   * Expected SHA-256 of the binary (64 hex digits). When set, `executable` must
   * be an absolute, symlink-free path, and the binary is re-hashed before
   * every spawn; a mismatch denies with `pin_mismatch`.
   */
  expectedSha256?: string;
  /** Milliseconds to wait for one authorize call. Default 10s. */
  timeoutMs?: number;
  /** Parent environment to draw allowlisted variables from. Default `process.env`. */
  env?: Readonly<Record<string, string | undefined>>;
  /** Variables passed to the child verbatim, for settings outside the allowlist. */
  extraEnv?: Readonly<Record<string, string>>;
}

/**
 * Runs `kei-proxy authorize` once per call without blocking the event loop.
 * The runtime token reaches the child only through its environment, never argv.
 */
export class KeiProxyAuthorizeClient implements KeiProxyAuthorizationClient {
  /** Absolute path to spawn, or undefined when a bare name was not found. */
  private readonly executable: string | undefined;
  private readonly expectedDigest: Buffer | undefined;
  private readonly timeoutMs: number;
  private readonly env?: Readonly<Record<string, string | undefined>>;
  private readonly extraEnv: Readonly<Record<string, string>>;

  constructor(options: KeiProxyAuthorizeClientOptions = {}) {
    const executable = options.executable ?? "kei-proxy";
    if (options.expectedSha256 !== undefined) {
      if (!SHA256_HEX.test(options.expectedSha256)) {
        throw new TypeError("expectedSha256 must be 64 hex digits");
      }
      if (!isAbsolute(executable)) {
        throw new TypeError("a pinned executable must be an absolute path");
      }
      this.expectedDigest = Buffer.from(options.expectedSha256, "hex");
    }
    this.env = options.env;
    this.executable = resolveExecutable(
      executable,
      (this.env ?? process.env).PATH,
    );
    this.timeoutMs = options.timeoutMs ?? DEFAULT_AUTHORIZE_TIMEOUT_MS;
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

  async authorize(
    request: KeiProxyAuthorizeRequest,
  ): Promise<Record<string, unknown>> {
    const env = this.childEnv();
    if (!env[RUNTIME_TOKEN_ENV]) {
      throw new KeiProxyAuthorizeError(
        "missing_token",
        `${RUNTIME_TOKEN_ENV} is not set`,
      );
    }
    if (this.executable === undefined) {
      throw new KeiProxyAuthorizeError(
        "proxy_unavailable",
        "kei-proxy was not found on PATH",
      );
    }
    if (this.expectedDigest !== undefined) {
      await proveExecutable(this.executable, this.expectedDigest);
    }
    const { stdout, exitCode } = await this.run(
      this.executable,
      keiProxyAuthorizeArgv(request),
      env,
    );
    return parseKeiProxyAuthorizeOutput(stdout, exitCode);
  }

  /** Spawn once in a new process group; resolve with stdout and exit code. */
  private run(
    executable: string,
    argv: string[],
    env: Record<string, string>,
  ): Promise<{ stdout: string; exitCode: number }> {
    return new Promise((resolvePromise, rejectPromise) => {
      let child: ChildProcess;
      try {
        child = spawn(executable, argv, {
          env,
          stdio: ["ignore", "pipe", "ignore"],
          detached: process.platform !== "win32",
          shell: false,
          windowsHide: true,
        });
      } catch (err) {
        rejectPromise(unavailable(err));
        return;
      }

      let settled = false;
      const chunks: Buffer[] = [];
      let total = 0;
      const killGroup = (): void => {
        const pid = child.pid;
        if (pid === undefined) return;
        try {
          process.kill(process.platform === "win32" ? pid : -pid, "SIGKILL");
        } catch {
          try {
            child.kill("SIGKILL");
          } catch {
            // Already gone.
          }
        }
      };
      const fail = (error: KeiProxyAuthorizeError, kill: boolean): void => {
        if (settled) return;
        settled = true;
        clearTimeout(timer);
        if (kill) killGroup();
        rejectPromise(error);
      };

      const timer = setTimeout(() => {
        fail(
          new KeiProxyAuthorizeError(
            "proxy_timeout",
            `no answer within ${this.timeoutMs}ms`,
          ),
          true,
        );
      }, this.timeoutMs);

      child.on("error", (err) => {
        if (child.pid === undefined) {
          fail(unavailable(err), false);
        } else {
          fail(new KeiProxyAuthorizeError("proxy_error", "kei-proxy failed"), true);
        }
      });
      child.stdout?.on("data", (chunk: Buffer) => {
        if (settled) return;
        total += chunk.length;
        if (total > MAX_STDOUT_BYTES) {
          fail(
            new KeiProxyAuthorizeError(
              "malformed_response",
              "stdout exceeded the size limit",
            ),
            true,
          );
          return;
        }
        chunks.push(chunk);
      });
      child.on("close", (code, signal) => {
        if (settled) return;
        settled = true;
        clearTimeout(timer);
        if (code === null) {
          rejectPromise(
            new KeiProxyAuthorizeError(
              "proxy_error",
              `kei-proxy terminated by ${signal ?? "signal"}`,
            ),
          );
          return;
        }
        resolvePromise({
          stdout: Buffer.concat(chunks).toString("utf8"),
          exitCode: code,
        });
      });
    });
  }
}

function unavailable(err: unknown): KeiProxyAuthorizeError {
  const code = (err as NodeJS.ErrnoException | undefined)?.code;
  return new KeiProxyAuthorizeError(
    "proxy_unavailable",
    `could not start kei-proxy (${code ?? "error"})`,
  );
}

/**
 * An absolute path for `executable`. Names containing a separator resolve
 * against the working directory; bare names are looked up in the absolute
 * entries of `pathVar` (relative entries are skipped).
 */
function resolveExecutable(
  executable: string,
  pathVar: string | undefined,
): string | undefined {
  if (isAbsolute(executable)) return executable;
  if (executable.includes("/") || executable.includes("\\")) {
    return resolve(executable);
  }
  for (const dir of (pathVar ?? "").split(delimiter)) {
    if (!dir || !isAbsolute(dir)) continue;
    const candidate = join(dir, executable);
    try {
      if (!statSync(candidate).isFile()) continue;
      accessSync(candidate, fsConstants.X_OK);
      return candidate;
    } catch {
      // Not here; keep looking.
    }
  }
  return undefined;
}

function pinMismatch(detail: string): KeiProxyAuthorizeError {
  return new KeiProxyAuthorizeError("pin_mismatch", detail);
}

/** Re-prove the pinned executable immediately before a spawn. */
async function proveExecutable(path: string, expected: Buffer): Promise<void> {
  let real: string;
  try {
    real = await realpath(path);
  } catch {
    throw pinMismatch("executable is missing");
  }
  if (real !== path) throw pinMismatch("executable path is not canonical");
  const noFollow = fsConstants.O_NOFOLLOW;
  if (noFollow === undefined) throw pinMismatch("O_NOFOLLOW is unavailable");
  let handle: FileHandle;
  try {
    handle = await open(path, fsConstants.O_RDONLY | noFollow);
  } catch {
    throw pinMismatch("executable could not be opened without following links");
  }
  let actual: Buffer;
  try {
    if (!(await handle.stat()).isFile()) {
      throw pinMismatch("executable is not a regular file");
    }
    actual = await digestOfHandle(handle);
  } finally {
    await handle.close().catch(() => undefined);
  }
  if (actual.length !== expected.length || !timingSafeEqual(actual, expected)) {
    throw pinMismatch("executable digest does not match the pin");
  }
}

/** SHA-256 streamed off an already-open descriptor by explicit position. */
async function digestOfHandle(handle: FileHandle): Promise<Buffer> {
  const hash = createHash("sha256");
  const buffer = Buffer.allocUnsafe(HASH_CHUNK_BYTES);
  let position = 0;
  for (;;) {
    const { bytesRead } = await handle.read(buffer, 0, buffer.length, position);
    if (bytesRead === 0) break;
    position += bytesRead;
    if (position > MAX_EXECUTABLE_BYTES) {
      throw pinMismatch("executable exceeds the size limit");
    }
    hash.update(buffer.subarray(0, bytesRead));
  }
  return hash.digest();
}

/**
 * The argv (after the executable) for one authorize call. `agentId` and
 * `workspaceId` are not sent: kei-proxy (>= 0.1.11) resolves the agent from
 * the runtime installation and workspace scope from the runtime token.
 */
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
