import { randomUUID } from "node:crypto";
import { spawn } from "node:child_process";
import { createReadStream } from "node:fs";
import { createHash } from "node:crypto";
import {
  CHILD_ENV_ALLOWLIST,
  RUNTIME_LINK_SDK_LANG,
  linkEventToWire,
  normalizeRuntimeLinkConfig,
  parseChildLine,
  waitBackoff,
  type ChildEvent,
  type FailureClass,
  type LinkEvent,
  type LinkStatus,
  type RuntimeIdentity,
  type RuntimeLink,
  type RuntimeLinkConfig,
} from "./runtimeLink.js";

export interface RuntimeChild {
  readonly stdout: AsyncIterable<Uint8Array | string>;
  readonly exitCode: Promise<number>;
  kill(signal?: "SIGTERM" | "SIGKILL"): void;
}

/** Injectable process boundary for tests and embedders. */
export interface RuntimeChildFactory {
  start(
    command: string,
    args: readonly string[],
    env: Readonly<Record<string, string>>,
  ): RuntimeChild;
}

export interface RuntimeLinkOptions {
  env?: Readonly<Record<string, string | undefined>>;
  childFactory?: RuntimeChildFactory;
  now?: () => Date;
  random?: () => number;
  sdkVersion?: string;
  sdkInstanceId?: string;
  audit?: (event: Readonly<Record<string, unknown>>) => void | Promise<void>;
}

/** Node process implementation. stderr is drained and discarded; never surfaced to audit or logs. */
export class NodeRuntimeChildFactory implements RuntimeChildFactory {
  start(
    command: string,
    args: readonly string[],
    env: Readonly<Record<string, string>>,
  ): RuntimeChild {
    const child = spawn(command, [...args], {
      env: { ...env },
      stdio: ["ignore", "pipe", "ignore"],
      detached: process.platform !== "win32",
    });
    const stdout = child.stdout;
    if (!stdout) throw new Error("runtime stdout unavailable");
    const exitCode = new Promise<number>((resolve) => {
      child.once("error", () => resolve(1));
      child.once("close", (code) => resolve(code ?? 1));
    });
    return {
      stdout: (async function* () {
        for await (const chunk of stdout) yield chunk as Uint8Array;
      })(),
      exitCode,
      kill: (signal = "SIGTERM") => {
        if (process.platform !== "win32" && child.pid) {
          try {
            process.kill(-child.pid, signal);
          } catch {
            child.kill(signal);
          }
        } else {
          child.kill(signal);
        }
      },
    };
  }
}

const SDK_VERSION = "0.2.1";
const OUTCOME_FAILURE: Partial<Record<string, FailureClass>> = {
  catalog_unreachable: "catalog_unreachable",
  catalog_timeout: "catalog_timeout",
  catalog_error: "catalog_error",
  catalog_backpressure: "catalog_backpressure",
  catalog_rejected: "contract_mismatch",
};

/** Create a supervisor that owns the harness → kei-connector-runtime process and heartbeat boundary. */
export function newRuntimeLink(
  config: RuntimeLinkConfig,
  options: RuntimeLinkOptions = {},
): RuntimeLink {
  return new RuntimeLinkImpl(normalizeRuntimeLinkConfig(config), options);
}

class RuntimeLinkImpl implements RuntimeLink {
  private readonly eventsQueue: LinkEvent[] = [];
  private readonly waiters: Array<(event: IteratorResult<LinkEvent>) => void> =
    [];
  private readonly controller = new AbortController();
  private readonly sdkId: string;
  private state: LinkStatus["state"];
  private reason: FailureClass | undefined;
  private identityValue?: RuntimeIdentity;
  private runId = "";
  private lastBeatAt?: Date;
  private lastCatalogOkAt?: Date;
  private consecutiveFails = 0;
  private restarts = 0;
  private started = false;
  private stopped = false;
  private supervisor?: Promise<void>;
  private child?: RuntimeChild;

  constructor(
    private readonly config: RuntimeLinkConfig,
    private readonly options: RuntimeLinkOptions,
  ) {
    this.state = config.enabled ? "disabled" : "disabled";
    this.sdkId = options.sdkInstanceId ?? randomUUID();
  }

  async start(signal?: AbortSignal): Promise<void> {
    if (this.started || this.stopped || !this.config.enabled) return;
    this.started = true;
    if (signal?.aborted) {
      await this.stop();
      return;
    }
    signal?.addEventListener(
      "abort",
      () => {
        void this.stop();
      },
      { once: true },
    );
    this.supervisor = this.supervise();
    // Starting is non-blocking; only config errors can reject.
  }

  async stop(): Promise<void> {
    if (this.stopped) {
      await this.withTimeout(this.supervisor);
      return;
    }
    this.stopped = true;
    this.controller.abort(new Error("RuntimeLink stopped"));
    this.transition("stopped");
    this.child?.kill("SIGTERM");
    await this.withTimeout(this.supervisor);
    this.child = undefined;
    for (const resolve of this.waiters.splice(0))
      resolve({ done: true, value: undefined });
  }

  status(): LinkStatus {
    return {
      state: this.state,
      reason: this.reason,
      lastBeatAt: this.lastBeatAt,
      lastCatalogOkAt: this.lastCatalogOkAt,
      consecutiveFails: this.consecutiveFails,
      runId: this.runId,
      restarts: this.restarts,
    };
  }
  identity(): RuntimeIdentity | undefined {
    return this.identityValue && { ...this.identityValue };
  }
  events(): AsyncIterable<LinkEvent> {
    const queue = this.eventsQueue;
    const waiters = this.waiters;
    return {
      [Symbol.asyncIterator]() {
        return {
          next: () =>
            queue.length
              ? Promise.resolve({ done: false as const, value: queue.shift()! })
              : new Promise<IteratorResult<LinkEvent>>((resolve) =>
                  waiters.push(resolve),
                ),
          return: () =>
            Promise.resolve({ done: true as const, value: undefined }),
        };
      },
    };
  }

  private async supervise(): Promise<void> {
    while (!this.controller.signal.aborted) {
      if (this.restarts > 0) {
        this.transition("reconnecting");
        try {
          await waitBackoff(this.config.restart, this.restarts - 1, {
            signal: this.controller.signal,
            random: this.options.random,
          });
        } catch {
          return;
        }
      }
      if (this.controller.signal.aborted) return;
      this.transition("starting");
      let failure: FailureClass = "runtime_unavailable";
      try {
        failure = await this.runChild();
      } catch (e) {
        if (this.controller.signal.aborted) return;
        failure =
          e instanceof BinaryFailure
            ? "config_invalid"
            : "runtime_unresponsive";
      }
      if (this.controller.signal.aborted || this.state === "terminal") return;
      this.restarts++;
      this.consecutiveFails++;
      this.reason = failure;
      if (
        failure === "config_invalid" ||
        failure === "contract_mismatch" ||
        failure === "installation_unauthorized"
      ) {
        this.transition("terminal", failure);
        return;
      }
      this.transition("degraded", failure);
      if (this.restarts >= 5) {
        this.transition("terminal", "runtime_crashloop");
        return;
      }
    }
  }

  private async runChild(): Promise<FailureClass> {
    await this.verifyBinary();
    const env = this.childEnv();
    const factory = this.options.childFactory ?? new NodeRuntimeChildFactory();
    const child = factory.start(
      this.config.binary.path,
      ["runtime", "heartbeat", "--output", "jsonl"],
      env,
    );
    this.child = child;
    let identitySeen = false;
    let heartbeatDeadline = Date.now() + this.config.beatTimeoutMs;
    const lines = this.consume(child.stdout);
    const abortPromise = new Promise<never>((_, reject) => {
      if (this.controller.signal.aborted) reject(this.controller.signal.reason);
      else
        this.controller.signal.addEventListener(
          "abort",
          () => reject(this.controller.signal.reason),
          { once: true },
        );
    });
    try {
      while (!this.controller.signal.aborted) {
        const remaining = identitySeen
          ? Math.max(1, heartbeatDeadline - Date.now())
          : this.config.graceMs;
        let timer: ReturnType<typeof setTimeout> | undefined;
        const next = await Promise.race([
          lines.next(),
          new Promise<never>((_, reject) => {
            timer = setTimeout(
              () => reject(new Error("runtime heartbeat timeout")),
              remaining,
            );
          }),
          abortPromise,
        ]).finally(() => {
          if (timer) clearTimeout(timer);
        });
        if (next.done) {
          const code = await child.exitCode;
          const failure: FailureClass =
            code === 2
              ? "contract_mismatch"
              : code === 3
                ? "installation_unauthorized"
                : code >= 4
                  ? "config_invalid"
                  : "runtime_unavailable";
          if (
            failure === "contract_mismatch" ||
            failure === "installation_unauthorized" ||
            failure === "config_invalid"
          ) {
            this.transition("terminal", failure);
          }
          return failure;
        }
        let ev: ChildEvent;
        try {
          ev = parseChildLine(next.value);
        } catch (error) {
          this.consecutiveFails++;
          if (
            error instanceof Error &&
            error.message.includes("contract_mismatch")
          )
            this.reason = "contract_mismatch";
          continue;
        }
        if (ev.kind === "identity") {
          this.identityValue = ev.identity;
          this.runId = ev.identity.runId;
          identitySeen = true;
          heartbeatDeadline = Date.now() + this.config.beatTimeoutMs;
          this.transition("connected");
        } else if (ev.kind === "beat") {
          this.runId = ev.beat.runId || this.runId;
          this.lastBeatAt = this.options.now?.() ?? new Date();
          heartbeatDeadline = Date.now() + this.config.beatTimeoutMs;
          if (ev.beat.outcome === "ok") {
            this.lastCatalogOkAt = this.lastBeatAt;
            this.consecutiveFails = 0;
            if (this.state !== "connected") this.transition("connected");
          } else if (ev.beat.outcome === "unauthorized") {
            this.transition("terminal", "installation_unauthorized");
            return "installation_unauthorized";
          } else {
            this.consecutiveFails++;
            this.transition(
              "degraded",
              OUTCOME_FAILURE[ev.beat.outcome] ?? "runtime_unavailable",
            );
          }
        } else if (ev.kind === "terminal") {
          const reason: FailureClass =
            ev.terminal.reason === "unauthorized"
              ? "installation_unauthorized"
              : ev.terminal.reason === "contract"
                ? "contract_mismatch"
                : "runtime_unavailable";
          this.transition("terminal", reason);
          return reason;
        }
      }
      return "runtime_unavailable";
    } finally {
      child.kill("SIGTERM");
      this.child = undefined;
    }
  }

  private async *consume(
    source: AsyncIterable<Uint8Array | string>,
  ): AsyncGenerator<string> {
    const decoder = new TextDecoder();
    let pending = "";
    for await (const chunk of source) {
      if (this.controller.signal.aborted) return;
      pending +=
        typeof chunk === "string"
          ? chunk
          : decoder.decode(chunk, { stream: true });
      let idx: number;
      while ((idx = pending.indexOf("\n")) >= 0) {
        yield pending.slice(0, idx + 1);
        pending = pending.slice(idx + 1);
      }
      if (new TextEncoder().encode(pending).length > 4097)
        pending = " ".repeat(4098);
    }
    if (pending) yield pending;
  }

  private childEnv(): Record<string, string> {
    const source = this.options.env ?? process.env;
    const out: Record<string, string> = {};
    for (const key of CHILD_ENV_ALLOWLIST) {
      const value = source[key];
      if (value !== undefined) out[key] = value;
    }
    out.KEI_AGENTWARE_SDK_LANG = RUNTIME_LINK_SDK_LANG;
    out.KEI_AGENTWARE_SDK_VERSION = this.options.sdkVersion ?? SDK_VERSION;
    out.KEI_RUNTIME_CONTROL_PLANE_URL = this.config.controlPlaneUrl;
    out.KEI_HARNESS_KIND = this.config.harness.kind;
    out.KEI_HARNESS_VERSION = this.config.harness.version;
    out.KEI_DEPLOYMENT_ENV = this.config.harness.deploymentEnv;
    return out;
  }

  private async verifyBinary(): Promise<void> {
    const expected = this.config.binary.sha256;
    if (!expected) return;
    const hash = createHash("sha256");
    try {
      for await (const chunk of createReadStream(this.config.binary.path))
        hash.update(chunk);
    } catch {
      throw new BinaryFailure();
    }
    if (hash.digest("hex") !== expected) throw new BinaryFailure();
  }

  private transition(state: LinkStatus["state"], reason?: FailureClass): void {
    if (this.state === state && this.reason === reason) return;
    this.state = state;
    this.reason = reason;
    const name = (
      {
        starting: "runtime.link.started",
        connected: "runtime.link.connected",
        degraded: "runtime.link.degraded",
        reconnecting: "runtime.link.reconnecting",
        terminal: "runtime.link.terminal",
        stopped: "runtime.link.stopped",
      } as const
    )[
      state as
        | "starting"
        | "connected"
        | "degraded"
        | "reconnecting"
        | "terminal"
        | "stopped"
    ];
    if (!name) return;
    const event: LinkEvent = {
      name,
      at: this.options.now?.() ?? new Date(),
      state,
      reason: reason ?? "",
      runId: this.runId,
      seq: Date.now(),
      sdkInstanceId: this.sdkId,
      harness: {
        kind: this.config.harness.kind as LinkEvent["harness"]["kind"],
        version: this.config.harness.version,
        deploymentEnv: this.config.harness.deploymentEnv,
        sdkLang: RUNTIME_LINK_SDK_LANG,
        sdkVersion: this.options.sdkVersion ?? SDK_VERSION,
      },
      restarts: this.restarts,
      consecutiveFails: this.consecutiveFails,
    };
    try {
      const safe = linkEventToWire(event) as unknown as Record<string, unknown>;
      if (this.waiters.length)
        this.waiters.shift()!({ done: false, value: event });
      else {
        if (this.eventsQueue.length >= 64) this.eventsQueue.shift();
        this.eventsQueue.push(event);
      }
      if (this.options.audit)
        void Promise.resolve(this.options.audit(safe)).catch(() => undefined);
    } catch {
      /* audit/event serialization must fail closed without leaking arbitrary input */
    }
  }

  private async withTimeout(task?: Promise<void>): Promise<void> {
    if (!task) return;
    let timer: ReturnType<typeof setTimeout> | undefined;
    await Promise.race([
      task,
      new Promise<void>((resolve) => {
        timer = setTimeout(resolve, this.config.stopTimeoutMs);
      }),
    ]);
    if (timer) clearTimeout(timer);
  }
}

class BinaryFailure extends Error {}
