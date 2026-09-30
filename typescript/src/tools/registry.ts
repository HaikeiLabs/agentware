import type { AnyTool, GovernedTool } from "./tool.js";

export interface KeiToolManifestEntry {
  name: string;
  service: string;
  description: string;
  action: string;
  resources: string[];
  enabled: boolean;
}

export interface KeiToolManifest {
  tools: KeiToolManifestEntry[];
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
      a.name.localeCompare(b.name)
    );
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

  exportKeiToolManifest(): KeiToolManifest {
    const names: string[] = [];
    for (const [name, tool] of this.tools) {
      if (this.isGoverned(tool)) {
        names.push(name);
      }
    }
    names.sort();
    const tools: KeiToolManifestEntry[] = names.map((name) => {
      const tool = this.tools.get(name) as GovernedTool;
      const scope = tool.keiScope();
      return {
        name,
        service: scope.service,
        description: tool.description,
        action: scope.action,
        resources: [...scope.resources],
        enabled: true,
      };
    });
    return { tools };
  }

  private isGoverned(tool: AnyTool): tool is GovernedTool {
    return "keiScope" in tool && typeof (tool as unknown as GovernedTool).keiScope === "function";
  }

  clear(): void {
    this.tools.clear();
  }
}