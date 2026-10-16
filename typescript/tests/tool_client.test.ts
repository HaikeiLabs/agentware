import { Action, AuditedToolClient, InMemoryAuditor } from "../src/index.js";
import type { CallerContext, Decision } from "../src/index.js";

const caller: CallerContext = {
  user_id: "user-1",
  session_id: "session-1",
  invoking_subject: "human-1",
  parent_span: "parent-1",
  delegation_depth: 2,
  trusted: false,
};

const decision = (action: Action): Decision => ({
  action,
  rule: "fixture-rule",
  reason: action,
  timestamp: new Date(),
});

describe("AuditedToolClient", () => {
  it("awaits policy before executing and audits allowed delegation context", async () => {
    const auditor = new InMemoryAuditor();
    const order: string[] = [];
    const client = new AuditedToolClient("test-harness", async (_tool, args) => {
      order.push("execute");
      return [args, true, ""];
    }, auditor).withPolicy({
      evaluate: async () => {
        await Promise.resolve();
        order.push("evaluate");
        return decision(Action.ALLOW);
      },
    });

    const result = await client.execute("read", { id: 1 }, caller);

    expect(order).toEqual(["evaluate", "execute"]);
    expect(result[1]).toBe(true);
    expect(auditor.query({})).toMatchObject([{
      invoking_subject: "human-1",
      parent_span: "parent-1",
      delegation_depth: 2,
      framework: "test-harness",
      decision: { action: Action.ALLOW },
    }]);
  });

  it("denies and audits when an asynchronous evaluator throws", async () => {
    const auditor = new InMemoryAuditor();
    let executions = 0;
    const client = new AuditedToolClient("test-harness", async () => {
      executions++;
      return [null, true, ""];
    }, auditor).withPolicy({ evaluate: async () => { throw new Error("private detail"); } });

    const result = await client.execute("write", {}, caller);

    expect(result[1]).toBe(false);
    expect(result[2]).toContain("default deny");
    expect(result[2]).not.toContain("private detail");
    expect(executions).toBe(0);
    expect(auditor.query({})).toMatchObject([{ decision: { action: Action.DENY } }]);
  });
});
