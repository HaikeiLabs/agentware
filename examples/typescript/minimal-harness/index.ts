import {
  Action,
  KeiProxyEvaluator,
  selectAuthorizationClient,
} from "@haikeilabs/agentware";
import type {
  CallerContext,
  KeiProxyAuthorizeRequest,
} from "@haikeilabs/agentware";

type Authorization = Record<string, unknown>;

function isRecord(value: unknown): value is Authorization {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

export async function runHarness(): Promise<void> {
  const socketPath = process.env.KEI_RUNTIME_SOCKET_PATH;
  if (!socketPath) throw new Error("KEI_RUNTIME_SOCKET_PATH is required");

  const client = selectAuthorizationClient("example", { socketPath });
  let authorization: Authorization = {};
  const evaluator = new KeiProxyEvaluator({
    authorize: async (request: KeiProxyAuthorizeRequest): Promise<unknown> => {
      const response: unknown = await client.authorize(request);
      authorization = isRecord(response) ? response : {};
      return response;
    },
  }, { logger: () => undefined });
  const caller: CallerContext = {
    user_id: "local-user",
    invoking_subject: "local-user",
    trusted: false,
  };
  const echo = {
    name: "echo",
    execute: (args: Record<string, unknown>) => String(args.message ?? ""),
  };

  for (const [tool, args] of [
    [echo.name, { message: "hello" }],
    ["delete_everything", {}],
  ] as const) {
    authorization = {};
    const decision = await evaluator.evaluate(tool, args, caller);
    const result = decision.action === Action.ALLOW ? echo.execute(args) : undefined;
    console.log(JSON.stringify({
      tool,
      decision: decision.action,
      reason: decision.reason,
      reason_code: authorization.reason_code ?? null,
      hint: authorization.hint ?? null,
      result,
    }));
  }
}
