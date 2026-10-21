import { createServer, type Server } from "node:http";
import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

interface FakeProxy {
  socketPath: string;
  close(): Promise<void>;
}

export async function startFakeProxy(): Promise<FakeProxy> {
  const directory = mkdtempSync(join(tmpdir(), "aw-ts-"));
  const socketPath = join(directory, "kei.sock");
  const server: Server = createServer(async (request, response) => {
    const chunks: Buffer[] = [];
    for await (const chunk of request) chunks.push(Buffer.from(chunk));
    const body = chunks.length
      ? JSON.parse(Buffer.concat(chunks).toString("utf8")) as Record<string, unknown>
      : {};
    const answer = request.url === "/v1/session"
      ? {
          schema: "kei.session/v2",
          session_id: "fake-session",
          user_id: `u-${String(body.external_id ?? "")}`,
          status: "linked",
        }
      : body.tool === "echo"
        ? {
            schema: "kei.authorize-response/v1",
            decision: "permit",
            policy_id: "fake-allow-echo",
            bundle_state: "fresh",
          }
        : body.tool === "delete_everything"
          ? {
              schema: "kei.authorize-response/v1",
              decision: "deny",
              reason_code: "no_matching_policy",
              reason: "no policy permits this tool",
              hint: "Add a policy for this tool: kei policies create",
            }
          : body.tool === "subject_unlinked_tool"
            ? {
                schema: "kei.authorize-response/v1",
                decision: "deny",
                reason_code: "subject_unlinked",
                reason: "the subject is not linked to this installation",
                hint: "Link this identity to the installation: open the enrollment link when the response carries one, or link the user on the console Users page.",
                enrollment: { claim_url: "https://kei.example.test/claim/1" },
              }
            : body.tool === "preflight_required_tool"
              ? {
                  schema: "kei.authorize-response/v1",
                  decision: "deny",
                  reason_code: "preflight_required",
                  reason: "no registered tool by this name",
                  hint: "Register the tool so the active policy bundle lists it (POST /v1/tools:register on the runtime socket, or kei harness sync --harness custom --manifest tools.json), then wait for the next bundle refresh.",
                }
              : {
                  schema: "kei.authorize-response/v1",
                  decision: "deny",
                  reason_code: "tool_not_registered",
                  hint: "Register the harness tools: kei harness sync --harness custom --manifest tools.json",
                };

    if (request.url === "/v1/authorize") {
      console.log(`AUDIT ${JSON.stringify({
        tool: body.tool,
        decision: answer.decision,
        reason_code: "reason_code" in answer ? answer.reason_code : "",
        hint: "hint" in answer ? answer.hint : "",
      })}`);
    }
    response.writeHead(request.url === "/v1/session" ? 201 : 200, {
      "Content-Type": "application/json",
    }).end(JSON.stringify(answer));
  });

  await new Promise<void>((resolve, reject) => {
    server.once("error", reject);
    server.listen(socketPath, resolve);
  });
  return {
    socketPath,
    close: () => new Promise<void>((resolve, reject) => {
      server.close((error) => {
        if (error) reject(error);
        else resolve();
      });
    }),
  };
}
