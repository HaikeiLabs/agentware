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

  it("exports v3 routes only from explicit dispatch registrations", () => {
    const registry = new ToolRegistry();
    registry.register(new UngovernedEchoTool(), {
      service: "local", source: "harness", operation_class: "write",
      route: { harness_executor: { executor: "pi", registration: "local_echo" } },
    });
    registry.register(new GovernedGetIssueTool(), {
      service: "github", source: "github", operation_class: "read",
      route: { connector_binding: { connector_id: "binding-1" } },
      required_capabilities: ["issue.read"],
      resource_types: [{ type: "issue", parent_type: "repository" }],
    });
    expect(registry.exportKeiToolManifest(3)).toEqual({
      schema: "kei.tool-manifest/v3",
      tools: [
        { name: "github.get_issue", service: "github", source: "github", operation_class: "read",
          route: { connector_binding: { connector_id: "binding-1" } }, required_capabilities: ["issue.read"],
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

  it("keeps v1 behind an explicit export option", () => {
    const registry = new ToolRegistry();
    registry.register(new GovernedGetIssueTool());
    const legacy = registry.exportKeiToolManifest(1);
    expect("schema" in legacy).toBe(false);
    expect(legacy.tools[0].action).toBe("read");
  });
});
