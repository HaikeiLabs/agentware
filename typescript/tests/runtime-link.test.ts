import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import {
  BEAT_OUTCOMES,
  CHILD_ENV_ALLOWLIST,
  ChildLineError,
  FAILURE_CLASSES,
  HARNESS_KINDS,
  InvalidLinkEventError,
  LIFECYCLE_EVENT_NAMES,
  LINK_STATES,
  MAX_CHILD_LINE_BYTES,
  RUNTIME_LINK_BOUNDS,
  RUNTIME_LINK_CONFIG_ERROR_CODES,
  RUNTIME_LINK_CONTRACT_VERSION,
  RUNTIME_LINK_EVENT_BUFFER_SIZE,
  RUNTIME_LINK_SDK_LANG,
  RuntimeLinkConfigError,
  SDK_LANGS,
  backoffDelayMs,
  defaultRuntimeLinkConfig,
  linkEventFromWire,
  linkEventToWire,
  normalizeRuntimeLinkConfig,
  newRuntimeLink,
  parseChildLine,
  runtimeLinkConfigFromEnv,
  waitBackoff,
  Action,
  InMemoryAuditor,
  MiddlewareImpl,
  SimplePolicyEvaluator,
} from "../src/index.js";
import type {
  ChildEvent,
  HarnessEnvelope,
  LinkEvent,
  RuntimeLinkConfig,
  RuntimeChild,
  RuntimeChildFactory,
} from "../src/index.js";

// Jest runs from typescript/, so the shared fixtures are one level up.
const CONTRACTS = resolve(
  process.cwd(),
  "..",
  "testing",
  "contracts",
  "runtime-link",
);
// eslint-disable-next-line @typescript-eslint/no-explicit-any
const load = (name: string): any =>
  JSON.parse(readFileSync(resolve(CONTRACTS, name), "utf8"));

const ENUMS = load("enums.json");
// eslint-disable-next-line @typescript-eslint/no-explicit-any
type Case = Record<string, any>;
const CONFIG: { token_canary: string; cases: Case[] } =
  load("config-cases.json");
const BACKOFF: { cases: Case[] } = load("backoff-cases.json");
const WIRE: { cases: Case[]; synthesized: Case[] } = load(
  "wire/child-events.json",
);
const EXIT_CODE_CASES: { cases: Case[] } = load("exit-code-cases.json");
const CHILD_ENV_ALLOWLIST_FIXTURE: {
  allowlist: string[];
  canaries: string[];
  sdk_injected: string[];
  env_count_max: number;
} = load("child-env-allowlist.json");
const REDACTION: {
  record_keys: string[];
  harness_keys: string[];
  canaries: string[];
  cases: Case[];
} = load("lifecycle-redaction.json");

describe("RuntimeLink contract enums", () => {
  it("match the shared fixture exactly", () => {
    expect(RUNTIME_LINK_CONTRACT_VERSION).toBe(ENUMS.contract_version);
    expect([...HARNESS_KINDS]).toEqual(ENUMS.harness_kinds);
    expect([...LINK_STATES]).toEqual(ENUMS.link_states);
    expect([...FAILURE_CLASSES]).toEqual(ENUMS.failure_classes);
    expect([...BEAT_OUTCOMES]).toEqual(ENUMS.beat_outcomes);
    expect([...LIFECYCLE_EVENT_NAMES]).toEqual(ENUMS.lifecycle_events);
    expect([...SDK_LANGS]).toEqual(ENUMS.sdk_langs);
    expect([...RUNTIME_LINK_CONFIG_ERROR_CODES]).toEqual(
      ENUMS.config_error_codes,
    );
    expect([...CHILD_ENV_ALLOWLIST]).toEqual(ENUMS.child_env_allowlist);
    expect(MAX_CHILD_LINE_BYTES).toBe(ENUMS.max_child_line_bytes);
    expect(RUNTIME_LINK_EVENT_BUFFER_SIZE).toBe(ENUMS.event_buffer_size);
  });
});

function toContract(cfg: RuntimeLinkConfig): unknown {
  return {
    enabled: cfg.enabled,
    token_env: cfg.token.env,
    control_plane_url: cfg.controlPlaneUrl,
    binary_path: cfg.binary.path,
    binary_sha256: cfg.binary.sha256,
    interval_s: cfg.intervalMs / 1000,
    beat_timeout_s: cfg.beatTimeoutMs / 1000,
    grace_s: cfg.graceMs / 1000,
    restart_min_s: cfg.restart.minMs / 1000,
    restart_max_s: cfg.restart.maxMs / 1000,
    stable_reset_s: cfg.restart.stableResetMs / 1000,
    stop_timeout_s: cfg.stopTimeoutMs / 1000,
    log_count: cfg.logCount,
    harness: {
      kind: cfg.harness.kind,
      version: cfg.harness.version,
      deployment_env: cfg.harness.deploymentEnv,
    },
  };
}

function harnessArg(
  raw: Record<string, string> | undefined,
): Partial<HarnessEnvelope> {
  if (!raw) return {};
  return {
    kind: raw.kind,
    version: raw.version,
    deploymentEnv: raw.deployment_env,
  };
}

describe("runtimeLinkConfigFromEnv contract", () => {
  it.each(CONFIG.cases)("$name", (c) => {
    const run = (): RuntimeLinkConfig =>
      runtimeLinkConfigFromEnv(c.env, harnessArg(c.harness));
    if (!c.expect.ok) {
      let err: unknown;
      try {
        run();
      } catch (e) {
        err = e;
      }
      expect(err).toBeInstanceOf(RuntimeLinkConfigError);
      expect((err as RuntimeLinkConfigError).code).toBe(c.expect.error);
      expect(String(err)).not.toContain(CONFIG.token_canary);
      return;
    }
    const cfg = run();
    expect(toContract(cfg)).toEqual(c.expect.config);
    expect(JSON.stringify(cfg)).not.toContain(CONFIG.token_canary);
  });
});

describe("normalizeRuntimeLinkConfig", () => {
  const base: RuntimeLinkConfig = {
    ...defaultRuntimeLinkConfig(),
    enabled: true,
    controlPlaneUrl: "http://localhost:8080",
    harness: { kind: "cli", version: "", deploymentEnv: "" },
  };

  it("accepts a valid direct config", () => {
    expect(() => normalizeRuntimeLinkConfig(base)).not.toThrow();
  });

  it.each<[string, Partial<RuntimeLinkConfig>, string]>([
    [
      "unknown kind",
      { harness: { kind: "telegram", version: "", deploymentEnv: "" } },
      "harness_kind",
    ],
    [
      "legacy token env",
      { token: { env: "KEI_HARNESS_TOKEN" } },
      "legacy_token",
    ],
    ["other token env", { token: { env: "DISCORD_TOKEN" } }, "invalid_value"],
    [
      "timeout equal to half interval",
      { intervalMs: 20_000, beatTimeoutMs: 10_000 },
      "beat_timeout",
    ],
    ["NaN timeout", { beatTimeoutMs: Number.NaN }, "beat_timeout"],
    ["negative grace", { graceMs: -1 }, "out_of_range"],
    [
      "grace too long",
      { graceMs: RUNTIME_LINK_BOUNDS.maxGraceMs + 1 },
      "out_of_range",
    ],
    ["stop timeout zero", { stopTimeoutMs: 0 }, "out_of_range"],
    ["stop timeout too long", { stopTimeoutMs: 120_000 }, "out_of_range"],
    [
      "stable reset zero",
      { restart: { minMs: 1000, maxMs: 300_000, stableResetMs: 0 } },
      "out_of_range",
    ],
    ["fractional log count", { logCount: 1.5 }, "out_of_range"],
    ["empty binary", { binary: { path: "", sha256: "" } }, "binary"],
    ["url without host", { controlPlaneUrl: "https://" }, "control_plane_url"],
  ])("%s", (_name, changes, code) => {
    expect(() => normalizeRuntimeLinkConfig({ ...base, ...changes })).toThrow(
      expect.objectContaining({ code }),
    );
  });

  it("clamps the interval of a disabled config", () => {
    const cfg = normalizeRuntimeLinkConfig({
      ...defaultRuntimeLinkConfig(),
      intervalMs: 0,
      beatTimeoutMs: 5_000,
    });
    expect(cfg.intervalMs).toBe(RUNTIME_LINK_BOUNDS.minIntervalMs);
  });
});

describe("backoff contract", () => {
  it.each(BACKOFF.cases)("attempt $attempt rand $rand", (c) => {
    expect(
      backoffDelayMs({ minMs: c.min_ms, maxMs: c.max_ms }, c.attempt, c.rand),
    ).toBe(c.delay_ms);
  });
});

describe("waitBackoff cancellation", () => {
  const hour = { minMs: 3_600_000, maxMs: 3_600_000 };

  it("rejects immediately when the signal is already aborted", async () => {
    const ac = new AbortController();
    const reason = new Error("stopped");
    ac.abort(reason);
    await expect(waitBackoff(hour, 0, { signal: ac.signal })).rejects.toBe(
      reason,
    );
  });

  it("rejects promptly when aborted mid-wait", async () => {
    const ac = new AbortController();
    const started = Date.now();
    const waiting = waitBackoff(hour, 0, {
      signal: ac.signal,
      random: () => 0.5,
    });
    setTimeout(() => ac.abort(new Error("stopped")), 20);
    await expect(waiting).rejects.toThrow("stopped");
    expect(Date.now() - started).toBeLessThan(1_000);
  });

  it("resolves when the delay elapses", async () => {
    await expect(
      waitBackoff({ minMs: 1, maxMs: 1 }, 0, { random: () => 0.5 }),
    ).resolves.toBeUndefined();
  });
});

function childKind(line: string): [string, unknown] {
  let ev: ChildEvent;
  try {
    ev = parseChildLine(line);
  } catch (e) {
    if (e instanceof ChildLineError) return [e.kind, undefined];
    throw e;
  }
  switch (ev.kind) {
    case "identity": {
      const i = ev.identity;
      return [
        "identity",
        {
          run_id: i.runId,
          installation_id: i.installationId,
          org_id: i.orgId,
          workspace_id: i.workspaceId,
          platform: i.platform,
          status: i.status,
          binding_status: i.bindingStatus,
          runtime_version: i.runtimeVersion,
        },
      ];
    }
    case "beat": {
      const b = ev.beat;
      return [
        "beat",
        {
          run_id: b.runId,
          seq: b.seq,
          at: b.at,
          outcome: b.outcome,
          http_status: b.httpStatus,
          latency_ms: b.latencyMs,
          next_in_ms: b.nextInMs,
        },
      ];
    }
    case "terminal":
      return [
        "terminal",
        { run_id: ev.terminal.runId, reason: ev.terminal.reason },
      ];
    default:
      return ["ignored", undefined];
  }
}

function padLine(n: number): string {
  const head = '{"v":1,"event":"pad","pad":"';
  const tail = '"}';
  return head + "x".repeat(n - head.length - tail.length) + tail;
}

describe("parseChildLine contract", () => {
  it.each(WIRE.cases)("$name", (c) => {
    const [kind, fields] = childKind(c.line);
    expect(kind).toBe(c.expect.kind);
    if (c.expect.fields) expect(fields).toEqual(c.expect.fields);
  });

  it.each(WIRE.synthesized)("$name", (c) => {
    const line = padLine(c.bytes);
    expect(Buffer.byteLength(line)).toBe(c.bytes);
    expect(childKind(line + "\n")[0]).toBe(c.expect.kind);
  });
});

describe("exit code mapping fixture", () => {
  it("has valid failure class values", () => {
    const valid = new Set(FAILURE_CLASSES);
    for (const c of EXIT_CODE_CASES.cases) {
      if (c.failure_class) {
        expect(valid.has(c.failure_class)).toBe(true);
      }
    }
  });
  // TODO: Add behavioral assertions when handleChildExit is implemented.
});

describe("child env allowlist fixture", () => {
  it("matches the CHILD_ENV_ALLOWLIST constant", () => {
    expect([...CHILD_ENV_ALLOWLIST_FIXTURE.allowlist].sort()).toEqual(
      [...CHILD_ENV_ALLOWLIST].sort(),
    );
  });

  it("has complete fixture structure", () => {
    expect(CHILD_ENV_ALLOWLIST_FIXTURE.canaries).toBeDefined();
    expect(CHILD_ENV_ALLOWLIST_FIXTURE.sdk_injected).toBeDefined();
    expect(CHILD_ENV_ALLOWLIST_FIXTURE.env_count_max).toBe(
      CHILD_ENV_ALLOWLIST_FIXTURE.allowlist.length +
        CHILD_ENV_ALLOWLIST_FIXTURE.sdk_injected.length,
    );
  });
  // TODO: Add behavioral assertions when buildCommand is implemented.
});

describe("lifecycle event redaction contract", () => {
  it.each(REDACTION.cases)("$name", (c) => {
    if (!c.expect.ok) {
      expect(() => linkEventFromWire(c.input)).toThrow(InvalidLinkEventError);
      return;
    }
    const out = JSON.stringify(linkEventToWire(linkEventFromWire(c.input)));
    for (const canary of REDACTION.canaries) expect(out).not.toContain(canary);
    const decoded = JSON.parse(out);
    expect(decoded).toEqual(c.expect.serialized);
    expect(Object.keys(decoded)).toEqual(REDACTION.record_keys);
    expect(Object.keys(decoded.harness).sort()).toEqual(
      [...REDACTION.harness_keys].sort(),
    );
  });

  it("rejects invalid direct construction and normalizes time to UTC", () => {
    const ev: LinkEvent = {
      name: "runtime.link.started",
      at: new Date("2026-09-23T18:00:00+02:00"),
      state: "starting",
      reason: "",
      runId: "",
      seq: 0,
      sdkInstanceId: "sdk-1",
      harness: {
        kind: "cli",
        version: "",
        deploymentEnv: "",
        sdkLang: RUNTIME_LINK_SDK_LANG,
        sdkVersion: "",
      },
      restarts: 0,
      consecutiveFails: 0,
    };
    expect(linkEventToWire(ev).at).toBe("2026-09-23T16:00:00.000Z");
    expect(() => linkEventToWire({ ...ev, at: new Date("nope") })).toThrow(
      InvalidLinkEventError,
    );
    expect(() =>
      linkEventToWire({
        ...ev,
        harness: { ...ev.harness, kind: "telegram" as never },
      }),
    ).toThrow(InvalidLinkEventError);
  });
});

describe("RuntimeLink supervised runtime integration", () => {
  const config: RuntimeLinkConfig = {
    ...defaultRuntimeLinkConfig(),
    enabled: true,
    controlPlaneUrl: "https://kei.example.test",
    harness: { kind: "assistant", version: "1.0", deploymentEnv: "test" },
    graceMs: 100,
    intervalMs: 15_000,
    beatTimeoutMs: 100,
  };

  it("owns identity and heartbeat, strips child env, and cancels cleanly", async () => {
    let childEnv: Readonly<Record<string, string>> = {};
    const factory: RuntimeChildFactory = {
      start: (_command, _args, env): RuntimeChild => {
        childEnv = env;
        return {
          stdout: (async function* () {
            yield JSON.stringify({
              v: 1,
              event: "identity",
              run_id: "run-1",
              installation_id: "inst-1",
              org_id: "org-1",
            }) + "\n";
            yield JSON.stringify({
              v: 1,
              event: "beat",
              run_id: "run-1",
              at: "2026-09-25T10:00:00Z",
              outcome: "ok",
            }) + "\n";
            await new Promise<void>(() => undefined);
          })(),
          exitCode: new Promise<number>(() => undefined),
          kill: () => undefined,
        };
      },
    };
    const audit: Array<Record<string, unknown>> = [];
    const link = newRuntimeLink(config, {
      env: {
        KEI_RUNTIME_TOKEN: "token-canary",
        PATH: "/bin",
        DISCORD_TOKEN: "must-not-pass",
      },
      childFactory: factory,
      sdkInstanceId: "sdk-test",
      audit: (ev) => {
        audit.push({ ...ev });
      },
    });
    await link.start();
    const deadline = Date.now() + 1_000;
    while (
      (!link.identity() || !link.status().lastCatalogOkAt) &&
      Date.now() < deadline
    ) {
      await new Promise((resolve) => setTimeout(resolve, 5));
    }
    expect(link.identity()).toMatchObject({
      runId: "run-1",
      installationId: "inst-1",
      orgId: "org-1",
    });
    expect(link.status().state).toBe("connected");
    expect(link.status().lastCatalogOkAt).toBeInstanceOf(Date);
    expect(childEnv).toMatchObject({
      KEI_RUNTIME_TOKEN: "token-canary",
      PATH: "/bin",
      KEI_AGENTWARE_SDK_LANG: "typescript",
    });
    expect(childEnv).not.toHaveProperty("DISCORD_TOKEN");
    const serializedAudit = JSON.stringify(audit);
    expect(serializedAudit).not.toContain("token-canary");
    expect(serializedAudit).not.toContain("installation_id");
    expect(Object.keys(audit[0] ?? {})).toEqual(REDACTION.record_keys);

    const executed: string[] = [];
    const toolAudit = new InMemoryAuditor();
    const middleware = new MiddlewareImpl({
      execute: (tool) => {
        executed.push(tool);
        return ["ok", true, ""];
      },
    })
      .withPolicy(
        new SimplePolicyEvaluator({
          default_deny: true,
          rules: [{ name: "allow-read", tools: ["read_file"], action: Action.ALLOW }],
        }),
      )
      .withAuditor(toolAudit);
    const caller = { trusted: false, user_id: "human-1", session_id: "session-1" };
    expect(middleware.execute("read_file", { query: "content-canary" }, caller)[1]).toBe(true);
    expect(middleware.execute("write_file", { body: "content-canary" }, caller)[1]).toBe(false);
    expect(executed).toEqual(["read_file"]);
    expect(toolAudit.query({ session_id: "session-1" })).toHaveLength(2);
    expect(toolAudit.query({ session_id: "session-1" })[1].decision.action).toBe(Action.DENY);

    await link.stop();
    expect(link.status().state).toBe("stopped");
  });

  it("restarts when a heartbeat misses beatTimeoutMs after identity", async () => {
    let starts = 0;
    const factory: RuntimeChildFactory = {
      start: () => {
        starts++;
        return {
          stdout: (async function* () {
            yield JSON.stringify({
              v: 1,
              event: "identity",
              run_id: `run-${starts}`,
              installation_id: "inst-1",
              org_id: "org-1",
            }) + "\n";
            await new Promise<void>(() => undefined);
          })(),
          exitCode: new Promise<number>(() => undefined),
          kill: () => undefined,
        };
      },
    };
    const audit: Array<Record<string, unknown>> = [];
    const link = newRuntimeLink(
      {
        ...config,
        graceMs: 300,
        beatTimeoutMs: 40,
        restart: { minMs: 1_000, maxMs: 1_000, stableResetMs: 1_000 },
      },
      {
        childFactory: factory,
        audit: (event) => {
          audit.push({ ...event });
        },
      },
    );

    await link.start();
    const deadline = Date.now() + 2_500;
    while (starts < 2 && Date.now() < deadline) {
      await new Promise((resolve) => setTimeout(resolve, 5));
    }

    expect(starts).toBeGreaterThanOrEqual(2);
    expect(audit).toContainEqual(
      expect.objectContaining({
        state: "degraded",
        reason: "runtime_unresponsive",
      }),
    );
    await link.stop();
  });

  it("preserves fail-closed allow/deny policy decisions and their audit lineage", () => {
    const executed: string[] = [];
    const auditor = new InMemoryAuditor();
    const middleware = new MiddlewareImpl({
      execute: (tool) => {
        executed.push(tool);
        return ["ok", true, ""];
      },
    })
      .withPolicy(
        new SimplePolicyEvaluator({
          default_deny: true,
          rules: [
            { name: "allow-read", tools: ["read_file"], action: Action.ALLOW },
          ],
        }),
      )
      .withAuditor(auditor);
    const caller = {
      trusted: false,
      user_id: "human-1",
      session_id: "session-1",
    };
    expect(
      middleware.execute("read_file", { query: "content-canary" }, caller)[1],
    ).toBe(true);
    expect(
      middleware.execute("write_file", { body: "content-canary" }, caller)[1],
    ).toBe(false);
    expect(executed).toEqual(["read_file"]);
    expect(auditor.query({ session_id: "session-1" })).toHaveLength(2);
    expect(auditor.query({ session_id: "session-1" })[1].decision.action).toBe(
      Action.DENY,
    );
  });
});
