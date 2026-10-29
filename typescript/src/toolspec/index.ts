/** A model tool plus the explicit metadata needed for runtime registration. */
export interface ToolSpec {
  name: string;
  description: string;
  parameters: Record<string, unknown>;
  service?: string;
  source?: string;
  operation_class?: "read" | "write";
  route?: Route;
  required_capabilities?: string[];
  resource_types?: ResourceType[];
  enabled?: boolean;
}

export type Route =
  | { harness_executor: HarnessExecutor }
  | { connector_binding: ConnectorBinding };

export interface HarnessExecutor { executor: string; registration: string }
export interface ConnectorBinding { agent_id: string; connector_id: string }
export type ToolRoute = Route;

export interface ResourceType {
  type: string;
  parent_type?: string;
}
export type ToolResourceType = ResourceType;

/** The runtime v3 manifest schema identifier. */
export const MANIFEST_SCHEMA_V3 = "kei.tool-manifest/v3" as const;

export { ManifestV3Error, loadManifestV3, parseManifestV3 } from "./manifest-v3.js";

export interface ToolManifestV3 {
  schema: "kei.tool-manifest/v3";
  tools: ToolManifestV3Entry[];
}

export interface ToolManifestV3Entry {
  name: string;
  service: string;
  source: string;
  operation_class: "read" | "write";
  route: ToolRoute;
  required_capabilities?: string[];
  resource_types?: ToolResourceType[];
  description: string;
  enabled: boolean;
}

/** Serialize ToolSpecs to the runtime manifest, validating registration metadata. */
export function serializeToolSpecs(specs: readonly ToolSpec[]): ToolManifestV3 {
  return {
    schema: MANIFEST_SCHEMA_V3,
    tools: specs.map(serializeToolSpec),
  };
}

/** Go-parity name for serializing a ToolSpec list to a manifest. */
export const serializeManifestV3 = serializeToolSpecs;

/** Validate and serialize one ToolSpec to a v3 wire entry. */
export function serializeToolSpec(spec: ToolSpec): ToolManifestV3Entry {
  if (!spec.name) throw new Error("name is required");
  if (!spec.description) throw new Error("description is required");
  if (!spec.service?.trim()) throw new Error("service is required");
  if (!spec.source?.trim()) throw new Error("source is required");
  if (!spec.operation_class) {
    throw new Error("operation_class is required");
  }
  if (spec.operation_class !== "read" && spec.operation_class !== "write") {
    throw new Error("operation_class must be read or write");
  }
  if (!spec.route) throw new Error("route is required");
  const routes = Object.keys(spec.route);
  if (routes.length !== 1 || !["connector_binding", "harness_executor"].includes(routes[0])) {
    throw new Error("route must contain exactly one route kind");
  }

  const base: ToolManifestV3Entry = {
    name: spec.name,
    service: spec.service,
    source: spec.source,
    operation_class: spec.operation_class,
    route: spec.route,
    description: spec.description,
    enabled: spec.enabled ?? true,
  };
  if ("connector_binding" in spec.route) {
    const binding = spec.route.connector_binding;
    if (!binding.agent_id.trim() || !binding.connector_id.trim()) {
      throw new Error("connector_binding agent_id and connector_id are required");
    }
    if (!spec.required_capabilities?.length) {
      throw new Error("connector_binding required_capabilities must not be empty");
    }
    base.required_capabilities = spec.required_capabilities;
    if (spec.resource_types?.length) base.resource_types = spec.resource_types;
  } else {
    const harness = spec.route.harness_executor;
    if (!harness.executor.trim() || !harness.registration.trim()) {
      throw new Error("harness_executor executor and registration are required");
    }
    if (spec.required_capabilities?.length || spec.resource_types?.length) {
      throw new Error("harness_executor routes omit connector-only required_capabilities and resource_types");
    }
  }
  return base;
}

/** Convert a ToolSpec to the existing LLM request ToolDefinition shape. */
export function toolSpecToToolDefinition(spec: ToolSpec): {
  name: string;
  description: string;
  input_schema: Record<string, unknown>;
} {
  return { name: spec.name, description: spec.description, input_schema: spec.parameters };
}

export { registerToolSpecs } from "./registrar.js";
