import type { AnyTool, GovernedTool, KeiResourceType } from "./tool.js";

export type KeiToolRoute =
  | { connector_binding: { agent_id: string; connector_id: string } }
  | { harness_executor: { executor: string; registration: string } };

/** Trusted metadata attached to the same registry entry used for dispatch. */
export interface KeiToolRegistration {
  service: string;
  source: string;
  operation_class: "read" | "write";
  route: KeiToolRoute;
  required_capabilities?: string[];
  resource_types?: KeiResourceType[];
}

export interface KeiToolManifestEntry {
  name: string;
  source: string;
  required_capabilities: string[];
  resource_types: KeiResourceType[];
  operation_class: "read" | "write";
  service?: string;
  description: string;
  enabled: boolean;
}

export interface KeiToolManifest {
  schema: "kei.tool-manifest/v2";
  tools: KeiToolManifestEntry[];
}

export interface KeiToolManifestV3 {
  schema: "kei.tool-manifest/v3";
  tools: Array<{
    name: string;
    service: string;
    source: string;
    operation_class: "read" | "write";
    route: KeiToolRoute;
    required_capabilities?: string[];
    resource_types?: KeiResourceType[];
    description: string;
    enabled: boolean;
  }>;
}

export interface KeiToolManifestV1 {
  tools: Array<{
    name: string;
    service: string;
    description: string;
    action: string;
    resources: string[];
    enabled: boolean;
  }>;
}

export class ToolRegistry {
  private tools: Map<string, AnyTool> = new Map();
  private registrations: Map<string, KeiToolRegistration> = new Map();

  register(tool: AnyTool, registration?: KeiToolRegistration): void {
    this.tools.set(tool.name, tool);
    if (registration) this.registrations.set(tool.name, registration);
    else this.registrations.delete(tool.name);
  }

  get(name: string): AnyTool | undefined {
    return this.tools.get(name);
  }

  all(): AnyTool[] {
    return Array.from(this.tools.values()).sort((a, b) => a.name.localeCompare(b.name));
  }

  names(): string[] {
    return Array.from(this.tools.keys()).sort();
  }

  schemas(): Record<string, Record<string, unknown>> {
    const schemas: Record<string, Record<string, unknown>> = {};
    for (const [name, tool] of this.tools) {
      if ("inputSchema" in tool) {
        schemas[name] = (tool as unknown as { inputSchema(): Record<string, unknown> }).inputSchema();
      }
    }
    return schemas;
  }

  exportKeiToolManifest(version: 1): KeiToolManifestV1;
  exportKeiToolManifest(version: 3): KeiToolManifestV3;
  exportKeiToolManifest(version?: 2): KeiToolManifest;
  exportKeiToolManifest(version: 1 | 2 | 3 = 2): KeiToolManifest | KeiToolManifestV1 | KeiToolManifestV3 {
    if (version === 3) return this.exportV3();
    const names = [...this.tools.keys()].filter((name) => this.isGoverned(this.tools.get(name)!)).sort();
    if (version === 1) {
      return { tools: names.map((name) => {
        const tool = this.tools.get(name) as GovernedTool;
        const scope = tool.keiScope();
        return { name, service: scope.service ?? scope.source, description: tool.description,
          action: scope.operation_class, resources: scope.resource_types.map((item) => item.type).sort(), enabled: true };
      }) };
    }
    const tools: KeiToolManifestEntry[] = names.map((name) => {
      const tool = this.tools.get(name) as GovernedTool;
      const scope = tool.keiScope();
      return { name, source: scope.source, required_capabilities: [...scope.required_capabilities].sort(),
        resource_types: [...scope.resource_types].sort((a, b) => a.type.localeCompare(b.type) || (a.parent_type ?? "").localeCompare(b.parent_type ?? "")),
        operation_class: scope.operation_class, ...(scope.service ? { service: scope.service } : {}),
        description: tool.description, enabled: true };
    });
    return { schema: "kei.tool-manifest/v2", tools };
  }

  private exportV3(): KeiToolManifestV3 {
    const tools = [...this.registrations.keys()].sort().map((name) => {
      const registration = this.registrations.get(name)!;
      this.validateRegistration(registration);
      const tool = this.tools.get(name)!;
      const common = { name, service: registration.service, source: registration.source,
        operation_class: registration.operation_class, route: registration.route, description: tool.description, enabled: true };
      if ("connector_binding" in registration.route) return {
        ...common,
        required_capabilities: [...registration.required_capabilities!].sort(),
        ...((registration.resource_types?.length ?? 0) > 0 ? { resource_types: [...registration.resource_types!].sort((a, b) => a.type.localeCompare(b.type) || (a.parent_type ?? "").localeCompare(b.parent_type ?? "")) } : {}),
      };
      return common;
    });
    return { schema: "kei.tool-manifest/v3", tools };
  }

  private validateRegistration(value: KeiToolRegistration): void {
    if (!value.service?.trim() || !value.source?.trim()) throw new Error("v3 registration requires non-empty service and source");
    if (value.operation_class !== "read" && value.operation_class !== "write") throw new Error("invalid operation_class");
    if ("connector_binding" in value.route) {
      if (!value.route.connector_binding.agent_id?.trim() || !value.route.connector_binding.connector_id?.trim() || !value.required_capabilities?.length || value.required_capabilities.some((v) => !v.trim())) throw new Error("connector route requires binding and non-empty capabilities");
    } else if (!value.route.harness_executor.executor?.trim() || !value.route.harness_executor.registration?.trim() || value.required_capabilities !== undefined || value.resource_types !== undefined) {
      throw new Error("harness route requires identity and omits connector-only fields");
    }
  }

  private isGoverned(tool: AnyTool): tool is GovernedTool {
    return "keiScope" in tool && typeof (tool as unknown as GovernedTool).keiScope === "function";
  }

  clear(): void {
    this.tools.clear();
    this.registrations.clear();
  }
}
