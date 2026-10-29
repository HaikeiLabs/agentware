import { appendFileSync, readFileSync } from "node:fs";
import { resolve } from "node:path";
import {
  loadManifestV3,
  ManifestV3Error,
  parseManifestV3,
  serializeToolSpecs,
  type ToolSpec,
} from "../src/toolspec/index.js";

// Records a passing shared fixture case for the equal-coverage gate
// (scripts/conformance/gate.py, docs/testing.md); a no-op unless
// AGENTWARE_CONFORMANCE_LOG is set.
function record(fixture: string, section: string, name: string): void {
  const log = process.env.AGENTWARE_CONFORMANCE_LOG;
  if (!log) return;
  appendFileSync(log, `${JSON.stringify({ language: "typescript", fixture, section, case: name })}\n`);
}

const root = resolve(process.cwd(), "../fixtures/toolspec/manifest-v3");
const index = JSON.parse(readFileSync(resolve(root, "index.json"), "utf8")) as {
  valid: Array<{
    name: string;
    input: string;
    expected: string;
    canonical: string;
  }>;
  invalid: Array<{ name: string; input: string; error: string }>;
};

function readJson(path: string): unknown {
  return JSON.parse(readFileSync(resolve(root, path), "utf8"));
}

function normalize(specs: ToolSpec[]): unknown {
  return {
    tools: specs.map((spec) => ({
      name: spec.name,
      description: spec.description,
      service: spec.service,
      source: spec.source,
      operation_class: spec.operation_class,
      route: spec.route,
      required_capabilities: spec.required_capabilities ?? [],
      resource_types: spec.resource_types ?? [],
      parameters: spec.parameters ?? {},
      enabled: spec.enabled ?? true,
    })),
  };
}

describe("kei.tool-manifest/v3 shared fixtures", () => {
  test.each(index.valid)(
    "$name loads, normalizes, and round-trips",
    (testCase) => {
      const input = readFileSync(resolve(root, testCase.input), "utf8");
      const specs = parseManifestV3(input);
      expect(normalize(specs)).toEqual(readJson(testCase.expected));
      expect(serializeToolSpecs(specs)).toEqual(readJson(testCase.canonical));
      expect(loadManifestV3(resolve(root, testCase.input))).toEqual(specs);
      record("toolspec/manifest-v3/index.json", "valid", testCase.name);
    },
  );

  test.each(index.invalid)("$name rejects with $error", (testCase) => {
    const input = readFileSync(resolve(root, testCase.input), "utf8");
    try {
      parseManifestV3(input);
      throw new Error("expected manifest parse to fail");
    } catch (error) {
      expect(error).toBeInstanceOf(ManifestV3Error);
      expect((error as ManifestV3Error).code).toBe(testCase.error);
    }
    record("toolspec/manifest-v3/index.json", "invalid", testCase.name);
  });
});
