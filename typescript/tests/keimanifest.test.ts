import { ToolRegistry, BaseTool, Result } from "../src/tools/index.js";
import { lintKeiToolManifest } from "../src/tools/lint.js";
import type { GovernedTool, KeiScope } from "../src/tools/index.js";
import { readFileSync } from "fs";
import { join, dirname } from "path";
import { fileURLToPath } from "url";

const __dirname = dirname(fileURLToPath(import.meta.url));

class GovernedGetIssueTool extends BaseTool implements GovernedTool {
  constructor() {
    super("github.get_issue", "Fetch an issue from a GitHub repository");
  }

  execute(_args: Record<string, unknown>): Result {
    return new Result(true);
  }

  keiScope(): KeiScope {
    return {
      source: "github",
      required_capabilities: ["issue.read"],
      resource_types: [{ type: "issue", parent_type: "repository" }],
      operation_class: "read",
      service: "github",
    };
  }
}

class GovernedListIssuesTool extends BaseTool implements GovernedTool {
  constructor() {
    super("github.list_issues", "List issues in a GitHub repository");
  }

  execute(_args: Record<string, unknown>): Result {
    return new Result(true);
  }

  keiScope(): KeiScope {
    return {
      source: "github",
      required_capabilities: ["issue.read"],
      resource_types: [{ type: "issue", parent_type: "repository" }],
      operation_class: "read",
      service: "github",
    };
  }
}

class GovernedLinearTool extends BaseTool implements GovernedTool {
  constructor() {
    super("linear.get_issue", "Fetch an issue from Linear");
  }

  execute(_args: Record<string, unknown>): Result {
    return new Result(true);
  }

  keiScope(): KeiScope {
    return {
      source: "linear",
      required_capabilities: ["issue.read"],
      resource_types: [{ type: "issue", parent_type: "team" }],
      operation_class: "read",
      service: "linear",
    };
  }
}

class GovernedSlackTool extends BaseTool implements GovernedTool {
  constructor() {
    super("slack.post_message", "Post a message to a Slack channel");
  }

  execute(_args: Record<string, unknown>): Result {
    return new Result(true);
  }

  keiScope(): KeiScope {
    return {
      source: "github",
      required_capabilities: ["issue.comment"],
      resource_types: [{ type: "issue", parent_type: "repository" }],
      operation_class: "write",
      service: "slack",
    };
  }
}

class GovernedEmailTool extends BaseTool implements GovernedTool {
  constructor() {
    super("send_email", "Send an email message");
  }

  execute(_args: Record<string, unknown>): Result {
    return new Result(true);
  }

  keiScope(): KeiScope {
    return {
      source: "github",
      required_capabilities: ["issue.create"],
      resource_types: [],
      operation_class: "write",
      service: "email",
    };
  }
}

class V4IssueTool extends BaseTool {
  constructor() { super("github.get_issue", "Fetch issue"); }
  execute(_args: Record<string, unknown>): Result { return new Result(true); }
  inputSchema(): Record<string, unknown> { return { type: "object", properties: { issue_number: { type: "integer", minimum: 1 } }, required: ["issue_number"], additionalProperties: false }; }
  examples(): never[] { return []; }
}

function v4Registry(operation?: any, caps = ["issue.read"], resources: any[] = [{ type: "issue", parent_type: "repository" }]): ToolRegistry {
  const registry = new ToolRegistry();
  registry.register(new V4IssueTool() as any, {
    service: "github", source: "github", operation_class: "read",
    route: { connector_binding: { agent_id: "agent-1", connector_id: "github-1" } },
    required_capabilities: caps, resource_types: resources,
    plan: { context_schema: { type: "object", properties: { repository: { type: "string", minLength: 1, maxLength: 256, pattern: "^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$" } }, required: ["repository"], additionalProperties: false }, operations: [operation ?? { id: "get-issue", capability: "issue.read", resource: { type: "issue", id: { from: "args", pointer: "/issue_number", type: "integer" }, parent: { type: "repository", id: { from: "context", field: "repository", type: "string" } } }, provider_resource_template: "repos/{parent.id}/issues/{resource.id}", provider_input: {} }] },
  });
  return registry;
}

class UngovernedEchoTool extends BaseTool {
  constructor() {
    super("local_echo", "Echo input back");
  }

  execute(_args: Record<string, unknown>): Result {
    return new Result(true);
  }
}

function loadFixture(): unknown {
  const fixturePath = join(
    __dirname,
    "..",
    "..",
    "fixtures",
    "kei",
    "tool-manifest.v2.json",
  );
  return JSON.parse(readFileSync(fixturePath, "utf-8"));
}

describe("KeiToolManifest", () => {
  it("explicitly exports v4 matching the shared fixture", () => {
    const fixture = JSON.parse(readFileSync(join(__dirname, "..", "..", "fixtures", "kei", "tool-manifest.v4.json"), "utf8"));
    expect(v4Registry().exportKeiToolManifest(4)).toEqual(fixture);
  });

  it.each([
    ["typed ref mismatch", (op: any) => { op.resource.id.type = "string"; }],
    ["undeclared context", (op: any) => { op.resource.parent.id.field = "missing"; }],
    ["extra ref key", (op: any) => { op.provider_input = { ref: { from: "args", pointer: "/issue_number", type: "integer", extra: true } }; }],
    ["malformed template", (op: any) => { op.provider_resource_template = "repos/{context.repository}/{resource.id}"; }],
  ])("rejects %s", (_label, mutate) => {
    const operation = { id: "get-issue", capability: "issue.read", resource: { type: "issue", id: { from: "args", pointer: "/issue_number", type: "integer" }, parent: { type: "repository", id: { from: "context", field: "repository", type: "string" } } }, provider_resource_template: "repos/{parent.id}/issues/{resource.id}", provider_input: {} };
    mutate(operation);
    expect(() => v4Registry(operation).exportKeiToolManifest(4)).toThrow();
  });

  it("rejects missing and extra capability coverage", () => {
    const op = { id: "only", capability: "issue.read", provider_input: {} };
    expect(() => v4Registry(op, ["issue.read", "issue.write"], []).exportKeiToolManifest(4)).toThrow();
    expect(() => v4Registry({ ...op, capability: "other" }, ["issue.read"], []).exportKeiToolManifest(4)).toThrow();
  });

  it("accepts resource-less and parent-only collection operations", () => {
    expect(v4Registry({ id: "none", capability: "issue.read", provider_input: {} }, ["issue.read"], []).exportKeiToolManifest(4).tools).toHaveLength(1);
    const op = { id: "list", capability: "issue.read", resource: { type: "issue", parent: { type: "repository", id: { from: "context", field: "repository", type: "string" } } }, provider_resource_template: "repos/{parent.id}/issues", provider_input: {} };
    expect(v4Registry(op).exportKeiToolManifest(4).tools).toHaveLength(1);
  });

  it("should match the shared fixture", () => {
    const registry = new ToolRegistry();
    registry.register(new GovernedGetIssueTool());
    registry.register(new GovernedListIssuesTool());
    registry.register(new GovernedLinearTool());
    registry.register(new UngovernedEchoTool());

    const manifest = registry.exportKeiToolManifest();
    const expected = loadFixture();

    expect(manifest).toEqual(expected);
    const fixtureText = readFileSync(
      join(__dirname, "..", "..", "fixtures", "kei", "tool-manifest.v2.json"),
      "utf8",
    ).trim();
    expect(JSON.stringify(manifest, null, 2)).toBe(fixtureText);
  });

  it("should produce empty tools for empty registry", () => {
    const registry = new ToolRegistry();
    const manifest = registry.exportKeiToolManifest();
    expect(manifest).toEqual({ schema: "kei.tool-manifest/v2", tools: [] });
  });

  it("should exclude ungoverned tools", () => {
    const registry = new ToolRegistry();
    registry.register(new UngovernedEchoTool());
    const manifest = registry.exportKeiToolManifest();
    expect(manifest).toEqual({ schema: "kei.tool-manifest/v2", tools: [] });
  });

  it("lints the shared v2 fixture", () => {
    const raw = readFileSync(
      join(__dirname, "..", "..", "fixtures", "kei", "tool-manifest.v2.json"),
      "utf8",
    );
    expect(lintKeiToolManifest(raw)).toEqual([]);
  });

  it("prefers a connector route when the registered tool also has local execute", () => {
    const registry = new ToolRegistry();
    registry.register(new UngovernedEchoTool(), {
      service: "local", source: "harness", operation_class: "write",
      route: { harness_executor: { executor: "pi", registration: "local_echo" } },
    });
    registry.register(new GovernedGetIssueTool(), {
      service: "github", source: "github", operation_class: "read",
      route: { connector_binding: { agent_id: "agent-1", connector_id: "binding-1" } },
      required_capabilities: ["issue.read"],
      resource_types: [{ type: "issue", parent_type: "repository" }],
    });
    expect(typeof registry.get("github.get_issue")?.execute).toBe("function");
    const connectorEntry = registry.exportKeiToolManifest(3).tools[0];
    expect(connectorEntry.route).toEqual({ connector_binding: { agent_id: "agent-1", connector_id: "binding-1" } });
    expect("harness_executor" in connectorEntry.route).toBe(false);
    expect(registry.exportKeiToolManifest(3)).toEqual({
      schema: "kei.tool-manifest/v3",
      tools: [
        { name: "github.get_issue", service: "github", source: "github", operation_class: "read",
          route: { connector_binding: { agent_id: "agent-1", connector_id: "binding-1" } }, required_capabilities: ["issue.read"],
          resource_types: [{ type: "issue", parent_type: "repository" }], description: "Fetch an issue from a GitHub repository", enabled: true },
        { name: "local_echo", service: "local", source: "harness", operation_class: "write",
          route: { harness_executor: { executor: "pi", registration: "local_echo" } }, description: "Echo input back", enabled: true },
      ],
    });
  });

  it("rejects incomplete v3 registration metadata", () => {
    const registry = new ToolRegistry();
    registry.register(new UngovernedEchoTool(), { service: "", source: "harness", operation_class: "write",
      route: { harness_executor: { executor: "pi", registration: "local_echo" } } });
    expect(() => registry.exportKeiToolManifest(3)).toThrow("non-empty service and source");
  });

  it("rejects connector routes without explicit agent identity", () => {
    const registry = new ToolRegistry();
    registry.register(new GovernedGetIssueTool(), { service: "github", source: "github", operation_class: "read",
      route: { connector_binding: { connector_id: "binding-1" } } as any, required_capabilities: ["issue.read"] });
    expect(() => registry.exportKeiToolManifest(3)).toThrow("connector route requires binding");
  });

  it("keeps v1 behind an explicit export option", () => {
    const registry = new ToolRegistry();
    registry.register(new GovernedGetIssueTool());
    const legacy = registry.exportKeiToolManifest(1);
    expect("schema" in legacy).toBe(false);
    expect(legacy.tools[0].action).toBe("read");
  });
});
