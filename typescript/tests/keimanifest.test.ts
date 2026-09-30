import { ToolRegistry, BaseTool, Result } from "../src/tools/index.js";
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
      service: "github",
      action: "read",
      resources: ["repo:haikeilabs/*", "issue:*"],
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
      service: "github",
      action: "read",
      resources: ["repo:haikeilabs/*", "issue:*"],
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
      service: "linear",
      action: "read",
      resources: ["team:*", "issue:*"],
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
      service: "slack",
      action: "write",
      resources: ["channel:*"],
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
      service: "email",
      action: "write",
      resources: [],
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
    "tool-manifest.v1.json"
  );
  return JSON.parse(readFileSync(fixturePath, "utf-8"));
}

describe("KeiToolManifest", () => {
  it("should match the shared fixture", () => {
    const registry = new ToolRegistry();
    registry.register(new GovernedGetIssueTool());
    registry.register(new GovernedListIssuesTool());
    registry.register(new GovernedLinearTool());
    registry.register(new GovernedSlackTool());
    registry.register(new GovernedEmailTool());
    registry.register(new UngovernedEchoTool());

    const manifest = registry.exportKeiToolManifest();
    const expected = loadFixture();

    expect(manifest).toEqual(expected);
  });

  it("should produce empty tools for empty registry", () => {
    const registry = new ToolRegistry();
    const manifest = registry.exportKeiToolManifest();
    expect(manifest).toEqual({ tools: [] });
  });

  it("should exclude ungoverned tools", () => {
    const registry = new ToolRegistry();
    registry.register(new UngovernedEchoTool());
    const manifest = registry.exportKeiToolManifest();
    expect(manifest).toEqual({ tools: [] });
  });
});
