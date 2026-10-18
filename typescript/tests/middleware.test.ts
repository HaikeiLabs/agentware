import {
  MiddlewareImpl,
  newMiddleware,
  ToolExecutor,
} from "../src/middleware/middleware.js";
import type { PolicyEvaluator } from "../src/middleware/policy.js";
import { Action } from "../src/middleware/types.js";
import { InMemoryAuditor, AuditRecord } from "../src/middleware/audit.js";
import type { CallerContext } from "../src/middleware/types.js";
import { createCallerContext } from "../src/middleware/types.js";

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

describe("CallerContext delegation", () => {
  it("preserves the invoking subject, increments depth, and does not mutate the parent", () => {
    const parent = createCallerContext(createCaller({ invoking_subject: "U_HUMAN" }));
    const child = parent.delegate("span-parent");
    const grandchild = child.delegate("span-child");

    expect([child.invoking_subject, grandchild.invoking_subject]).toEqual(["U_HUMAN", "U_HUMAN"]);
    expect([parent.delegation_depth, child.delegation_depth, grandchild.delegation_depth]).toEqual([undefined, 1, 2]);
    expect([child.parent_span, grandchild.parent_span]).toEqual(["span-parent", "span-child"]);
    expect(parent.parent_span).toBeUndefined();
  });

  it("inherits its own parent span when no new span is supplied", () => {
    expect(createCallerContext(createCaller({ parent_span: "outer" })).delegate("").parent_span).toBe("outer");
  });
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
    const allowEval: PolicyEvaluator = {
      evaluate: (toolName) =>
        toolName === "test_tool"
          ? { action: Action.ALLOW, rule: "allow_test", reason: "ok", timestamp: new Date() }
          : { action: Action.DENY, rule: "default", reason: "denied", timestamp: new Date() },
    };

    it("should allow tool when policy permits", () => {
      middleware = middleware.withPolicy(allowEval);

      const result = middleware.execute("test_tool", {}, createCaller());

      expect(result[1]).toBe(true);
      expect(executor.callCount).toBe(1);
    });

    it("should deny tool when policy denies", () => {
      middleware = middleware.withPolicy(allowEval);

      const result = middleware.execute("other_tool", {}, createCaller());

      expect(result[1]).toBe(false);
      expect(result[2]).toContain("denied by policy");
      expect(executor.callCount).toBe(0);
    });

    it("should apply default deny when no policy matches (fail closed)", () => {
      middleware = middleware.withPolicy({
        evaluate: () => ({ action: Action.DENY, rule: "default", reason: "denied", timestamp: new Date() }),
      });

      const result = middleware.execute("test_tool", {}, createCaller());

      expect(result[1]).toBe(false);
      expect(executor.callCount).toBe(0);
    });
  });

  describe("with policy - filter", () => {
    it("should filter args when policy specifies filter action", () => {
      middleware = middleware.withPolicy({
        evaluate: (_toolName, args) => ({
          action: Action.FILTER,
          rule: "filter_sensitive",
          reason: "redact password",
          timestamp: new Date(),
          redacted_args: "password" in args ? { password: true } : undefined,
        }),
      });

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
      middleware = middleware
        .withPolicy({
          evaluate: () => ({ action: Action.DENY, rule: "deny_all", reason: "denied", timestamp: new Date() }),
        })
        .withAuditor(auditor);

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
      middleware = middleware
        .withPolicy({
          evaluate: (toolName) =>
            toolName === "tool1"
              ? { action: Action.ALLOW, rule: "allow", reason: "ok", timestamp: new Date() }
              : { action: Action.DENY, rule: "deny", reason: "denied", timestamp: new Date() },
        })
        .withAuditor(auditor);

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
      const auditor = new InMemoryAuditor();

      const result = middleware
        .withPolicy({
          evaluate: () => ({ action: Action.ALLOW, rule: "allow", reason: "ok", timestamp: new Date() }),
        })
        .withAuditor(auditor)
        .execute("test", {}, createCaller());

      expect(result[1]).toBe(true);
      expect(auditor.query({})).toHaveLength(1);
    });
  });
});

describe("PolicyEvaluator delegation", () => {
  const createCaller = (overrides: Partial<CallerContext> = {}): CallerContext => ({
    user_id: "user-123",
    session_id: "session-456",
    role: "admin",
    source: "api",
    trusted: true,
    ...overrides,
  });

  it("passes tool name and args to the evaluator", () => {
    const seen: Array<{ toolName: string; args: Record<string, unknown> }> = [];
    const evaluator: PolicyEvaluator = {
      evaluate: (toolName, args) => {
        seen.push({ toolName, args });
        return { action: Action.ALLOW, rule: "test", reason: "ok", timestamp: new Date() };
      },
    };
    const executor: ToolExecutor = { execute: () => ["ok", true, ""] };
    const mw = newMiddleware(executor).withPolicy(evaluator);

    mw.execute("read_file", { path: "/tmp/x" }, createCaller());
    expect(seen).toEqual([{ toolName: "read_file", args: { path: "/tmp/x" } }]);
  });

  it("passes caller context through to the evaluator", () => {
    const seen: Array<CallerContext> = [];
    const evaluator: PolicyEvaluator = {
      evaluate: (_tn, _a, caller) => {
        seen.push(caller);
        return { action: Action.ALLOW, rule: "test", reason: "ok", timestamp: new Date() };
      },
    };
    const executor: ToolExecutor = { execute: () => ["ok", true, ""] };
    const mw = newMiddleware(executor).withPolicy(evaluator);

    const caller = createCaller({ role: "admin", trusted: true });
    mw.execute("tool", {}, caller);
    expect(seen[0].role).toBe("admin");
    expect(seen[0].trusted).toBe(true);
  });

  it("replaces the evaluator on successive withPolicy calls (last wins)", () => {
    const denyEval: PolicyEvaluator = {
      evaluate: () => ({ action: Action.DENY, rule: "r1", reason: "no", timestamp: new Date() }),
    };
    const allowEval: PolicyEvaluator = {
      evaluate: () => ({ action: Action.ALLOW, rule: "r2", reason: "yes", timestamp: new Date() }),
    };
    const executor: ToolExecutor = { execute: () => ["ok", true, ""] };
    const mw = newMiddleware(executor).withPolicy(denyEval).withPolicy(allowEval);

    const result = mw.execute("tool", {}, createCaller());
    expect(result[1]).toBe(true);
  });

  it("allows through a permissive evaluator", () => {
    const evaluator: PolicyEvaluator = {
      evaluate: () => ({ action: Action.ALLOW, rule: "allow_all", reason: "ok", timestamp: new Date() }),
    };
    const spy = { count: 0 };
    const executor: ToolExecutor = {
      execute: () => { spy.count++; return ["ok", true, ""]; },
    };
    const mw = newMiddleware(executor).withPolicy(evaluator);

    expect(mw.execute("any_tool", {}, createCaller())[1]).toBe(true);
    expect(spy.count).toBe(1);
  });

  it("denies through a restrictive evaluator", () => {
    const evaluator: PolicyEvaluator = {
      evaluate: () => ({ action: Action.DENY, rule: "deny_all", reason: "blocked", timestamp: new Date() }),
    };
    const spy = { count: 0 };
    const executor: ToolExecutor = {
      execute: () => { spy.count++; return ["ok", true, ""]; },
    };
    const mw = newMiddleware(executor).withPolicy(evaluator);

    expect(mw.execute("any_tool", {}, createCaller())[1]).toBe(false);
    expect(spy.count).toBe(0);
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
