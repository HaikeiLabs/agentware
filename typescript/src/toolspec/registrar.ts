import type { KeiProxySocketAuthorizeClient } from "../kei/socketAuthorizeClient.js";
import { serializeToolSpecs, type ToolSpec } from "./index.js";

/** Serialize and register ToolSpecs with the local runtime socket. */
export async function registerToolSpecs(
  specs: readonly ToolSpec[],
  client: KeiProxySocketAuthorizeClient,
): Promise<Record<string, unknown>> {
  return client.registerToolManifest(serializeToolSpecs(specs));
}
