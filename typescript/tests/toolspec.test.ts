import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { getFormatter } from "../src/toolformat/index.js";
import {
  serializeToolSpecs,
  toolSpecToToolDefinition,
  type ToolSpec,
} from "../src/toolspec/index.js";

interface Case {
  name: string;
  spec: ToolSpec;
  expected_wire?: Record<string, unknown>;
  expected_toolformat?: Record<string, string>;
  expected_error?: string;
}

const fixture = JSON.parse(
  readFileSync(
    process.env.TOOLSPEC_FIXTURE_PATH ?? resolve(process.cwd(), "../fixtures/toolspec/cases.v1.json"),
    "utf8",
  ),
) as { cases: Case[]; toolformat_compare?: Record<string, "json" | "text"> };

describe("ToolSpec shared fixtures", () => {
  for (const testCase of fixture.cases) {
    test(testCase.name, () => {
      if (testCase.expected_error) {
        expect(() => serializeToolSpecs([testCase.spec])).toThrow(testCase.expected_error);
        return;
      }
      if (testCase.expected_wire) {
        const actual = serializeToolSpecs([testCase.spec]);
        expect(actual.tools[0]).toEqual(testCase.expected_wire);
      }
      if (testCase.expected_toolformat) {
        for (const [flavor, expected] of Object.entries(testCase.expected_toolformat)) {
          if (fixture.toolformat_compare?.[flavor] === "text") continue;
          const actual = getFormatter(flavor).formatToolDefinitions([testCase.spec]);
          expect(JSON.parse(actual)).toEqual(JSON.parse(expected));
        }
      }
    });
  }

  test.each([
    "generic",
    "openai",
    "deepseek",
    "glm",
    "anthropic",
    "qwen",
    "llama",
    "minimax",
    "mistral",
    "nemotron",
  ])(
    "%s formats ToolSpec the same as its ToolDefinition equivalent",
    (flavor) => {
      const formatter = getFormatter(flavor);
      for (const testCase of fixture.cases) {
        expect(formatter.formatToolDefinitions([testCase.spec])).toBe(
          formatter.formatToolDefinitions([toolSpecToToolDefinition(testCase.spec)]),
        );
      }
    },
  );

  test.each([
    ["service", { service: undefined }, "service is required"],
    ["source", { source: undefined }, "source is required"],
    ["operation_class", { operation_class: undefined }, "operation_class is required"],
    ["route", { route: undefined }, "route is required"],
  ])("requires explicit %s metadata", (_field, missing, message) => {
    const complete: ToolSpec = {
      name: "sample.tool",
      description: "sample",
      parameters: { type: "object" },
      service: "sample",
      source: "sample",
      operation_class: "read",
      route: { harness_executor: { executor: "sample", registration: "sample.tool" } },
    };
    expect(() => serializeToolSpecs([{ ...complete, ...(missing as Partial<ToolSpec>) }])).toThrow(message);
  });

  test("defaults only enabled and omits model parameters from wire", () => {
    const spec: ToolSpec = {
      name: "local.tool",
      description: "Local only",
      parameters: { type: "object" },
      service: "local",
      source: "local",
      operation_class: "write",
      route: { harness_executor: { executor: "test", registration: "local.tool" } },
    };
    const wire = serializeToolSpecs([spec]).tools[0];
    expect(wire.enabled).toBe(true);
    expect(wire).not.toHaveProperty("parameters");
    expect(serializeToolSpecs([{ ...spec, enabled: false }]).tools[0].enabled).toBe(false);
  });

  test("requires connector capabilities and omits empty optional arrays", () => {
    const spec: ToolSpec = {
      name: "sample.read",
      description: "Read a sample",
      parameters: { type: "object" },
      service: "sample",
      source: "sample",
      operation_class: "read",
      route: { connector_binding: { agent_id: "a", connector_id: "c" } },
    };
    expect(() => serializeToolSpecs([spec])).toThrow("required_capabilities must not be empty");
    const wire = serializeToolSpecs([{ ...spec, required_capabilities: ["sample.read"], resource_types: [] }]).tools[0];
    expect(wire.required_capabilities).toEqual(["sample.read"]);
    expect(wire).not.toHaveProperty("resource_types");
  });
});
