import { execFileSync } from "node:child_process";
import { resolve } from "node:path";
import { Action } from "../src/index.js";

describe("TypeScript minimal harness example", () => {
  it("shows allow, deny details, and one audit line per authorization", () => {
    const example = resolve(process.cwd(), "../examples/typescript/minimal-harness");
    const output = execFileSync("npm", ["start", "--", "--fake"], {
      cwd: example,
      encoding: "utf8",
    });
    const lines = output.trim().split("\n");
    const results = lines.filter((line) => line.startsWith("{")).map((line) => JSON.parse(line));
    const audits = lines.filter((line) => line.startsWith("AUDIT ")).map((line) => JSON.parse(line.slice(6)));
    expect(results).toHaveLength(2);
    expect(results[0]).toMatchObject({ tool: "echo", decision: Action.ALLOW, result: "hello" });
    expect(results[1]).toMatchObject({ tool: "delete_everything", decision: Action.DENY });
    expect(results[1].reason_code).toBe("no_matching_policy");
    expect(results[1].hint).toBe("Add a policy for this tool: kei policies create");
    expect(results[1].reason).toContain("no policy permits this tool");
    expect(audits).toHaveLength(2);
    expect(audits.map(({ tool, decision }) => [tool, decision])).toEqual([
      ["echo", "permit"],
      ["delete_everything", "deny"],
    ]);
    expect(audits[1]).toMatchObject({
      reason_code: "no_matching_policy",
      hint: "Add a policy for this tool: kei policies create",
    });
  }, 30_000);
});
