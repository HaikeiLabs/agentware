"""Offline check (no AWS): with Claude-style thinking enabled, the backend must
(1) send additionalModelRequestFields.thinking and (2) replay the signed
reasoningContent block verbatim on the turn that carries the toolResult."""
import json
from bedrock_backend import BedrockConverseBackend, StubConverseClient
from haikeilabs.agentware.llm.request import Message, Role, ToolDefinition

class Recording(StubConverseClient):
    calls = []
    def converse(self, **kw):
        Recording.calls.append(json.loads(json.dumps(kw)))
        return super().converse(**kw)

b = BedrockConverseBackend("us.anthropic.claude-haiku-4-5-20251001-v1:0",
    [ToolDefinition("get_weather", "w", {"type": "object", "properties": {"city": {"type": "string"}}})],
    client=Recording(), thinking_budget=1024)
msgs = [Message(role=Role.USER, content="weather in Paris?")]
r1 = b.complete(msgs)
a = Message(role=Role.ASSISTANT, content=r1.content, tool_calls=r1.tool_calls); b.remember(a, r1); msgs.append(a)
msgs.append(Message(role=Role.TOOL, tool_call_id=r1.tool_calls[0].id, content=json.dumps({"ok": True, "text": "18C"})))
r2 = b.complete(msgs)
first, second = Recording.calls
print("turn1 additionalModelRequestFields:", first.get("additionalModelRequestFields"))
asst = second["messages"][1]
print("turn2 replayed assistant block kinds:", [list(x)[0] for x in asst["content"]])
print("turn2 signature preserved:", asst["content"][0]["reasoningContent"]["reasoningText"]["signature"])
print("turn2 toolResult status:", second["messages"][2]["content"][0]["toolResult"]["status"])
print("agentware Response.reasoning:", repr(r1.reasoning))
# Counterfactual: without the side map, the agentware Message alone loses the reasoning block
b._raw_assistant.clear()
print("without side map, replayed kinds:", [list(x)[0] for x in b._to_converse(msgs)[1]["content"]])
