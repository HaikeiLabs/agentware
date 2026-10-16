"""Throwaway prototype (HAI-716): agentware middleware inside an AgentCore Runtime app.

Runs locally on :8080 via bedrock-agentcore's BedrockAgentCoreApp (the same
/invocations + /ping contract AgentCore Runtime calls in the cloud). The model
is Bedrock Converse (or a stub). Every model-requested tool call goes through
agentware MiddlewareImpl -> KeiProxyEvaluator -> audit before it may execute.

The Kei decision comes from a LOCAL fake AuthorizationClient that mimics
kei-proxy's decision vocabulary; no real Kei/kei-proxy call is made.
"""

from __future__ import annotations

import base64
import json
import os
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[4] / "python" / "src"))  # repo checkout, read-only

from bedrock_agentcore.runtime import BedrockAgentCoreApp

from haikeilabs.agentware.kei.evaluator import KeiProxyEvaluator
from haikeilabs.agentware.llm.request import Message, Role, ToolDefinition
from haikeilabs.agentware.middleware.audit import AuditFilter
from haikeilabs.agentware.middleware import CallerContext, InMemoryAuditor, MiddlewareImpl
from haikeilabs.agentware.middleware.guardrails import ErrorTracker, retry_nudge
from haikeilabs.agentware.middleware.guardrails.error_tracker import ErrorCategory

from bedrock_backend import BedrockConverseBackend

app = BedrockAgentCoreApp()
AUDITOR = InMemoryAuditor()
ERRORS = ErrorTracker(max_errors_per_tool=3, window_duration_minutes=5)

TOOLS = [
    ToolDefinition(
        name="get_weather",
        description="Get the current weather for a city.",
        input_schema={"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]},
    ),
    ToolDefinition(
        name="github_delete_repo",
        description="Permanently delete a GitHub repository.",
        input_schema={
            "type": "object",
            "properties": {"owner": {"type": "string"}, "repo": {"type": "string"}},
            "required": ["owner", "repo"],
        },
    ),
]


class LocalToolExecutor:
    """agentware ToolExecutor: (result, success, error)."""

    def execute(self, tool_name: str, args: dict[str, Any]) -> tuple[Any, bool, str]:
        if tool_name == "get_weather":
            return ({"city": args.get("city"), "temp_c": 18, "sky": "clear"}, True, "")
        if tool_name == "github_delete_repo":
            return ({"deleted": f"{args.get('owner')}/{args.get('repo')}"}, True, "")  # must never run
        return (None, False, f"unknown tool {tool_name}")


class FakeKeiProxyClient:
    """Mimics `kei-proxy authorize` answers. Stand-in for KeiProxyAuthorizeClient /
    the daemon socket client; the real decision would come from a Kei policy bundle
    evaluated for the signed-in human's groups."""

    calls: list[dict[str, Any]] = []

    def authorize(self, user_id: str, tool: str, action: str, resource: str, **ctx: Any) -> dict[str, Any]:
        FakeKeiProxyClient.calls.append({"user_id": user_id, "tool": tool, "action": action, "resource": resource,
                                         "framework": ctx.get("framework"), "agent_id": ctx.get("agent_id")})
        if not user_id:
            return {"decision": "deny", "reason": "no human subject"}
        if tool.startswith("github_delete"):
            return {"decision": "deny", "reason": "destructive github action not granted", "policy_id": "pol-gh-ro"}
        if tool == "get_weather":
            return {"decision": "permit", "reason": "member may read weather", "policy_id": "pol-weather"}
        return {"decision": "deny", "reason": "no policy grants this tool"}


def human_subject(headers: dict[str, str] | None) -> str:
    """Policy subject = the signed-in human. In AgentCore with a JWT inbound
    authorizer the runtime has already validated the token; we only read `sub`.
    No fallback to the agent's workload identity: no human -> empty -> Kei denies."""
    auth = (headers or {}).get("Authorization", "")
    if not auth.startswith("Bearer "):
        return ""
    try:
        part = auth.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
        return str(claims.get("sub", ""))
    except Exception:
        return ""


@app.entrypoint
def invoke(payload: dict[str, Any], context: Any) -> dict[str, Any]:
    subject = human_subject(context.request_headers)
    caller = CallerContext(
        user_id=subject,
        invoking_subject=subject,
        session_id=context.session_id or "local",
        source="agentcore-runtime",
        framework="agentcore-runtime",
        agent_id="proto-agent",
    )
    mw = MiddlewareImpl(LocalToolExecutor(), evaluator=KeiProxyEvaluator(FakeKeiProxyClient()), auditor=AUDITOR)
    backend = BedrockConverseBackend(
        model_id=os.environ.get("MODEL_ID", "us.anthropic.claude-haiku-4-5-20251001-v1:0"),
        tools=TOOLS,
        thinking_budget=int(os.environ.get("THINKING_BUDGET", "0")),
    )
    msgs = [
        Message(role=Role.SYSTEM, content="You are a terse ops assistant. Use a tool when one fits; never ask follow-up questions."),
        Message(role=Role.USER, content=payload.get("prompt", "")),
    ]
    trace: list[dict[str, Any]] = []
    final, reasoning = "", []
    for _turn in range(4):
        resp = backend.complete(msgs)
        if resp.reasoning:
            reasoning.append(resp.reasoning)
        asst = Message(role=Role.ASSISTANT, content=resp.content, tool_calls=resp.tool_calls)
        backend.remember(asst, resp)
        msgs.append(asst)
        if not resp.tool_calls:
            final = resp.content
            break
        for call in resp.tool_calls:
            if ERRORS.should_block_tool(caller.session_id, call.name):
                result, ok, err = None, False, "blocked by error tracker"
            else:
                result, ok, err = mw.execute(call.name, call.arguments, caller, framework="agentcore-runtime")
            if not ok:
                ERRORS.record_error(caller.session_id, call.name, call.arguments, Exception(err),
                                    ErrorCategory.PERMISSION if "denied" in err else ErrorCategory.UNKNOWN)
            trace.append({"tool": call.name, "args": call.arguments, "ok": ok, "error": err, "result": result})
            msgs.append(Message(role=Role.TOOL, tool_call_id=call.id,
                                content=json.dumps({"ok": ok, "text": json.dumps(result) if ok else err})))
    else:
        final = retry_nudge("", [t.name for t in TOOLS]).content  # loop cap hit (illustrative)

    return {
        "subject": subject,
        "model": backend.model_name(),
        "stub": os.environ.get("BEDROCK_STUB") == "1",
        "final": final,
        "reasoning_chars": sum(len(r) for r in reasoning),
        "reasoning_preview": (reasoning[0][:160] if reasoning else ""),
        "tool_trace": trace,
        "kei_authorize_calls": FakeKeiProxyClient.calls[-len(trace):] if trace else [],
        "audit": [
            {"tool": r.tool_name, "subject": r.invoking_subject, "framework": r.framework,
             "action": r.decision.action.value, "reason": r.decision.reason}
            for r in AUDITOR.query(AuditFilter())
        ][-len(trace):] if trace else [],
    }


if __name__ == "__main__":
    app.run(port=8080, host="127.0.0.1")
