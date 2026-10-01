import type { AnyTool, GovernedTool, KeiResourceType } from "./tool.js";

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

  register(tool: AnyTool): void {
    this.tools.set(tool.name, tool);
  }

  get(name: string): AnyTool | undefined {
    return this.tools.get(name);
  }

  all(): AnyTool[] {
    return Array.from(this.tools.values()).sort((a, b) =>
      a.name.localeCompare(b.name),
    );
  }

  names(): string[] {
    return Array.from(this.tools.keys()).sort();
  }

  schemas(): Record<string, Record<string, unknown>> {
    const schemas: Record<string, Record<string, unknown>> = {};
    for (const [name, tool] of this.tools) {
      if ("inputSchema" in tool) {
        schemas[name] = (
          tool as unknown as { inputSchema(): Record<string, unknown> }
        ).inputSchema();
      }
    }
    return schemas;
  }

  exportKeiToolManifest(version: 1): KeiToolManifestV1;
  exportKeiToolManifest(version?: 2): KeiToolManifest;
  exportKeiToolManifest(
    version: 1 | 2 = 2,
  ): KeiToolManifest | KeiToolManifestV1 {
    const names: string[] = [];
    for (const [name, tool] of this.tools) {
      if (this.isGoverned(tool)) {
        names.push(name);
      }
    }
    names.sort();
    if (version === 1) {
      return {
        tools: names.map((name) => {
          const tool = this.tools.get(name) as GovernedTool;
          const scope = tool.keiScope();
          return {
            name,
            service: scope.service ?? scope.source,
            description: tool.description,
            action: scope.operation_class,
            resources: scope.resource_types.map((item) => item.type).sort(),
            enabled: true,
          };
        }),
      };
    }
    const tools: KeiToolManifestEntry[] = names.map((name) => {
      const tool = this.tools.get(name) as GovernedTool;
      const scope = tool.keiScope();
      return {
        name,
        source: scope.source,
        required_capabilities: [...scope.required_capabilities].sort(),
        resource_types: [...scope.resource_types].sort((a, b) => {
          if (a.type !== b.type) return a.type < b.type ? -1 : 1;
          const leftParent = a.parent_type ?? "";
          const rightParent = b.parent_type ?? "";
          return leftParent === rightParent
            ? 0
            : leftParent < rightParent
              ? -1
              : 1;
        }),
        operation_class: scope.operation_class,
        ...(scope.service ? { service: scope.service } : {}),
        description: tool.description,
        enabled: true,
      };
    });
    return { schema: "kei.tool-manifest/v2", tools };
  }

  private isGoverned(tool: AnyTool): tool is GovernedTool {
    return (
      "keiScope" in tool &&
      typeof (tool as unknown as GovernedTool).keiScope === "function"
    );
  }

  clear(): void {
    this.tools.clear();
  }
}
