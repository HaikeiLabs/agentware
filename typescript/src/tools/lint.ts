import { readFileSync } from "node:fs";

type JsonObject = Record<string, unknown>;
const retired = new Set([
  "allow",
  "approval",
  "approval_id",
  "approval_required",
  "requires_approval",
]);
const identifier = /^[A-Za-z][A-Za-z0-9_.-]*$/;

/** Validate a tool manifest using the shared v2 linter rules. */
export function lintKeiToolManifest(
  raw: string,
  capabilityFile = "fixtures/kei/connector-capabilities.v0.5.0.json",
): string[] {
  let manifest: JsonObject;
  try {
    manifest = JSON.parse(raw) as JsonObject;
  } catch {
    return ["invalid JSON"];
  }
  if (manifest.schema !== "kei.tool-manifest/v2")
    return ["schema must be kei.tool-manifest/v2"];
  if (!Array.isArray(manifest.tools)) return ["tools must be an array"];
  let capabilityData: string;
  try {
    capabilityData = readFileSync(capabilityFile, "utf8");
  } catch {
    capabilityData = readFileSync(`../${capabilityFile}`, "utf8");
  }
  const capabilities = JSON.parse(capabilityData) as Record<string, string[]>;
  const errors: string[] = [];
  const seen = new Set<string>();
  const names: string[] = [];
  manifest.tools.forEach((value, index) => {
    if (!value || typeof value !== "object" || Array.isArray(value)) {
      errors.push(`tools[${index}] must be an object`);
      return;
    }
    const tool = value as JsonObject;
    const prefix = `tools[${index}]`;
    const name = typeof tool.name === "string" ? tool.name : "";
    names.push(name);
    if (seen.has(name)) errors.push(`duplicate tool name: ${name}`);
    seen.add(name);
    const source = typeof tool.source === "string" ? tool.source : "";
    if (!source) errors.push(`${prefix} is missing source`);
    if (
      !Array.isArray(tool.required_capabilities) ||
      tool.required_capabilities.length === 0
    )
      errors.push(`${prefix} is missing required_capabilities`);
    else {
      const caps = tool.required_capabilities as unknown[];
      if (caps.some((cap, i) => i > 0 && String(caps[i - 1]) > String(cap)))
        errors.push(`${prefix} required_capabilities are not sorted`);
      for (const cap of caps)
        if (
          typeof cap !== "string" ||
          !(capabilities[source] ?? []).includes(cap)
        )
          errors.push(
            `${prefix} capability ${JSON.stringify(cap)} is not declared for source ${JSON.stringify(source)}`,
          );
    }
    if (Array.isArray(tool.resource_types)) {
      const resources = tool.resource_types as unknown[];
      const keys = resources.map((value) =>
        value && typeof value === "object"
          ? `${(value as JsonObject).type ?? ""}\0${(value as JsonObject).parent_type ?? ""}`
          : "",
      );
      if (keys.some((key, i) => i > 0 && keys[i - 1] > key))
        errors.push(`${prefix} resource_types are not sorted`);
      for (const value of resources) {
        if (!value || typeof value !== "object" || Array.isArray(value)) {
          errors.push(`${prefix} resource type must be an object`);
          continue;
        }
        const resource = value as JsonObject;
        for (const key of ["type", "parent_type"] as const)
          if (
            resource[key] !== undefined &&
            (typeof resource[key] !== "string" ||
              !identifier.test(resource[key] as string))
          )
            errors.push(
              `${prefix} invalid resource type ${key}: ${JSON.stringify(resource[key])}`,
            );
        if (resource.type === undefined)
          errors.push(`${prefix} invalid resource type type: undefined`);
      }
    } else errors.push(`${prefix}.resource_types must be an array`);
    if (tool.operation_class !== "read" && tool.operation_class !== "write")
      errors.push(`${prefix} operation_class must be read or write`);
    if (containsRetired(tool))
      errors.push(`${prefix} contains allow or a retired approval field`);
  });
  if (names.some((name, index) => index > 0 && names[index - 1] > name))
    errors.push("tools are not sorted by name");
  return errors.sort();
}

function containsRetired(value: unknown): boolean {
  if (Array.isArray(value)) return value.some(containsRetired);
  if (value && typeof value === "object")
    return Object.entries(value).some(
      ([key, child]) =>
        retired.has(key.toLowerCase()) ||
        key.toLowerCase().includes("approval") ||
        containsRetired(child),
    );
  if (typeof value === "string") return value.toLowerCase() === "allow";
  return false;
}
