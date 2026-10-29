import { readFileSync } from "node:fs";
import type { ToolSpec } from "./index.js";
import { MANIFEST_SCHEMA_V3 } from "./index.js";

export type ManifestV3ErrorCode =
  | "invalid_json"
  | "invalid_type"
  | "unsupported_schema"
  | "unknown_field"
  | "missing_field"
  | "invalid_operation_class"
  | "invalid_route"
  | "duplicate_tool_name";

/** A manifest validation error with the parity-contract error code. */
export class ManifestV3Error extends Error {
  constructor(
    public readonly code: ManifestV3ErrorCode,
    message: string,
    public readonly path?: string,
  ) {
    super(path ? `${message} at ${path}` : message);
    this.name = "ManifestV3Error";
  }
}

/** Load and validate a v3 tool manifest from a file. */
export function loadManifestV3(path: string): ToolSpec[] {
  return parseManifestV3(readFileSync(path));
}

/** Parse and validate a v3 tool manifest. */
export function parseManifestV3(data: string | Uint8Array): ToolSpec[] {
  let document: unknown;
  try {
    document = JSON.parse(
      typeof data === "string" ? data : new TextDecoder().decode(data),
    );
  } catch {
    throw new ManifestV3Error("invalid_json", "manifest is not valid JSON");
  }
  const root = objectAt(document, "");
  if (root.schema !== MANIFEST_SCHEMA_V3) {
    fail("unsupported_schema", "schema is missing or unsupported", "/schema");
  }
  checkFields(root, ["schema", "tools"], "");
  if (!Object.hasOwn(root, "tools"))
    fail("missing_field", "tools is required", "/tools");
  if (!Array.isArray(root.tools))
    fail("invalid_type", "tools must be an array", "/tools");

  const names = new Set<string>();
  const specs: ToolSpec[] = [];
  for (let i = 0; i < root.tools.length; i++) {
    const path = `/tools/${i}`;
    const tool = objectAt(root.tools[i], path);
    checkFields(
      tool,
      [
        "name",
        "description",
        "service",
        "source",
        "operation_class",
        "route",
        "required_capabilities",
        "resource_types",
        "parameters",
        "enabled",
      ],
      path,
    );
    const name = requiredString(tool, "name", path);
    const description = requiredString(tool, "description", path);
    const service = requiredString(tool, "service", path);
    const source = requiredString(tool, "source", path);
    if (!Object.hasOwn(tool, "operation_class"))
      fail(
        "missing_field",
        "operation_class is required",
        `${path}/operation_class`,
      );
    if (tool.operation_class !== "read" && tool.operation_class !== "write") {
      fail(
        typeof tool.operation_class === "string"
          ? "invalid_operation_class"
          : "invalid_type",
        "operation_class must be read or write",
        `${path}/operation_class`,
      );
    }
    if (!Object.hasOwn(tool, "route"))
      fail("missing_field", "route is required", `${path}/route`);
    const routeObject = objectAt(tool.route, `${path}/route`);
    checkFields(
      routeObject,
      ["harness_executor", "connector_binding"],
      `${path}/route`,
    );
    const kinds = ["harness_executor", "connector_binding"].filter((kind) =>
      Object.hasOwn(routeObject, kind),
    );
    if (kinds.length !== 1)
      fail(
        "invalid_route",
        "route must contain exactly one kind",
        `${path}/route`,
      );

    let route: ToolSpec["route"];
    if (kinds[0] === "harness_executor") {
      const harness = objectAt(
        routeObject.harness_executor,
        `${path}/route/harness_executor`,
      );
      checkFields(
        harness,
        ["executor", "registration"],
        `${path}/route/harness_executor`,
      );
      const executor = routeString(
        harness,
        "executor",
        `${path}/route/harness_executor`,
      );
      const registration = routeString(
        harness,
        "registration",
        `${path}/route/harness_executor`,
      );
      if (
        Object.hasOwn(tool, "required_capabilities") ||
        Object.hasOwn(tool, "resource_types")
      ) {
        fail(
          "invalid_route",
          "harness_executor cannot carry connector fields",
          Object.hasOwn(tool, "required_capabilities")
            ? `${path}/required_capabilities`
            : `${path}/resource_types`,
        );
      }
      route = { harness_executor: { executor, registration } };
    } else {
      const binding = objectAt(
        routeObject.connector_binding,
        `${path}/route/connector_binding`,
      );
      checkFields(
        binding,
        ["agent_id", "connector_id"],
        `${path}/route/connector_binding`,
      );
      const agent_id = routeString(
        binding,
        "agent_id",
        `${path}/route/connector_binding`,
      );
      const connector_id = routeString(
        binding,
        "connector_id",
        `${path}/route/connector_binding`,
      );
      route = { connector_binding: { agent_id, connector_id } };
    }

    const required_capabilities = optionalStringArray(
      tool,
      "required_capabilities",
      path,
    );
    const resource_types = optionalResourceTypes(tool, path);
    if ("connector_binding" in route) {
      if (!required_capabilities?.length)
        fail(
          "invalid_route",
          "connector_binding requires capabilities",
          `${path}/required_capabilities`,
        );
      if (Object.hasOwn(tool, "resource_types") && !resource_types?.length)
        fail(
          "invalid_route",
          "resource_types must not be empty when present",
          `${path}/resource_types`,
        );
    }
    let parameters: Record<string, unknown> = {};
    if (Object.hasOwn(tool, "parameters")) {
      parameters = objectAt(tool.parameters, `${path}/parameters`);
    }
    let enabled = true;
    if (Object.hasOwn(tool, "enabled")) {
      if (typeof tool.enabled !== "boolean")
        fail("invalid_type", "enabled must be a boolean", `${path}/enabled`);
      enabled = tool.enabled;
    }
    if (names.has(name))
      fail("duplicate_tool_name", "tool name is duplicated", `${path}/name`);
    names.add(name);
    specs.push({
      name,
      description,
      service,
      source,
      operation_class: tool.operation_class,
      route,
      required_capabilities,
      resource_types,
      parameters,
      enabled,
    });
  }
  return specs;
}

type JsonObject = Record<string, unknown>;
function objectAt(value: unknown, path: string): JsonObject {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    fail("invalid_type", "value must be an object", path);
  }
  return value as JsonObject;
}
function checkFields(
  object: JsonObject,
  allowed: string[],
  path: string,
): void {
  for (const key of Object.keys(object)) {
    if (!allowed.includes(key))
      fail(
        "unknown_field",
        "unknown field",
        path ? `${path}/${key}` : `/${key}`,
      );
  }
}
function requiredString(object: JsonObject, key: string, path: string): string {
  const fieldPath = `${path}/${key}`;
  if (!Object.hasOwn(object, key))
    fail("missing_field", `${key} is required`, fieldPath);
  if (typeof object[key] !== "string")
    fail("invalid_type", `${key} must be a string`, fieldPath);
  if (object[key].length === 0)
    fail("missing_field", `${key} must not be empty`, fieldPath);
  return object[key];
}
function routeString(object: JsonObject, key: string, path: string): string {
  const fieldPath = `${path}/${key}`;
  if (
    !Object.hasOwn(object, key) ||
    typeof object[key] !== "string" ||
    object[key].length === 0
  ) {
    if (Object.hasOwn(object, key) && typeof object[key] !== "string")
      fail("invalid_type", `${key} must be a string`, fieldPath);
    fail("invalid_route", `${key} must not be empty`, fieldPath);
  }
  return object[key];
}
function optionalStringArray(
  object: JsonObject,
  key: string,
  path: string,
): string[] | undefined {
  if (!Object.hasOwn(object, key)) return undefined;
  if (!Array.isArray(object[key]))
    fail("invalid_type", `${key} must be an array`, `${path}/${key}`);
  return object[key].map((item: unknown, i: number) => {
    if (typeof item !== "string")
      fail(
        "invalid_type",
        `${key} entries must be strings`,
        `${path}/${key}/${i}`,
      );
    return item;
  });
}
function optionalResourceTypes(
  object: JsonObject,
  path: string,
): ToolSpec["resource_types"] {
  if (!Object.hasOwn(object, "resource_types")) return undefined;
  if (!Array.isArray(object.resource_types))
    fail(
      "invalid_type",
      "resource_types must be an array",
      `${path}/resource_types`,
    );
  return object.resource_types.map((value: unknown, i: number) => {
    const itemPath = `${path}/resource_types/${i}`;
    const item = objectAt(value, itemPath);
    checkFields(item, ["type", "parent_type"], itemPath);
    const type = requiredString(item, "type", itemPath);
    const result: { type: string; parent_type?: string } = { type };
    if (Object.hasOwn(item, "parent_type")) {
      if (typeof item.parent_type !== "string")
        fail(
          "invalid_type",
          "parent_type must be a string",
          `${itemPath}/parent_type`,
        );
      result.parent_type = item.parent_type;
    }
    return result;
  });
}
function fail(code: ManifestV3ErrorCode, message: string, path: string): never {
  throw new ManifestV3Error(code, message, path);
}
