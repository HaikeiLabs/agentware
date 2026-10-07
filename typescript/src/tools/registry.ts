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
  plan?: KeiToolPlan;
}

export interface KeiValueRef { from: "args" | "context"; pointer?: string; field?: string }
export interface KeiToolPlan { context_schema: Record<string, unknown>; operations: KeiToolOperation[] }
export interface KeiToolOperation { id: string; capability: string; resource: { type: string; id: KeiValueRef; parent?: { type: string; id: KeiValueRef } }; provider_resource_template: string; provider_input: unknown }
export interface KeiToolManifestV4 { schema: "kei.tool-manifest/v4"; tools: Array<Record<string, unknown>> }


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
  exportKeiToolManifest(version: 4): KeiToolManifestV4;
  exportKeiToolManifest(version?: 2): KeiToolManifest;
  exportKeiToolManifest(version: 1 | 2 | 3 | 4 = 2): KeiToolManifest | KeiToolManifestV1 | KeiToolManifestV3 | KeiToolManifestV4 {
    if (version === 4) return this.exportV4();
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

  private exportV4(): KeiToolManifestV4 {
    const tools: Array<Record<string, unknown>> = [];
    for (const name of [...this.registrations.keys()].sort()) {
      const r = this.registrations.get(name)!; this.validateRegistration(r);
      const base: Record<string, unknown> = { name, service: r.service, source: r.source, operation_class: r.operation_class, route: r.route, description: this.tools.get(name)!.description, enabled: true };
      if ("harness_executor" in r.route) {
        if (r.plan !== undefined || r.required_capabilities !== undefined || r.resource_types !== undefined) throw new Error("harness route omits connector plan and fields");
      } else {
        const caps = r.required_capabilities ?? [], resources = r.resource_types ?? [], plan = r.plan;
        const tool = this.tools.get(name)!;
        if (!r.route.connector_binding.agent_id.trim() || !r.route.connector_binding.connector_id.trim() || !caps.length || !plan || !("inputSchema" in tool)) throw new Error("v4 connector requires binding, capabilities, plan and ExtendedTool input schema");
        const args = (tool as unknown as {inputSchema(): Record<string, unknown>}).inputSchema();
        this.closedSchema(args); this.closedSchema(plan.context_schema); this.validatePlan(plan, args, caps, resources);
        base.required_capabilities = [...caps].sort(); if (resources.length) base.resource_types = [...resources].sort((a,b)=>a.type.localeCompare(b.type)||(a.parent_type??"").localeCompare(b.parent_type??""));
        base.plan = { args_schema: args, ...plan };
      }
      tools.push(base);
    }
    return { schema: "kei.tool-manifest/v4", tools };
  }

  private closedSchema(s: unknown): asserts s is Record<string, unknown> {
    if (!s || typeof s !== "object" || (s as any).type !== "object" || (s as any).additionalProperties !== false || !("properties" in s) || typeof (s as any).properties !== "object") throw new Error("must be a closed object schema");
  }

  private validatePlan(plan: KeiToolPlan, args: Record<string, unknown>, caps: string[], resources: KeiResourceType[]): void {
    if (!Array.isArray(plan.operations) || plan.operations.length < 1 || plan.operations.length > 32) throw new Error("operations must contain 1..32 entries");
    const props = (args.properties ?? {}) as Record<string, unknown>, context = (plan.context_schema.properties ?? {}) as Record<string, unknown>;
    const checkRef = (v: KeiValueRef): void => {
      if (v.from === "args" ? !!v.field || !v.pointer || !/^\/[^/]+$/.test(v.pointer) || !(v.pointer.slice(1) in props) : v.from === "context" ? !!v.pointer || !v.field || !(v.field in context) : true) throw new Error("invalid typed ref");
    };
    const ids = new Set<string>(), used = new Set<string>();
    const checkTemplate = (v: unknown, d=0, n={value:0}): void => { if (++n.value > 256 || d > 16) throw new Error("provider_input bounds exceeded"); if (Array.isArray(v)) v.forEach(x=>checkTemplate(x,d+1,n)); else if (v && typeof v === "object") { const o=v as any; if ("from" in o) checkRef(o); else Object.values(o).forEach(x=>checkTemplate(x,d+1,n)); } else if (v !== null && !["string","number","boolean"].includes(typeof v)) throw new Error("unsupported provider_input value"); };
    for (const op of plan.operations) {
      if (!op.id || ids.has(op.id) || !caps.includes(op.capability)) throw new Error("invalid operation id/capability"); ids.add(op.id); used.add(op.capability);
      if (!resources.some(x=>x.type===op.resource.type && (x.parent_type??undefined)===(op.resource.parent?.type))) throw new Error("resource/parent pair is not declared");
      checkRef(op.resource.id); if (op.resource.parent) checkRef(op.resource.parent.id);
      const t=op.provider_resource_template; if (!t.includes("{resource.id}") || t.replaceAll("{resource.id}","").replaceAll("{parent.id}","").includes("{" ) || t.includes("{parent.id}") !== !!op.resource.parent) throw new Error("invalid provider resource template");
      checkTemplate(op.provider_input);
    }
    if (used.size !== new Set(caps).size || caps.some(c=>!used.has(c))) throw new Error("operation capability set must exactly cover registered capabilities");
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
