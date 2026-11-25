import { getOption, hasOption, type DescMessage } from "@bufbuild/protobuf";
import { operation } from "@haikeilabs/connector-contracts/kei/contracts/v1/options_pb";
import type {
  HTTP,
  Op,
} from "@haikeilabs/connector-contracts/kei/connection/index";
import type {
  KeiProxySocketSession,
  KeiProxySocketAuthorizeRequest,
  KeiProxySocketCredentialReleaseRequest,
} from "../kei/socketAuthorizeClient.js";
import type { CallerContext } from "../middleware/types.js";
import {
  assertSupportedConnection,
  buildConnection,
  UnsupportedConnection,
  type ConnectionKind,
} from "./connections.js";

/** The socket methods needed by the operation runner. */
export interface OperationSocketClient {
  authorize(
    session: KeiProxySocketSession,
    input: KeiProxySocketAuthorizeRequest,
  ): Promise<Record<string, unknown>>;
  releaseCredential(
    session: KeiProxySocketSession,
    input: KeiProxySocketCredentialReleaseRequest,
  ): Promise<Record<string, unknown>>;
}
/** Binds a generated request descriptor to its generated queries constructor. */
export interface OperationBinding {
  requestSchema: DescMessage;
  queries: (connection: HTTP) => object;
  /** Version of the installed tool binding used for authorization. */
  bindingVersion?: string;
}
/** Installed connector routing metadata, indexed by the contract connector ID. */
export type ConnectorConfig = Record<
  string,
  {
    connectorId: string;
    connectionName: string;
    kind: ConnectionKind;
    baseURL: string;
    headerName?: string;
    operations: Record<string, OperationBinding>;
  }
>;
/** An audit record contains metadata only. */
export interface OperationAudit {
  operationId: string;
  decision: "allow" | "deny";
  outcome: "success" | "denied" | "error";
}
export type OperationAuditSink = (record: OperationAudit) => void;

/** A policy or credential release was not explicitly allowed. */
export class OperationDenied extends Error {
  constructor(
    readonly reasonCode: string,
    readonly decision = "deny",
  ) {
    super(`Operation denied (${reasonCode})`);
    this.name = "OperationDenied";
  }
}
/** Enrollment details are passed back to the caller without logging. */
export class EnrollmentRequired extends Error {
  constructor(readonly enrollment: unknown) {
    super("Enrollment required");
    this.name = "EnrollmentRequired";
  }
}
/** Connection details are passed back to the caller without logging. */
export class ConnectRequired extends Error {
  constructor(readonly connect: unknown) {
    super("Connection required");
    this.name = "ConnectRequired";
  }
}

/** Resolve operation metadata: generated Op constant fields win, the request descriptor's options remain the fallback. */
export function operationMeta(op: Op, requestSchema: DescMessage) {
  const meta = hasOption(requestSchema, operation)
    ? getOption(requestSchema, operation)
    : undefined;
  if (meta && meta.id !== op.id) throw new OperationDenied("invalid_operation");
  const connector = op.id.split(".")[0];
  const resourceTemplate =
    typeof op.resourceTemplate === "string" && op.resourceTemplate
      ? op.resourceTemplate
      : meta?.resourceTemplate;
  const methodName =
    typeof op.methodName === "string" && op.methodName
      ? op.methodName
      : meta?.methodName;
  const connections =
    Array.isArray(op.connections) && op.connections.length > 0
      ? op.connections
      : meta?.connections;
  if (!connector || !resourceTemplate || !methodName || !connections)
    throw new OperationDenied("invalid_operation");
  return { id: op.id, connector, resourceTemplate, methodName, connections };
}

function renderResource(
  template: string,
  schema: DescMessage,
  request: unknown,
): string {
  if (
    !request ||
    typeof request !== "object" ||
    !template ||
    /[{}]/.test(template.replace(/\{[a-zA-Z_][a-zA-Z0-9_]*\}/g, ""))
  )
    throw new OperationDenied("invalid_resource");
  return template.replace(
    /\{([a-zA-Z_][a-zA-Z0-9_]*)\}/g,
    (_, name: string) => {
      const field = schema.fields.find(
        (f) => f.name === name || f.localName === name,
      );
      const value =
        field && (request as Record<string, unknown>)[field.localName];
      if (
        typeof value !== "string" &&
        typeof value !== "number" &&
        typeof value !== "bigint"
      )
        throw new OperationDenied("invalid_resource");
      const text = String(value);
      if (!text) throw new OperationDenied("invalid_resource");
      return /^\.+$/.test(text)
        ? "%2E".repeat(text.length)
        : encodeURIComponent(text);
    },
  );
}
function safeReason(value: unknown, fallback: string): string {
  return typeof value === "string" && /^[a-z][a-z0-9_]{0,99}$/.test(value)
    ? value
    : fallback;
}
function checkDecision(response: Record<string, unknown>): void {
  const decision = response.decision;
  if (decision === "allow" || decision === "permit") return;
  if (
    decision === "enrollment_required" ||
    response.reason_code === "enrollment_required" ||
    response.enrollment !== undefined
  )
    throw new EnrollmentRequired(response.enrollment);
  if (
    decision === "connect_required" ||
    response.reason_code === "connect_required" ||
    response.connect !== undefined
  )
    throw new ConnectRequired(response.connect);
  throw new OperationDenied(safeReason(response.reason_code, "denied"));
}

/** Runs one generated operation behind kei-proxy authorization and release. */
export class OperationRunner {
  constructor(
    private readonly client: OperationSocketClient,
    private readonly session: KeiProxySocketSession,
    private readonly connectors: ConnectorConfig,
    private readonly audit?: OperationAuditSink,
  ) {}

  async run<Req, Resp>(
    caller: CallerContext,
    op: Op,
    request: Req,
    opts: {
      /** The registered tool name the call is authorized as. */
      tool?: string;
      resourceTemplateOverride?: string;
      /**
       * The tool call's arguments, exactly as the model sent them. For a
       * connector_binding tool they must satisfy the bundle plan's
       * args_schema: kei-proxy derives the operation from them, denies
       * `preflight_required` without them, and re-derives the plan before
       * releasing the credential. The runner cannot derive them from
       * `request`. They stay on the tenant-local socket and never reach audit.
       */
      toolArgs?: Record<string, unknown>;
      /** The tool call's context (the plan's context_schema). */
      toolContext?: Record<string, unknown>;
    } = {},
  ): Promise<Resp> {
    let decision: OperationAudit["decision"] = "deny";
    let outcome: OperationAudit["outcome"] = "denied";
    try {
      if (!caller || !op || typeof op.id !== "string")
        throw new OperationDenied("invalid_context");
      const config = this.connectors[op.id.split(".")[0]];
      const binding = config?.operations[op.id];
      if (!config || !binding || !config.connectorId || !config.connectionName)
        throw new OperationDenied("connector_not_named");
      const meta = operationMeta(op, binding.requestSchema);
      if (
        !request ||
        typeof request !== "object" ||
        (request as { $typeName?: string }).$typeName !==
          binding.requestSchema.typeName
      )
        throw new OperationDenied("invalid_request");
      if (
        meta.connector !== op.id.split(".")[0] ||
        !meta.connections.includes(config.connectionName)
      )
        throw new OperationDenied("invalid_connection");
      assertSupportedConnection(config.kind);
      const resource = renderResource(
        opts.resourceTemplateOverride ?? meta.resourceTemplate,
        binding.requestSchema,
        request,
      );
      // The release capability is the bare operation id: the runtime's
      // capability pattern (^[a-z0-9_]+(\.[a-z0-9_]+)*$) rejects ':'.
      const capability = op.id;
      const context = {
        parentSpan: caller.parent_span,
        delegationDepth: caller.delegation_depth,
      };
      // Sent only when given: the daemon rejects tool_args on a tool that is
      // not a registered connector_binding.
      const planInputs = {
        ...(opts.toolArgs === undefined ? {} : { toolArgs: opts.toolArgs }),
        ...(opts.toolContext === undefined ? {} : { toolContext: opts.toolContext }),
      };
      let result: Record<string, unknown>;
      try {
        result = await this.client.authorize(this.session, {
          tool: opts.tool ?? "",
          resource,
          ...(binding.bindingVersion === undefined ? {} : { bindingVersion: binding.bindingVersion }),
          ...context,
          ...planInputs,
        });
      } catch {
        throw new OperationDenied("proxy_unavailable");
      }
      checkDecision(result);
      try {
        result = await this.client.releaseCredential(this.session, {
          connectorId: config.connectorId,
          capability,
          resource,
          tool: opts.tool,
          ...context,
          ...planInputs,
        });
      } catch {
        throw new OperationDenied("credential_release_failed");
      }
      checkDecision(result);
      const credential = result.credential;
      if (!credential || typeof credential !== "object")
        throw new OperationDenied("credential_release_failed");
      const token = (credential as Record<string, unknown>).access_token;
      if (typeof token !== "string" || !token)
        throw new OperationDenied("credential_release_failed");
      decision = "allow";
      outcome = "error";
      const conn = buildConnection(
        config.kind,
        config.baseURL,
        token,
        config.headerName,
      );
      const queries = binding.queries(conn) as Record<string, unknown>;
      const name = meta.methodName[0].toLowerCase() + meta.methodName.slice(1);
      const method = queries[name];
      if (typeof method !== "function")
        throw new Error("Generated operation method is unavailable");
      const response = (await method.call(queries, request)) as Resp;
      outcome = "success";
      return response;
    } catch (error) {
      if (
        error instanceof OperationDenied ||
        error instanceof EnrollmentRequired ||
        error instanceof ConnectRequired ||
        error instanceof UnsupportedConnection
      )
        throw error;
      throw new Error("Operation execution failed");
    } finally {
      try {
        this.audit?.({
          operationId: typeof op?.id === "string" ? op.id : "",
          decision,
          outcome,
        });
      } catch {
        /* Audit sinks cannot expose execution data or alter results. */
      }
    }
  }
}
