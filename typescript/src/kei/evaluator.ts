/**
 * Policy evaluation backed by `kei-proxy authorize`.
 *
 * TypeScript port of `pedro_agentware.kei.evaluator`. All three SDK languages
 * are held to the parity table in `fixtures/kei/authorize-cases.v1.json`
 * (see `docs/kei-proxy-evaluator-parity.md`): the same proxy output yields the
 * same Decision, reason class and enrollment everywhere.
 *
 * Fail closed: only an explicit `allow`/`permit` with exit 0 allows. This
 * module never spawns the proxy: `KeiProxyAuthorizeClient`
 * (`authorizeClient.ts`) does, behind the `KeiProxyAuthorizationClient` seam.
 *
 * `evaluate` is asynchronous so a harness's event loop keeps running while
 * kei-proxy answers. It therefore does not implement the synchronous
 * `PolicyEvaluator`; await it before running the tool.
 */
import { createHash } from "node:crypto";
import {
  Action,
  type CallerContext,
  type Decision,
} from "../middleware/types.js";

/** kei-proxy's affirmative decisions. Everything else denies. */
export const KEI_PROXY_AFFIRMATIVE_DECISIONS: ReadonlySet<string> = new Set([
  "permit",
  "allow",
]);

/** Every reason reads `kei-proxy <class>` or `kei-proxy <class>: <detail>`. */
export const KEI_PROXY_REASON_CLASSES = [
  "allow",
  "deny",
  "enrollment_required",
  "unknown_decision",
  "no_decision",
  "malformed_response",
  "empty_response",
  "proxy_error",
  "exit_mismatch",
  "proxy_unavailable",
  "proxy_timeout",
  "missing_token",
  "pin_mismatch",
] as const;

export type KeiProxyReasonClass = (typeof KEI_PROXY_REASON_CLASSES)[number];

/**
 * An authorize call that produced no usable answer. The message is built by
 * this library and never contains proxy stdout or stderr.
 */
export class KeiProxyAuthorizeError extends Error {
  readonly reasonClass: KeiProxyReasonClass;
  readonly detail: string;

  constructor(reasonClass: KeiProxyReasonClass, detail: string) {
    super(detail);
    this.name = "KeiProxyAuthorizeError";
    this.reasonClass = reasonClass;
    this.detail = detail;
  }
}

/** The canonical audit context sent with one authorize call. */
export interface KeiProxyAuthorizeRequest {
  userId: string;
  tool: string;
  action: string;
  resource: string;
  spanId: string;
  invokingSubject: string;
  parentSpan: string;
  delegationDepth: number;
  agentId: string;
  agentVersion: string;
  framework: string;
  toolArgsDigest: string;
  resources: string[];
  /** Accepted for parity; kei-proxy derives workspace scope from the runtime token. */
  workspaceId: string;
}

/**
 * The single call the evaluator needs. Return (or resolve to) the parsed
 * authorize object; throw or reject with `KeiProxyAuthorizeError` (or
 * anything) to deny.
 */
export interface KeiProxyAuthorizationClient {
  authorize(request: KeiProxyAuthorizeRequest): unknown;
}

/**
 * Derive `type:kind:id` resource identifiers from a tool call's arguments.
 * Only resources named in the arguments are knowable before the call.
 */
export function keiProxyResourcesTouched(
  toolName: string,
  args: Record<string, unknown>,
): string[] {
  const resourceType = toolName.split(".", 1)[0].split("_", 1)[0].toLowerCase();
  if (!resourceType) return [];
  const touched: string[] = [];
  const add = (kind: string, identifier: unknown): void => {
    if (identifier === undefined || identifier === null) return;
    const value = String(identifier).trim();
    if (!value) return;
    const entry = `${resourceType}:${kind}:${value}`;
    if (!touched.includes(entry)) touched.push(entry);
  };
  const owner = args.owner;
  const repo = args.repo || args.repository;
  if (owner && repo) add("repo", `${String(owner)}/${String(repo)}`);
  else if (repo) add("repo", repo);
  add("issue", args.issue_number);
  add("pull_request", args.pull_number || args.pr_number);
  add("branch", args.branch || args.ref);
  add("file", args.path || args.file_path);
  return touched;
}

/**
 * SHA-256 of the args as compact, sorted-key, ASCII-escaped JSON: byte-for-byte
 * Python's `json.dumps(args, sort_keys=True, separators=(",", ":"))` for
 * strings, integers, booleans, null, arrays and objects.
 */
export function keiProxyToolArgsDigest(args: Record<string, unknown>): string {
  return createHash("sha256").update(canonicalJson(args), "utf8").digest("hex");
}

function canonicalJson(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(canonicalJson).join(",")}]`;
  if (isPlainObject(value)) {
    const keys = Object.keys(value).sort();
    return `{${keys.map((k) => `${asciiJson(k)}:${canonicalJson(value[k])}`).join(",")}}`;
  }
  if (typeof value === "string") return asciiJson(value);
  if (value === undefined) return "null";
  return JSON.stringify(value) ?? "null";
}

function asciiJson(text: string): string {
  return JSON.stringify(text).replace(
    /[\u0080-\uffff]/g,
    (ch) => `\\u${ch.charCodeAt(0).toString(16).padStart(4, "0")}`,
  );
}

function isPlainObject(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

export type KeiProxyLogger = (level: "debug" | "warn", message: string) => void;

export interface KeiProxyEvaluatorOptions {
  /** Action verb sent when a tool call names none in `args.action`. Default `execute`. */
  defaultAction?: string;
  /** Receives class-level diagnostics only. Default: `console.warn` for warnings. */
  logger?: KeiProxyLogger;
}

const defaultLogger: KeiProxyLogger = (level, message) => {
  if (level === "warn") console.warn(message);
};

/**
 * A policy evaluator over kei-proxy. Translates the proxy's decision into an
 * agentware Decision and fails closed on every path that is not an explicit
 * affirmative. See `docs/kei-proxy-evaluator-parity.md` for the table.
 */
export class KeiProxyEvaluator {
  static readonly RULE = "kei-proxy";

  private readonly client: KeiProxyAuthorizationClient;
  private readonly defaultAction: string;
  private readonly logger: KeiProxyLogger;

  constructor(
    client: KeiProxyAuthorizationClient,
    options: KeiProxyEvaluatorOptions = {},
  ) {
    this.client = client;
    this.defaultAction = options.defaultAction ?? "execute";
    this.logger = options.logger ?? defaultLogger;
  }

  async evaluate(
    toolName: string,
    args: Record<string, unknown>,
    caller: CallerContext,
  ): Promise<Decision> {
    const resources = keiProxyResourcesTouched(toolName, args);
    const userId = caller.invoking_subject || caller.user_id || "";
    const meta = caller.metadata ?? {};
    const request: KeiProxyAuthorizeRequest = {
      userId,
      tool: toolName,
      action: String(args.action || this.defaultAction),
      resource: resources[0] ?? toolName,
      spanId: meta.span_id ?? "",
      invokingSubject: userId,
      parentSpan: caller.parent_span ?? "",
      delegationDepth: caller.delegation_depth ?? 0,
      agentId: meta.agent_id ?? "",
      agentVersion: meta.agent_version ?? "",
      framework: meta.framework || caller.source || "",
      toolArgsDigest: keiProxyToolArgsDigest(args),
      resources,
      workspaceId: meta.workspace_id ?? "",
    };

    let raw: unknown;
    try {
      raw = await this.client.authorize(request);
    } catch (err) {
      if (err instanceof KeiProxyAuthorizeError) {
        this.logger(
          "warn",
          `kei-proxy authorize for ${toolName} denied: ${err.reasonClass}`,
        );
        return this.decision(Action.DENY, reason(err.reasonClass, err.detail));
      }
      const message = err instanceof Error ? err.message : String(err);
      this.logger(
        "warn",
        `kei-proxy authorize failed for ${toolName}, denying: ${message}`,
      );
      return this.decision(
        Action.DENY,
        reason("proxy_error", `authorize failed: ${message}`),
      );
    }

    const response = isPlainObject(raw) ? raw : {};
    const rawDecision = response.decision;
    const decision =
      rawDecision === undefined || rawDecision === null
        ? ""
        : String(rawDecision).trim().toLowerCase();
    const proxyReason = optionalString(response.reason);
    const policyId =
      optionalString(response.policy_id) ?? optionalString(response.policy);

    if (KEI_PROXY_AFFIRMATIVE_DECISIONS.has(decision)) {
      return this.decision(
        Action.ALLOW,
        reason("allow", proxyReason),
        policyId,
      );
    }

    let reasonClass: KeiProxyReasonClass;
    let detail: string | undefined;
    let enrollment: Record<string, unknown> | undefined;
    const carried = isPlainObject(response.enrollment)
      ? response.enrollment
      : undefined;
    if (!decision) {
      reasonClass = "no_decision";
    } else if (decision === "deny") {
      reasonClass = "deny";
      detail = proxyReason;
      enrollment = carried;
    } else if (decision === "enrollment_required") {
      reasonClass = "enrollment_required";
      detail = proxyReason ?? "enrollment is required before this call";
      enrollment = carried;
    } else {
      reasonClass = "unknown_decision";
      detail = `'${decision}'` + (proxyReason ? ` (${proxyReason})` : "");
    }

    this.logger("debug", `kei-proxy ${reasonClass} for ${toolName}`);
    return this.decision(
      Action.DENY,
      reason(reasonClass, detail),
      policyId,
      enrollment,
    );
  }

  private decision(
    action: Action,
    reasonText: string,
    policyId?: string,
    enrollment?: Record<string, unknown>,
  ): Decision {
    const out: Decision = {
      action,
      rule: policyId ?? KeiProxyEvaluator.RULE,
      reason: reasonText,
      timestamp: new Date(),
    };
    if (enrollment !== undefined) out.enrollment = enrollment;
    return out;
  }
}

function reason(reasonClass: KeiProxyReasonClass, detail?: string): string {
  return detail
    ? `kei-proxy ${reasonClass}: ${detail}`
    : `kei-proxy ${reasonClass}`;
}

function optionalString(value: unknown): string | undefined {
  if (value === undefined || value === null) return undefined;
  const text = String(value);
  return text ? text : undefined;
}
