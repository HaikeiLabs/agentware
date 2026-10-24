import type { AnyTool, KeiResourceType } from "./tool.js";

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

  exportKeiToolManifest(version: 3 = 3): KeiToolManifestV3 {
    if (version !== 3) throw new Error("only manifest v3 is supported");
    return this.exportV3();
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

  clear(): void {
    this.tools.clear();
    this.registrations.clear();
  }
}
