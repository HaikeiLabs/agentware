import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { Guardrails } from "../src/middleware/guardrails/facade";

describe("Guardrails facade shared v1 cases", () => {
  const contract = JSON.parse(readFileSync(resolve(process.cwd(), "../fixtures/guardrails/cases.v1.json"), "utf8"));

  it.each(contract.cases as any[])("replays $name", (testCase) => {
    const guardrails = new Guardrails(testCase.config);
    if (testCase.input.record) {
      const result = guardrails.record(testCase.input.record, testCase.input.state);
      expect(result).toMatchObject(testCase.expected);
      return;
    }
    const result = guardrails.evaluate(testCase.input.response, testCase.input.state);
    expect(result).toMatchObject(testCase.expected);
  });
});
