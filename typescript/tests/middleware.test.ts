import {
  MiddlewareImpl,
  newMiddleware,
  ToolExecutor,
} from "../src/middleware/middleware.js";
import { SimplePolicyEvaluator, Policy, Operator } from "../src/middleware/policy.js";
import { Action } from "../src/middleware/types.js";
import { InMemoryAuditor, AuditRecord } from "../src/middleware/audit.js";
import type { CallerContext } from "../src/middleware/types.js";

const createMockExecutor = (): ToolExecutor & { callCount: number; lastCall: { tool: string; args: Record<string, unknown> } | null } => {
  let count = 0;
  let last: { tool: string; args: Record<string, unknown> } | null = null;
  return {
    execute: (toolName: string, args: Record<string, unknown>) => {
      count++;
      last = { tool: toolName, args };
      return [{ result: "success" }, true, ""];
    },
    get callCount() { return count; },
    get lastCall() { return last; },
  };
};

const createCaller = (overrides: Partial<CallerContext> = {}): CallerContext => ({
  user_id: "user-123",
  session_id: "session-456",
  role: "user",
  source: "api",
  trusted: false,
  ...overrides,
});

describe("MiddlewareImpl", () => {
  let executor: ReturnType<typeof createMockExecutor>;
  let middleware: MiddlewareImpl;

  beforeEach(() => {
    executor = createMockExecutor();
    middleware = newMiddleware(executor);
  });

  describe("basic execution", () => {
    it("should deny tool when no policy is configured", () => {
      const result = middleware.execute("test_tool", { foo: "bar" }, createCaller());

      expect(result[1]).toBe(false);
      expect(result[2]).toContain("no policy configured");
      expect(executor.callCount).toBe(0);
    });

    it("should return executor result tuple format", () => {
      const result = middleware.execute("test_tool", {}, createCaller());

      expect(result).toHaveLength(3);
      expect(result[0]).toBeNull();
      expect(result[1]).toBe(false);
      expect(result[2]).toContain("no policy configured");
    });
  });

  describe("with policy - allow", () => {
    it("should allow tool when policy permits", () => {
      const policy: Policy = {
        rules: [{ name: "allow_test", tools: ["test_tool"], action: Action.ALLOW }],
        default_deny: false,
      };
      middleware = middleware.withPolicy(new SimplePolicyEvaluator(policy));

      const result = middleware.execute("test_tool", {}, createCaller());

      expect(result[1]).toBe(true);
      expect(executor.callCount).toBe(1);
    });

    it("should deny tool when policy denies", () => {
      const policy: Policy = {
        rules: [{ name: "deny_test", tools: ["test_tool"], action: Action.DENY }],
        default_deny: false,
      };
      middleware = middleware.withPolicy(new SimplePolicyEvaluator(policy));

      const result = middleware.execute("test_tool", {}, createCaller());

      expect(result[1]).toBe(false);
      expect(result[2]).toContain("denied by policy");
      expect(executor.callCount).toBe(0);
    });

    it("should apply default deny when no rules match", () => {
      const policy: Policy = {
        rules: [],
        default_deny: true,
      };
      middleware = middleware.withPolicy(new SimplePolicyEvaluator(policy));

      const result = middleware.execute("test_tool", {}, createCaller());

      expect(result[1]).toBe(false);
      expect(executor.callCount).toBe(0);
    });

    it("should apply default allow when no rules match and default_deny is false", () => {
      const policy: Policy = {
        rules: [],
        default_deny: false,
      };
      middleware = middleware.withPolicy(new SimplePolicyEvaluator(policy));

      const result = middleware.execute("test_tool", {}, createCaller());

      expect(result[1]).toBe(true);
      expect(executor.callCount).toBe(1);
    });
  });

  describe("with policy - filter", () => {
    it("should filter args when policy specifies filter action", () => {
      const policy: Policy = {
        rules: [{
          name: "filter_sensitive",
          tools: ["test_tool"],
          action: Action.FILTER,
          redact_fields: ["password"],
        }],
        default_deny: false,
      };
      middleware = middleware.withPolicy(new SimplePolicyEvaluator(policy));

      const result = middleware.execute("test_tool", { username: "john", password: "secret" }, createCaller());

      expect(result[1]).toBe(true);
      expect(executor.lastCall?.args).toEqual({ username: "john", password: "[REDACTED]" });
    });
  });

  describe("with auditor", () => {
    it("should record audit entry on execution", () => {
      const auditor = new InMemoryAuditor();
      middleware = middleware.withAuditor(auditor);

      middleware.execute("test_tool", { foo: "bar" }, createCaller());

      const records = auditor.query({});
      expect(records).toHaveLength(1);
      expect(records[0].tool_name).toBe("test_tool");
      expect(records[0].args).toEqual({ foo: "bar" });
      expect(records[0].decision.action).toBe(Action.DENY);
      expect(executor.callCount).toBe(0);
    });

    it("should record deny decisions", () => {
      const auditor = new InMemoryAuditor();
      const policy: Policy = {
        rules: [{ name: "deny_all", action: Action.DENY }],
        default_deny: false,
      };
      middleware = middleware.withPolicy(new SimplePolicyEvaluator(policy)).withAuditor(auditor);

      middleware.execute("test_tool", {}, createCaller());

      const records = auditor.query({});
      expect(records).toHaveLength(1);
      expect(records[0].decision.action).toBe(Action.DENY);
    });

    it("should support querying by session_id", () => {
      const auditor = new InMemoryAuditor();
      middleware = middleware.withAuditor(auditor);

      middleware.execute("tool1", {}, createCaller({ session_id: "session-a" }));
      middleware.execute("tool2", {}, createCaller({ session_id: "session-b" }));
      middleware.execute("tool3", {}, createCaller({ session_id: "session-a" }));

      const records = auditor.query({ session_id: "session-a" });
      expect(records).toHaveLength(2);
    });

    it("should support querying by tool_name", () => {
      const auditor = new InMemoryAuditor();
      middleware = middleware.withAuditor(auditor);

      middleware.execute("read_file", {}, createCaller());
      middleware.execute("write_file", {}, createCaller());
      middleware.execute("read_file", {}, createCaller());

      const records = auditor.query({ tool_name: "read_file" });
      expect(records).toHaveLength(2);
    });

    it("should support querying by action", () => {
      const auditor = new InMemoryAuditor();
      const policy: Policy = {
        rules: [
          { name: "allow_tool1", tools: ["tool1"], action: Action.ALLOW },
          { name: "deny_tool2", tools: ["tool2"], action: Action.DENY },
        ],
        default_deny: false,
      };
      middleware = middleware.withPolicy(new SimplePolicyEvaluator(policy)).withAuditor(auditor);

      middleware.execute("tool1", {}, createCaller());
      middleware.execute("tool2", {}, createCaller());

      const denied = auditor.query({ action: Action.DENY });
      expect(denied).toHaveLength(1);
      expect(denied[0].tool_name).toBe("tool2");
    });

    it("should support querying with since filter", () => {
      const auditor = new InMemoryAuditor();
      middleware = middleware.withAuditor(auditor);

      middleware.execute("old_tool", {}, createCaller());
      const cutoff = new Date();
      middleware.execute("new_tool", {}, createCaller());
      const afterCutoff = new Date();

      const records = auditor.query({ since: cutoff });
      expect(records.length).toBeGreaterThanOrEqual(1);
      expect(records.some(r => r.tool_name === "new_tool")).toBe(true);
    });

    it("should support limit in query", () => {
      const auditor = new InMemoryAuditor();
      middleware = middleware.withAuditor(auditor);

      for (let i = 0; i < 10; i++) {
        middleware.execute(`tool_${i}`, {}, createCaller());
      }

      const records = auditor.query({ limit: 3 });
      expect(records).toHaveLength(3);
    });
  });

  describe("chaining", () => {
    it("should support chaining withPolicy and withAuditor", () => {
      const policy: Policy = {
        rules: [{ name: "allow", action: Action.ALLOW }],
        default_deny: false,
      };
      const auditor = new InMemoryAuditor();

      const result = middleware
        .withPolicy(new SimplePolicyEvaluator(policy))
        .withAuditor(auditor)
        .execute("test", {}, createCaller());

      expect(result[1]).toBe(true);
      expect(auditor.query({})).toHaveLength(1);
    });
  });
});

describe("SimplePolicyEvaluator", () => {
  const createCaller = (overrides: Partial<CallerContext> = {}): CallerContext => ({
    user_id: "user-123",
    session_id: "session-456",
    role: "admin",
    source: "api",
    trusted: true,
    ...overrides,
  });

  describe("rule matching", () => {
    it("should match rule with specific tool", () => {
      const policy: Policy = {
        rules: [{ name: "read_only", tools: ["read_file"], action: Action.ALLOW }],
        default_deny: true,
      };
      const evaluator = new SimplePolicyEvaluator(policy);

      const allowResult = evaluator.evaluate("read_file", {}, createCaller());
      const denyResult = evaluator.evaluate("write_file", {}, createCaller());

      expect(allowResult.action).toBe(Action.ALLOW);
      expect(denyResult.action).toBe(Action.DENY);
    });

    it("should match wildcard tool", () => {
      const policy: Policy = {
        rules: [{ name: "allow_all", tools: ["*"], action: Action.ALLOW }],
        default_deny: true,
      };
      const evaluator = new SimplePolicyEvaluator(policy);

      const result = evaluator.evaluate("any_tool", {}, createCaller());
      expect(result.action).toBe(Action.ALLOW);
    });

    it("should apply rule without tools to all tools", () => {
      const policy: Policy = {
        rules: [{ name: "global_allow", action: Action.ALLOW }],
        default_deny: true,
      };
      const evaluator = new SimplePolicyEvaluator(policy);

      const result = evaluator.evaluate("any_tool", {}, createCaller());
      expect(result.action).toBe(Action.ALLOW);
    });
  });

  describe("conditions - caller fields", () => {
    it("should evaluate caller.role condition", () => {
      const policy: Policy = {
        rules: [{
          name: "admins_only",
          action: Action.ALLOW,
          conditions: [{ field: "caller.role", operator: Operator.EQ, value: "admin" }],
        }],
        default_deny: true,
      };
      const evaluator = new SimplePolicyEvaluator(policy);

      const adminResult = evaluator.evaluate("tool", {}, createCaller({ role: "admin" }));
      const userResult = evaluator.evaluate("tool", {}, createCaller({ role: "user" }));

      expect(adminResult.action).toBe(Action.ALLOW);
      expect(userResult.action).toBe(Action.DENY);
    });

    it("should evaluate caller.trusted condition", () => {
      const policy: Policy = {
        rules: [{
          name: "trusted_only",
          action: Action.ALLOW,
          conditions: [{ field: "caller.trusted", operator: Operator.EQ, value: "true" }],
        }],
        default_deny: true,
      };
      const evaluator = new SimplePolicyEvaluator(policy);

      const trustedResult = evaluator.evaluate("tool", {}, createCaller({ trusted: true }));
      const untrustedResult = evaluator.evaluate("tool", {}, createCaller({ trusted: false }));

      expect(trustedResult.action).toBe(Action.ALLOW);
      expect(untrustedResult.action).toBe(Action.DENY);
    });

    it("should evaluate caller.user_id condition", () => {
      const policy: Policy = {
        rules: [{
          name: "specific_user",
          action: Action.ALLOW,
          conditions: [{ field: "caller.user_id", operator: Operator.EQ, value: "user-123" }],
        }],
        default_deny: true,
      };
      const evaluator = new SimplePolicyEvaluator(policy);

      const result = evaluator.evaluate("tool", {}, createCaller({ user_id: "user-123" }));
      expect(result.action).toBe(Action.ALLOW);
    });

    it("should evaluate caller.source condition", () => {
      const policy: Policy = {
        rules: [{
          name: "web_only",
          action: Action.ALLOW,
          conditions: [{ field: "caller.source", operator: Operator.EQ, value: "web" }],
        }],
        default_deny: true,
      };
      const evaluator = new SimplePolicyEvaluator(policy);

      const webResult = evaluator.evaluate("tool", {}, createCaller({ source: "web" }));
      const apiResult = evaluator.evaluate("tool", {}, createCaller({ source: "api" }));

      expect(webResult.action).toBe(Action.ALLOW);
      expect(apiResult.action).toBe(Action.DENY);
    });
  });

  describe("conditions - args fields", () => {
    it("should evaluate args field condition", () => {
      const policy: Policy = {
        rules: [{
          name: "limit_size",
          action: Action.ALLOW,
          conditions: [{ field: "args.size", operator: Operator.NOT_EXISTS, value: "" }],
        }],
        default_deny: true,
      };
      const evaluator = new SimplePolicyEvaluator(policy);

      const withSize = evaluator.evaluate("tool", { size: 100 }, createCaller());
      const withoutSize = evaluator.evaluate("tool", {}, createCaller());

      expect(withSize.action).toBe(Action.DENY);
      expect(withoutSize.action).toBe(Action.ALLOW);
    });

    it("should evaluate args.CONTAINS condition", () => {
      const policy: Policy = {
        rules: [{
          name: "no_delete",
          action: Action.DENY,
          conditions: [{ field: "args.operation", operator: Operator.CONTAINS, value: "delete" }],
        }],
        default_deny: false,
      };
      const evaluator = new SimplePolicyEvaluator(policy);

      const deleteResult = evaluator.evaluate("tool", { operation: "delete_user" }, createCaller());
      const createResult = evaluator.evaluate("tool", { operation: "create_user" }, createCaller());

      expect(deleteResult.action).toBe(Action.DENY);
      expect(createResult.action).toBe(Action.ALLOW);
    });

    it("should evaluate args.MATCHES condition with regex", () => {
      const policy: Policy = {
        rules: [{
          name: "email_format",
          action: Action.ALLOW,
          conditions: [{ field: "args.email", operator: Operator.MATCHES, value: "^[^@]+@[^@]+$" }],
        }],
        default_deny: true,
      };
      const evaluator = new SimplePolicyEvaluator(policy);

      const validEmail = evaluator.evaluate("tool", { email: "test@example.com" }, createCaller());
      const invalidEmail = evaluator.evaluate("tool", { email: "not-an-email" }, createCaller());

      expect(validEmail.action).toBe(Action.ALLOW);
      expect(invalidEmail.action).toBe(Action.DENY);
    });
  });

  describe("multiple conditions", () => {
    it("should require all conditions to match (AND logic)", () => {
      const policy: Policy = {
        rules: [{
          name: "admin_from_web",
          action: Action.ALLOW,
          conditions: [
            { field: "caller.role", operator: Operator.EQ, value: "admin" },
            { field: "caller.source", operator: Operator.EQ, value: "web" },
          ],
        }],
        default_deny: true,
      };
      const evaluator = new SimplePolicyEvaluator(policy);

      const matchBoth = evaluator.evaluate("tool", {}, createCaller({ role: "admin", source: "web" }));
      const matchOne = evaluator.evaluate("tool", {}, createCaller({ role: "admin", source: "api" }));
      const matchNone = evaluator.evaluate("tool", {}, createCaller({ role: "user", source: "api" }));

      expect(matchBoth.action).toBe(Action.ALLOW);
      expect(matchOne.action).toBe(Action.DENY);
      expect(matchNone.action).toBe(Action.DENY);
    });

    it("should select first matching rule", () => {
      const policy: Policy = {
        rules: [
          { name: "first_rule", tools: ["tool1"], action: Action.DENY },
          { name: "second_rule", tools: ["tool1"], action: Action.ALLOW },
        ],
        default_deny: true,
      };
      const evaluator = new SimplePolicyEvaluator(policy);

      const result = evaluator.evaluate("tool1", {}, createCaller());
      expect(result.action).toBe(Action.DENY);
      expect(result.rule).toBe("first_rule");
    });
  });

  describe("edge cases", () => {
    it("should handle missing args field gracefully", () => {
      const policy: Policy = {
        rules: [{
          name: "check_field",
          action: Action.DENY,
          conditions: [{ field: "args.missing", operator: Operator.EQ, value: "test" }],
        }],
        default_deny: false,
      };
      const evaluator = new SimplePolicyEvaluator(policy);

      const result = evaluator.evaluate("tool", {}, createCaller());
      expect(result.action).toBe(Action.ALLOW);
    });

    it("should handle invalid regex in MATCHES gracefully", () => {
      const policy: Policy = {
        rules: [{
          name: "bad_regex",
          action: Action.ALLOW,
          conditions: [{ field: "args.field", operator: Operator.MATCHES, value: "[invalid(" }],
        }],
        default_deny: true,
      };
      const evaluator = new SimplePolicyEvaluator(policy);

      const result = evaluator.evaluate("tool", { field: "test" }, createCaller());
      expect(result.action).toBe(Action.DENY);
    });

    it("should handle invalid regex in NOT_MATCHES gracefully", () => {
      const policy: Policy = {
        rules: [{
          name: "bad_regex",
          action: Action.ALLOW,
          conditions: [{ field: "args.field", operator: Operator.NOT_MATCHES, value: "[invalid(" }],
        }],
        default_deny: true,
      };
      const evaluator = new SimplePolicyEvaluator(policy);

      const result = evaluator.evaluate("tool", { field: "test" }, createCaller());
      expect(result.action).toBe(Action.ALLOW);
    });
  });
});

describe("InMemoryAuditor", () => {
  it("should store and retrieve records", () => {
    const auditor = new InMemoryAuditor();
    const record: AuditRecord = {
      session_id: "s1",
      tool_name: "tool1",
      args: {},
      decision: { action: Action.ALLOW, rule: "test", reason: "ok", timestamp: new Date() },
      timestamp: new Date(),
    };

    auditor.record(record);
    const results = auditor.query({});

    expect(results).toHaveLength(1);
  });

  it("should clear all records", () => {
    const auditor = new InMemoryAuditor();
    auditor.record({
      session_id: "s1",
      tool_name: "tool1",
      args: {},
      decision: { action: Action.ALLOW, rule: "test", reason: "ok", timestamp: new Date() },
      timestamp: new Date(),
    });

    auditor.clear();
    expect(auditor.query({})).toHaveLength(0);
  });
});
