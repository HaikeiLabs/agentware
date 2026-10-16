"""Probe: what Converse reasoningContent looks like for DeepSeek-R1, and whether
agentware's ReasoningAdapter understands the raw Converse message shape."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[4] / "python" / "src"))  # repo checkout, read-only
import boto3
from haikeilabs.agentware.reasoning.adapter import ReasoningAdapter

c = boto3.client("bedrock-runtime", region_name="us-east-1")
out = c.converse(modelId="us.deepseek.r1-v1:0",
                 messages=[{"role": "user", "content": [{"text": "Is 91 prime? One line."}]}],
                 inferenceConfig={"maxTokens": 800})
msg = out["output"]["message"]
kinds = [list(b.keys())[0] for b in msg["content"]]
print("stopReason:", out["stopReason"], "| block kinds:", kinds)
for b in msg["content"]:
    if "reasoningContent" in b:
        rt = b["reasoningContent"].get("reasoningText", {})
        print("reasoningText chars:", len(rt.get("text", "")), "| signature present:", "signature" in rt)
    if "text" in b:
        print("text:", b["text"].strip()[:120])
a = ReasoningAdapter()
try:
    print("adapter on raw Converse message ->", a.parse(msg, model="deepseek-r1"))
except Exception as e:
    print("adapter on raw Converse message -> ERROR", type(e).__name__, e)
# normalized: flatten Converse blocks into the OpenAI-style shape the adapter knows
flat = {"content": "".join(b.get("text", "") for b in msg["content"]),
        "reasoning_content": "".join(b.get("reasoningContent", {}).get("reasoningText", {}).get("text", "") for b in msg["content"])}
clean, tree = a.parse(flat, model="deepseek-r1")
print("adapter on flattened ->", "format:", tree.format if hasattr(tree, "format") else "?", "| nodes:", len(getattr(tree, "nodes", []) or []), "| clean:", clean.strip()[:80])
