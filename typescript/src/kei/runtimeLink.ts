/**
 * RuntimeLink contract: the harness → Agentware → kei-connector-runtime heartbeat.
 *
 * TypeScript mirror of the Go reference `go/kei/runtimelink` for
 * `docs/specs/runtime-heartbeat-liveness.md` (HAI-141). This module defines
 * configuration, status, identity, failure classes, the child's JSONL wire
 * events, and redacted lifecycle events. Process supervision is implemented
 * in `runtimeLinkRuntime.ts`. The only heartbeat path is harness → RuntimeLink
 * → kei-connector-runtime; Agentware does not call the catalog itself.
 *
 * All three SDK languages share `testing/contracts/runtime-link`. Durations
 * are milliseconds here (`intervalMs`), seconds in Python, and
 * `time.Duration` in Go.
 */

/** The child stdout event version ("v") this SDK accepts. */
export const RUNTIME_LINK_CONTRACT_VERSION = 1;
/** Bound on one child stdout line, excluding the newline. */
export const MAX_CHILD_LINE_BYTES = 4096;
/** Capacity of the RuntimeLink event ring. */
export const RUNTIME_LINK_EVENT_BUFFER_SIZE = 64;
export const RUNTIME_LINK_SDK_LANG = "typescript";

export const HARNESS_KINDS = [
  "assistant",
  "pde",
  "chat-discord",
  "chat-slack",
  "chat-teams",
  "cli",
] as const;
export type HarnessKind = (typeof HARNESS_KINDS)[number];

export const LINK_STATES = [
  "disabled",
  "starting",
  "connected",
  "degraded",
  "reconnecting",
  "terminal",
  "stopped",
] as const;
export type LinkState = (typeof LINK_STATES)[number];

/**
 * Closed failure taxonomy (spec §6.3). Separates harness↔runtime failures
 * from runtime↔catalog failures so alerts route to the right owner.
 */
export const FAILURE_CLASSES = [
  "config_invalid",
  "runtime_unavailable",
  "runtime_crashloop",
  "runtime_unresponsive",
  "contract_mismatch",
  "catalog_unreachable",
  "catalog_timeout",
  "catalog_error",
  "catalog_backpressure",
  "installation_unauthorized",
  "installation_stale",
  "installation_offline",
  "audit_backlog",
  "legacy_runtime_unverified",
] as const;
export type FailureClass = (typeof FAILURE_CLASSES)[number];

export const BEAT_OUTCOMES = [
  "ok",
  "catalog_unreachable",
  "catalog_timeout",
  "catalog_error",
  "catalog_backpressure",
  "catalog_rejected",
  "unauthorized",
] as const;
export type BeatOutcome = (typeof BEAT_OUTCOMES)[number];

export const LIFECYCLE_EVENT_NAMES = [
  "runtime.link.started",
  "runtime.link.connected",
  "runtime.link.degraded",
  "runtime.link.reconnecting",
  "runtime.link.terminal",
  "runtime.link.stopped",
] as const;
export type LifecycleEventName = (typeof LIFECYCLE_EVENT_NAMES)[number];

export const SDK_LANGS = ["go", "python", "typescript"] as const;
export type SdkLang = (typeof SDK_LANGS)[number];

/** The exact environment a runtime child may receive; never `process.env`. */
export const CHILD_ENV_ALLOWLIST = [
  "KEI_RUNTIME_TOKEN",
  "KEI_RUNTIME_CONTROL_PLANE_URL",
  "KEI_HARNESS_KIND",
  "KEI_HARNESS_VERSION",
  "KEI_DEPLOYMENT_ENV",
  "KEI_AGENTWARE_SDK_LANG",
  "KEI_AGENTWARE_SDK_VERSION",
  "KEI_HEARTBEAT_RUN_ID",
  "PATH",
  "HOME",
  "TZ",
] as const;

function isOneOf<T extends string>(
  set: readonly T[],
  value: unknown,
): value is T {
  return (
    typeof value === "string" && (set as readonly string[]).includes(value)
  );
}

// ---------------------------------------------------------------------------
// Configuration (spec §3.1, §3.2)
// ---------------------------------------------------------------------------

/** Environment variables. KEI_PROXY_* keep their legacy names (runtime contract). */
export const RUNTIME_LINK_ENV = {
  enabled: "KEI_RUNTIME_ENABLED",
  token: "KEI_RUNTIME_TOKEN",
  legacyToken: "KEI_HARNESS_TOKEN",
  controlPlaneUrl: "KEI_RUNTIME_CONTROL_PLANE_URL",
  proxyPath: "KEI_PROXY_PATH",
  proxySha: "KEI_PROXY_SHA",
  interval: "KEI_HEARTBEAT_INTERVAL",
  timeout: "KEI_HEARTBEAT_TIMEOUT",
  restartMin: "KEI_HEARTBEAT_RESTART_MIN",
  restartMax: "KEI_HEARTBEAT_RESTART_MAX",
  stableSeconds: "KEI_HEARTBEAT_STABLE_SECONDS",
  logCount: "KEI_HEARTBEAT_LOG_COUNT",
  harnessKind: "KEI_HARNESS_KIND",
  harnessVersion: "KEI_HARNESS_VERSION",
  deploymentEnv: "KEI_DEPLOYMENT_ENV",
} as const;

/** The distribution keeps the kei-proxy name; the repo is kei-connector-runtime. */
export const DEFAULT_RUNTIME_BINARY_PATH = "kei-proxy";

/**
 * Bounds. Interval is clamped; everything else is rejected when out of range
 * so a typo fails closed instead of silently changing behavior.
 */
export const RUNTIME_LINK_BOUNDS = {
  minIntervalMs: 15_000,
  maxIntervalMs: 300_000,
  maxGraceMs: 300_000,
  minRestartMs: 1_000,
  maxRestartMs: 3_600_000,
  minStableResetMs: 1_000,
  maxStableResetMs: 3_600_000,
  minStopTimeoutMs: 1_000,
  maxStopTimeoutMs: 60_000,
  maxLogCount: 100,
} as const;

export const RUNTIME_LINK_CONFIG_ERROR_CODES = [
  "invalid_value",
  "out_of_range",
  "beat_timeout",
  "harness_kind",
  "envelope",
  "legacy_token",
  "token_missing",
  "control_plane_url",
  "binary",
] as const;
export type RuntimeLinkConfigErrorCode =
  (typeof RUNTIME_LINK_CONFIG_ERROR_CODES)[number];

/**
 * Invalid RuntimeLink configuration. Carries the offending field and a stable
 * code, never the offending value, so a misplaced secret cannot leak.
 */
export class RuntimeLinkConfigError extends Error {
  constructor(
    readonly code: RuntimeLinkConfigErrorCode,
    readonly field: string,
  ) {
    super(`runtimeLink: invalid config: ${field}: ${code}`);
    this.name = "RuntimeLinkConfigError";
  }
}

/** The runtime binary and its expected SHA-256 (lowercase hex). */
export interface BinaryRef {
  path: string;
  sha256: string;
}

/** Where the runtime token lives: the env var name only, never the value. */
export interface SecretSource {
  env: string;
}

/** Child restart backoff with full jitter. */
export interface Backoff {
  minMs: number;
  maxMs: number;
  stableResetMs: number;
}

/** Declared, non-authoritative harness metadata. Never used for authorization. */
export interface HarnessEnvelope {
  kind: string;
  version: string;
  deploymentEnv: string;
}

export interface RuntimeLinkConfig {
  enabled: boolean;
  binary: BinaryRef;
  controlPlaneUrl: string;
  token: SecretSource;
  intervalMs: number;
  beatTimeoutMs: number;
  graceMs: number;
  restart: Backoff;
  stopTimeoutMs: number;
  logCount: number;
  harness: HarnessEnvelope;
}

/** The spec defaults with the link disabled. */
export function defaultRuntimeLinkConfig(): RuntimeLinkConfig {
  return {
    enabled: false,
    binary: { path: DEFAULT_RUNTIME_BINARY_PATH, sha256: "" },
    controlPlaneUrl: "",
    token: { env: RUNTIME_LINK_ENV.token },
    intervalMs: 60_000,
    beatTimeoutMs: 10_000,
    graceMs: 15_000,
    restart: { minMs: 1_000, maxMs: 300_000, stableResetMs: 300_000 },
    stopTimeoutMs: 10_000,
    logCount: 3,
    harness: { kind: "", version: "", deploymentEnv: "" },
  };
}

const VERSION_RE = /^[A-Za-z0-9._+-]{1,64}$/;
const ENV_NAME_RE = /^[A-Za-z0-9._-]{1,32}$/;
const SHA256_RE = /^[0-9a-f]{64}$/;
const ID_RE = /^[A-Za-z0-9._:-]{1,128}$/;
const REASON_RE = /^[a-z0-9_]{1,64}$/;
const TIMESTAMP_RE =
  /^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(\.\d{1,9})?(Z|[+-]\d{2}:\d{2})$/;
const ENV_INT_RE = /^[0-9]{1,9}$/;

/**
 * Clamp `intervalMs` to [15s, 300s] and validate every other field, throwing
 * RuntimeLinkConfigError on the first violation. A disabled link (local-only
 * mode) is valid, but its declared envelope and timings are still checked so
 * typos fail loudly.
 */
export function normalizeRuntimeLinkConfig(
  cfg: RuntimeLinkConfig,
): RuntimeLinkConfig {
  const b = RUNTIME_LINK_BOUNDS;
  const h = cfg.harness;
  if ((h.kind && !isOneOf(HARNESS_KINDS, h.kind)) || (cfg.enabled && !h.kind)) {
    throw new RuntimeLinkConfigError("harness_kind", "harness.kind");
  }
  if (h.version && !VERSION_RE.test(h.version)) {
    throw new RuntimeLinkConfigError("envelope", "harness.version");
  }
  if (h.deploymentEnv && !ENV_NAME_RE.test(h.deploymentEnv)) {
    throw new RuntimeLinkConfigError("envelope", "harness.deploymentEnv");
  }

  if (cfg.enabled) {
    if (cfg.token.env === RUNTIME_LINK_ENV.legacyToken) {
      throw new RuntimeLinkConfigError("legacy_token", "token");
    }
    if (cfg.token.env !== RUNTIME_LINK_ENV.token) {
      throw new RuntimeLinkConfigError("invalid_value", "token");
    }
    validateControlPlaneUrl(cfg.controlPlaneUrl);
    if (!cfg.binary.path || cfg.binary.path.includes("\0")) {
      throw new RuntimeLinkConfigError("binary", "binary.path");
    }
    if (!cfg.binary.sha256 && h.deploymentEnv === "prod") {
      throw new RuntimeLinkConfigError("binary", "binary.sha256");
    }
  }
  if (cfg.binary.sha256 && !SHA256_RE.test(cfg.binary.sha256)) {
    throw new RuntimeLinkConfigError("binary", "binary.sha256");
  }

  const intervalMs = Math.min(
    Math.max(cfg.intervalMs, b.minIntervalMs),
    b.maxIntervalMs,
  );
  if (!(cfg.beatTimeoutMs > 0) || 2 * cfg.beatTimeoutMs >= intervalMs) {
    throw new RuntimeLinkConfigError("beat_timeout", "beatTimeoutMs");
  }
  if (!inRange(cfg.graceMs, 0, b.maxGraceMs)) {
    throw new RuntimeLinkConfigError("out_of_range", "graceMs");
  }
  const r = cfg.restart;
  if (
    !(r.minMs >= b.minRestartMs) ||
    !(r.maxMs <= b.maxRestartMs) ||
    r.minMs > r.maxMs
  ) {
    throw new RuntimeLinkConfigError("out_of_range", "restart");
  }
  if (!inRange(r.stableResetMs, b.minStableResetMs, b.maxStableResetMs)) {
    throw new RuntimeLinkConfigError("out_of_range", "restart.stableResetMs");
  }
  if (!inRange(cfg.stopTimeoutMs, b.minStopTimeoutMs, b.maxStopTimeoutMs)) {
    throw new RuntimeLinkConfigError("out_of_range", "stopTimeoutMs");
  }
  if (
    !Number.isInteger(cfg.logCount) ||
    !inRange(cfg.logCount, 0, b.maxLogCount)
  ) {
    throw new RuntimeLinkConfigError("out_of_range", "logCount");
  }
  return { ...cfg, intervalMs };
}

function inRange(v: number, lo: number, hi: number): boolean {
  return v >= lo && v <= hi;
}

function validateControlPlaneUrl(raw: string): void {
  let ok = false;
  try {
    const u = new URL(raw);
    ok =
      (u.protocol === "http:" || u.protocol === "https:") &&
      u.hostname !== "" &&
      u.username === "" &&
      u.password === "";
  } catch {
    ok = false;
  }
  if (!ok)
    throw new RuntimeLinkConfigError("control_plane_url", "controlPlaneUrl");
}

export type EnvSource = Readonly<Record<string, string | undefined>>;

/**
 * Build a normalized config from the spec §3.2 variables. Non-empty fields of
 * `harness` override the KEI_HARNESS_* variables. The runtime token is only
 * checked for presence; its value is never retained.
 */
export function runtimeLinkConfigFromEnv(
  env: EnvSource = process.env,
  harness: Partial<HarnessEnvelope> = {},
): RuntimeLinkConfig {
  const E = RUNTIME_LINK_ENV;
  const get = (key: string): string => (env[key] ?? "").trim();
  const seconds = (key: string): number | undefined => {
    const raw = get(key);
    if (raw === "") return undefined;
    if (!ENV_INT_RE.test(raw))
      throw new RuntimeLinkConfigError("invalid_value", key);
    return Number(raw) * 1000;
  };

  const cfg = defaultRuntimeLinkConfig();
  cfg.intervalMs = seconds(E.interval) ?? cfg.intervalMs;
  cfg.beatTimeoutMs = seconds(E.timeout) ?? cfg.beatTimeoutMs;
  cfg.restart.minMs = seconds(E.restartMin) ?? cfg.restart.minMs;
  cfg.restart.maxMs = seconds(E.restartMax) ?? cfg.restart.maxMs;
  cfg.restart.stableResetMs =
    seconds(E.stableSeconds) ?? cfg.restart.stableResetMs;
  const logCount = get(E.logCount);
  if (logCount !== "") {
    if (!ENV_INT_RE.test(logCount))
      throw new RuntimeLinkConfigError("invalid_value", E.logCount);
    cfg.logCount = Number(logCount);
  }

  const hasToken = get(E.token) !== "";
  if (!hasToken && get(E.legacyToken) !== "") {
    throw new RuntimeLinkConfigError("legacy_token", E.legacyToken);
  }
  cfg.enabled = hasToken;
  const enabled = get(E.enabled).toLowerCase();
  if (enabled !== "") {
    if (enabled === "true" || enabled === "1") {
      if (!hasToken) throw new RuntimeLinkConfigError("token_missing", E.token);
      cfg.enabled = true;
    } else if (enabled === "false" || enabled === "0") {
      cfg.enabled = false;
    } else {
      throw new RuntimeLinkConfigError("invalid_value", E.enabled);
    }
  }

  cfg.controlPlaneUrl = get(E.controlPlaneUrl);
  cfg.binary = {
    path: get(E.proxyPath) || DEFAULT_RUNTIME_BINARY_PATH,
    sha256: get(E.proxySha),
  };
  cfg.harness = {
    kind: harness.kind || get(E.harnessKind),
    version: harness.version || get(E.harnessVersion),
    deploymentEnv: harness.deploymentEnv || get(E.deploymentEnv),
  };
  return normalizeRuntimeLinkConfig(cfg);
}

// ---------------------------------------------------------------------------
// Backoff and cancellation
// ---------------------------------------------------------------------------

const MAX_JITTER_SAMPLE = 1 - Number.EPSILON / 2;

/**
 * Full-jitter restart delay for attempt n (0-based):
 * `floor(r × min(maxMs, minMs × 2^n))`. `r` is clamped to [0, 1); negative
 * attempts count as 0.
 */
export function backoffDelayMs(
  backoff: Pick<Backoff, "minMs" | "maxMs">,
  attempt: number,
  r: number,
): number {
  const n = Math.min(Math.max(Math.trunc(attempt), 0), 30);
  const capMs = Math.min(backoff.maxMs, backoff.minMs * 2 ** n);
  const sample = Math.min(Math.max(r, 0), MAX_JITTER_SAMPLE);
  return Math.floor(sample * capMs);
}

export interface WaitBackoffOptions {
  signal?: AbortSignal;
  random?: () => number;
}

/**
 * Sleep for the jittered delay, or reject with the signal's abort reason as
 * soon as `signal` aborts, so a stop during backoff never waits out the delay.
 */
export function waitBackoff(
  backoff: Pick<Backoff, "minMs" | "maxMs">,
  attempt: number,
  options: WaitBackoffOptions = {},
): Promise<void> {
  const { signal, random = Math.random } = options;
  return new Promise<void>((resolve, reject) => {
    const abortReason = (): unknown => signal?.reason ?? new Error("aborted");
    if (signal?.aborted) {
      reject(abortReason());
      return;
    }
    const onAbort = (): void => {
      clearTimeout(timer);
      reject(abortReason());
    };
    const timer = setTimeout(
      () => {
        signal?.removeEventListener("abort", onAbort);
        resolve();
      },
      backoffDelayMs(backoff, attempt, random()),
    );
    signal?.addEventListener("abort", onAbort, { once: true });
  });
}

// ---------------------------------------------------------------------------
// Status and identity
// ---------------------------------------------------------------------------

/** Point-in-time snapshot. Diagnostic only: readiness must never depend on it. */
export interface LinkStatus {
  state: LinkState;
  /** Absent when connected. */
  reason?: FailureClass;
  lastBeatAt?: Date;
  lastCatalogOkAt?: Date;
  consecutiveFails: number;
  runId: string;
  restarts: number;
}

/** Authoritative installation identity from catalog whoami, via the runtime. */
export interface RuntimeIdentity {
  runId: string;
  installationId: string;
  orgId: string;
  workspaceId: string;
  platform: string;
  status: string;
  bindingStatus: string;
  runtimeVersion: string;
  /**
   * The installation's default agent from whoami; undefined when the runtime
   * reports none (older runtimes omit it). A harness reads its agent from
   * here and must not require an agent-ID env var.
   */
  defaultAgentId?: string;
  /** Agents assigned to the installation; empty when the runtime reports none. */
  agents: AssignedAgent[];
}

/** One agent assigned to a runtime installation. */
export interface AssignedAgent {
  agentId: string;
  isDefault: boolean;
}

// ---------------------------------------------------------------------------
// Child stdout wire events (spec §4.1)
// ---------------------------------------------------------------------------

export type ChildLineErrorKind =
  | "line_too_long"
  | "malformed"
  | "contract_mismatch";

/** A child line the SDK must drop and count. */
export class ChildLineError extends Error {
  constructor(
    readonly kind: ChildLineErrorKind,
    detail: string,
  ) {
    super(`runtimeLink: child line ${kind}: ${detail}`);
    this.name = "ChildLineError";
  }
}

export interface BeatEvent {
  runId: string;
  /** 0 when absent: the runtime omits seq on beats sent before identity. */
  seq: number;
  at: string;
  outcome: BeatOutcome;
  httpStatus: number;
  latencyMs: number;
  nextInMs: number;
}

export interface TerminalEvent {
  runId: string;
  reason: string;
}

export type ChildEvent =
  | {
      kind: "identity";
      identity: RuntimeIdentity;
      /** Malformed agent entries dropped from the event; counted like dropped lines. */
      droppedAgents: number;
    }
  | { kind: "beat"; beat: BeatEvent }
  | { kind: "terminal"; terminal: TerminalEvent }
  | { kind: "ignored" };

const MAX_SEQ = Number.MAX_SAFE_INTEGER;
const MAX_LATENCY_MS = 3_600_000;
const MAX_NEXT_IN_MS = 3_600_000;

class FieldReader {
  constructor(private readonly raw: Record<string, unknown>) {}

  text(key: string, required: boolean): string {
    if (!Object.hasOwn(this.raw, key)) {
      if (required) throw new ChildLineError("malformed", key);
      return "";
    }
    const v = this.raw[key];
    if (typeof v !== "string") throw new ChildLineError("malformed", key);
    return v;
  }

  id(key: string, required: boolean): string {
    const v = this.text(key, required);
    if ((v !== "" || required) && !ID_RE.test(v))
      throw new ChildLineError("malformed", key);
    return v;
  }

  timestamp(key: string): string {
    const v = this.text(key, true);
    if (!TIMESTAMP_RE.test(v)) throw new ChildLineError("malformed", key);
    return v;
  }

  integer(key: string, required: boolean, lo: number, hi: number): number {
    if (!Object.hasOwn(this.raw, key)) {
      if (required) throw new ChildLineError("malformed", key);
      return 0;
    }
    const v = this.raw[key];
    if (typeof v !== "number" || !Number.isSafeInteger(v) || v < lo || v > hi) {
      throw new ChildLineError("malformed", key);
    }
    return v;
  }
}

/**
 * Fill identity.defaultAgentId and identity.agents from the optional agent_id
 * and agents fields. Older runtimes omit both, so absence is not an error; a
 * malformed agent_id, agents list, or entry is dropped and counted rather than
 * failing the whole identity. When agent_id is absent, the first entry marked
 * is_default supplies it. Returns the number of dropped values.
 */
function parseAgents(
  raw: Record<string, unknown>,
  identity: RuntimeIdentity,
): number {
  let dropped = 0;
  if (Object.hasOwn(raw, "agent_id")) {
    const v = raw.agent_id;
    if (typeof v === "string" && ID_RE.test(v)) identity.defaultAgentId = v;
    else dropped++;
  }
  if (!Object.hasOwn(raw, "agents")) return dropped;
  const entries = raw.agents;
  if (!Array.isArray(entries)) return dropped + 1;
  for (const entry of entries) {
    const agent = parseAgentEntry(entry);
    if (!agent) {
      dropped++;
      continue;
    }
    identity.agents.push(agent);
    if (identity.defaultAgentId === undefined && agent.isDefault) {
      identity.defaultAgentId = agent.agentId;
    }
  }
  return dropped;
}

/** Read one {agent_id, is_default} entry; is_default is false when absent. */
function parseAgentEntry(entry: unknown): AssignedAgent | undefined {
  if (entry === null || typeof entry !== "object" || Array.isArray(entry)) {
    return undefined;
  }
  const e = entry as Record<string, unknown>;
  const agentId = e.agent_id;
  if (typeof agentId !== "string" || !ID_RE.test(agentId)) return undefined;
  let isDefault = false;
  if (Object.hasOwn(e, "is_default")) {
    if (typeof e.is_default !== "boolean") return undefined;
    isDefault = e.is_default;
  }
  return { agentId, isDefault };
}

const encoder = new TextEncoder();
const decoder = new TextDecoder();

/**
 * Parse one line of child stdout (a trailing "\n" / "\r\n" is ignored). Only
 * allowlisted fields survive; payloads, results, reasoning, tokens, or any
 * other key the runtime might emit are dropped. Throws ChildLineError for
 * lines the SDK must drop and count.
 */
export function parseChildLine(line: string | Uint8Array): ChildEvent {
  let text = typeof line === "string" ? line : decoder.decode(line);
  if (text.endsWith("\n")) text = text.slice(0, -1);
  if (text.endsWith("\r")) text = text.slice(0, -1);
  if (encoder.encode(text).length > MAX_CHILD_LINE_BYTES) {
    throw new ChildLineError("line_too_long", "line");
  }
  let raw: unknown;
  try {
    raw = JSON.parse(text);
  } catch {
    throw new ChildLineError("malformed", "not json");
  }
  if (raw === null || typeof raw !== "object" || Array.isArray(raw)) {
    throw new ChildLineError("malformed", "not an object");
  }
  const obj = raw as Record<string, unknown>;
  if (obj.v !== RUNTIME_LINK_CONTRACT_VERSION) {
    throw new ChildLineError("contract_mismatch", "v");
  }
  if (typeof obj.event !== "string")
    throw new ChildLineError("malformed", "event");

  const f = new FieldReader(obj);
  switch (obj.event) {
    case "identity": {
      const identity: RuntimeIdentity = {
        runId: f.id("run_id", true),
        installationId: f.id("installation_id", true),
        orgId: f.id("org_id", true),
        workspaceId: f.id("workspace_id", false),
        platform: f.id("platform", false),
        status: f.id("status", false),
        bindingStatus: f.id("binding_status", false),
        runtimeVersion: f.id("runtime_version", false),
        agents: [],
      };
      const droppedAgents = parseAgents(obj, identity);
      return { kind: "identity", identity, droppedAgents };
    }
    case "beat": {
      const runId = f.id("run_id", true);
      const seq = f.integer("seq", false, 0, MAX_SEQ);
      const at = f.timestamp("at");
      const outcome = f.text("outcome", true);
      const latencyMs = f.integer("latency_ms", false, 0, MAX_LATENCY_MS);
      const nextInMs = f.integer("next_in_ms", false, 0, MAX_NEXT_IN_MS);
      const httpStatus = f.integer("http_status", false, 0, 599);
      if (httpStatus > 0 && httpStatus < 100)
        throw new ChildLineError("malformed", "http_status");
      if (!isOneOf(BEAT_OUTCOMES, outcome))
        throw new ChildLineError("contract_mismatch", "outcome");
      return {
        kind: "beat",
        beat: { runId, seq, at, outcome, httpStatus, latencyMs, nextInMs },
      };
    }
    case "terminal": {
      const runId = f.id("run_id", true);
      const reason = f.text("reason", true);
      if (!REASON_RE.test(reason))
        throw new ChildLineError("malformed", "reason");
      return { kind: "terminal", terminal: { runId, reason } };
    }
    default:
      return { kind: "ignored" };
  }
}

// ---------------------------------------------------------------------------
// Redacted lifecycle events (spec §5.3)
// ---------------------------------------------------------------------------

/** A lifecycle event has a value outside its closed set or bounds. */
export class InvalidLinkEventError extends Error {
  constructor(detail: string) {
    super(`runtimeLink: invalid lifecycle event: ${detail}`);
    this.name = "InvalidLinkEventError";
  }
}

export interface EventHarness {
  kind: HarnessKind;
  version: string;
  deploymentEnv: string;
  sdkLang: SdkLang;
  sdkVersion: string;
}

/**
 * A redacted lifecycle record. Redacted by construction: the type has no
 * field that could carry a token, argv, env, child stderr, provider payload
 * or result, reasoning, customer content, or a subject.
 */
export interface LinkEvent {
  name: LifecycleEventName;
  at: Date;
  state: LinkState;
  /** Empty when there is no failure. */
  reason: FailureClass | "";
  runId: string;
  seq: number;
  sdkInstanceId: string;
  harness: EventHarness;
  restarts: number;
  consecutiveFails: number;
}

/** The serialized form: exactly these keys, in this order. */
export interface LinkEventWire {
  name: LifecycleEventName;
  at: string;
  state: LinkState;
  reason: FailureClass | "";
  run_id: string;
  seq: number;
  sdk_instance_id: string;
  harness: {
    kind: HarnessKind;
    version: string;
    deployment_env: string;
    sdk_lang: SdkLang;
    sdk_version: string;
  };
  restarts: number;
  consecutive_fails: number;
}

function isCount(v: unknown): v is number {
  return typeof v === "number" && Number.isSafeInteger(v) && v >= 0;
}

/** Throws InvalidLinkEventError unless every closed set and bound holds. */
export function validateLinkEvent(ev: LinkEvent): LinkEvent {
  const h = ev.harness;
  const ok =
    isOneOf(LIFECYCLE_EVENT_NAMES, ev.name) &&
    isOneOf(LINK_STATES, ev.state) &&
    (ev.reason === "" || isOneOf(FAILURE_CLASSES, ev.reason)) &&
    ev.at instanceof Date &&
    !Number.isNaN(ev.at.getTime()) &&
    (ev.runId === "" || ID_RE.test(ev.runId)) &&
    ID_RE.test(ev.sdkInstanceId) &&
    isOneOf(HARNESS_KINDS, h.kind) &&
    isOneOf(SDK_LANGS, h.sdkLang) &&
    (h.version === "" || VERSION_RE.test(h.version)) &&
    (h.deploymentEnv === "" || ENV_NAME_RE.test(h.deploymentEnv)) &&
    (h.sdkVersion === "" || VERSION_RE.test(h.sdkVersion)) &&
    isCount(ev.seq) &&
    isCount(ev.restarts) &&
    isCount(ev.consecutiveFails);
  if (!ok) throw new InvalidLinkEventError("value outside contract");
  return ev;
}

function str(v: unknown, fallback = ""): string {
  if (v === undefined) return fallback;
  if (typeof v !== "string") throw new InvalidLinkEventError("expected string");
  return v;
}

function parseEventTimestamp(v: unknown): Date {
  const m = typeof v === "string" ? TIMESTAMP_RE.exec(v) : null;
  if (!m) throw new InvalidLinkEventError("at");
  const fraction = (m[2] ?? ".000").slice(0, 4);
  return new Date(`${m[1]}${fraction}${m[3]}`);
}

/** Build an event from allowlisted wire keys only; everything else is dropped. */
export function linkEventFromWire(raw: unknown): LinkEvent {
  if (raw === null || typeof raw !== "object" || Array.isArray(raw)) {
    throw new InvalidLinkEventError("not an object");
  }
  const o = raw as Record<string, unknown>;
  const hRaw = o.harness ?? {};
  if (hRaw === null || typeof hRaw !== "object" || Array.isArray(hRaw)) {
    throw new InvalidLinkEventError("harness");
  }
  const h = hRaw as Record<string, unknown>;
  return validateLinkEvent({
    name: str(o.name) as LifecycleEventName,
    at: parseEventTimestamp(o.at),
    state: str(o.state) as LinkState,
    reason: str(o.reason) as FailureClass | "",
    runId: str(o.run_id),
    seq: (o.seq ?? 0) as number,
    sdkInstanceId: str(o.sdk_instance_id),
    harness: {
      kind: str(h.kind) as HarnessKind,
      version: str(h.version),
      deploymentEnv: str(h.deployment_env),
      sdkLang: str(h.sdk_lang) as SdkLang,
      sdkVersion: str(h.sdk_version),
    },
    restarts: (o.restarts ?? 0) as number,
    consecutiveFails: (o.consecutive_fails ?? 0) as number,
  });
}

/** Validate and emit exactly the allowlisted keys; `at` is UTC with millisecond precision. */
export function linkEventToWire(ev: LinkEvent): LinkEventWire {
  validateLinkEvent(ev);
  const h = ev.harness;
  return {
    name: ev.name,
    at: ev.at.toISOString(),
    state: ev.state,
    reason: ev.reason,
    run_id: ev.runId,
    seq: ev.seq,
    sdk_instance_id: ev.sdkInstanceId,
    harness: {
      kind: h.kind,
      version: h.version,
      deployment_env: h.deploymentEnv,
      sdk_lang: h.sdkLang,
      sdk_version: h.sdkVersion,
    },
    restarts: ev.restarts,
    consecutive_fails: ev.consecutiveFails,
  };
}

// ---------------------------------------------------------------------------
// RuntimeLink interface (implementation arrives in a later slice)
// ---------------------------------------------------------------------------

/**
 * Supervises the harness→runtime heartbeat. `start` returns promptly and
 * rejects only for invalid config, never for network or runtime state.
 * Aborting the signal passed to `start`, or calling `stop`, ends the link;
 * `stop` is idempotent and bounded by `stopTimeoutMs`.
 */
export interface RuntimeLink {
  start(signal?: AbortSignal): Promise<void>;
  stop(): Promise<void>;
  status(): LinkStatus;
  identity(): RuntimeIdentity | undefined;
  events(): AsyncIterable<LinkEvent>;
}
