"""Tests for the live-capture script against a stub model server.

Run from the repository root (``capture`` reads the profiles with PyYAML):

    uv run --project python --extra dev python -m unittest discover -s scripts/live-captures -p '*_test.py'
"""

from __future__ import annotations

import http.server
import json
import tempfile
import threading
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

import capture

PROFILES = """\
profiles:
  stub-glm:
    backend: vllm
    model: stub/glm
    base_url_env: STUB_GLM_BASE_URL
    temperature: 0
    seed: 42
    thinking: true
  stub-qwen:
    backend: llamacpp
    model: stub/qwen
    base_url_env: STUB_QWEN_BASE_URL
    temperature: 0.6
    top_p: 0.95
    top_k: 20
    seed: 42
    thinking: true
"""

PROMPTS = {
    "max_tokens": 64,
    "profiles": {
        "stub-glm": {"toolformat_family": "glm", "reasoning_family": "glm"},
        "stub-qwen": {"toolformat_family": "openai", "text_toolformat_family": "qwen", "reasoning_family": "qwen"},
    },
    "prompts": [
        {"id": "single-tool-call", "tools": True, "messages": [{"role": "user", "content": "weather in Tokyo"}]},
        {"id": "parallel-tool-calls", "tools": True, "messages": [{"role": "user", "content": "Paris and London"}]},
        {"id": "reasoning-no-tools", "tools": False, "messages": [{"role": "user", "content": "17*23"}]},
    ],
}


def tool_call(call_id: str, name: str, args: dict[str, Any]) -> dict[str, Any]:
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}


def stub_response(payload: dict[str, Any]) -> dict[str, Any]:
    """A canned response shaped like the real servers' (vLLM GLM, llama.cpp Qwen)."""
    prompt = payload["messages"][-1]["content"]
    message: dict[str, Any] = {"role": "assistant", "content": None}
    if payload["model"] == "stub/glm":
        message["reasoning"] = f"The user asked: {prompt}."
    else:
        message["reasoning_content"] = f"The user asked: {prompt}."
    if "Tokyo" in prompt:
        message["tool_calls"] = [tool_call("chatcmpl-tool-1", "get_weather", {"location": "Tokyo"})]
    elif "Paris" in prompt:
        message["tool_calls"] = [
            tool_call("chatcmpl-tool-2", "get_weather", {"location": "Paris", "unit": "celsius"}),
            tool_call("chatcmpl-tool-3", "get_weather", {"location": "London", "unit": "celsius"}),
        ]
        if payload["model"] == "stub/qwen":
            # llama.cpp without a tool parser leaves the markup in content.
            message.pop("tool_calls")
            message["content"] = '<tool_call><tool name="get_weather">{"location":"Paris"}</tool></tool_call>'
    else:
        message["content"] = "391"
    return {"id": "chatcmpl-stub", "object": "chat.completion", "model": payload["model"], "choices": [{"index": 0, "message": message, "finish_reason": "stop"}]}


class StubModelServer:
    """An OpenAI-compatible /v1/chat/completions stub that records requests."""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                outer.requests.append(body)
                data = json.dumps(stub_response(body)).encode()
                self.send_response(200 if self.path == "/v1/chat/completions" else 404)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args: Any) -> None:
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base_url = f"http://127.0.0.1:{self.server.server_address[1]}/v1"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


def run_capture(root: Path, server: StubModelServer, *extra: str) -> int:
    (root / "profiles.yaml").write_text(PROFILES, encoding="utf-8")
    (root / "prompts.json").write_text(json.dumps(PROMPTS), encoding="utf-8")
    env = {"STUB_GLM_BASE_URL": server.base_url, "STUB_QWEN_BASE_URL": server.base_url}
    argv = ["capture", "--out", str(root / "out"), "--profiles", str(root / "profiles.yaml"), "--prompts", str(root / "prompts.json"), *extra]
    with mock.patch.dict("os.environ", env):
        return capture.main(argv)


class CaptureTest(unittest.TestCase):
    def setUp(self) -> None:
        self.server = StubModelServer()
        self.addCleanup(self.server.close)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)

    def test_requests_use_the_profile_sampling(self) -> None:
        self.assertEqual(run_capture(self.root, self.server, "--profile", "stub-qwen"), 0)
        first = self.server.requests[0]
        self.assertEqual(first["model"], "stub/qwen")
        self.assertEqual((first["temperature"], first["top_p"], first["top_k"], first["seed"]), (0.6, 0.95, 20, 42))
        self.assertEqual(first["chat_template_kwargs"], {"enable_thinking": True, "thinking": True})
        self.assertEqual([t["function"]["name"] for t in first["tools"]], ["get_weather", "search_web"])
        self.assertNotIn("tools", self.server.requests[2], "a prompt without tools sends none")

    def test_writes_raw_responses_and_candidate_fixtures(self) -> None:
        self.assertEqual(run_capture(self.root, self.server), 0)
        out = self.root / "out"
        raw = json.loads((out / "raw" / "stub-glm" / "single-tool-call.json").read_text())
        self.assertEqual(raw["response"]["choices"][0]["message"]["tool_calls"][0]["id"], "chatcmpl-tool-1")
        self.assertNotIn("127.0.0.1", json.dumps(raw), "the endpoint URL is never recorded")

        glm = json.loads((out / "candidates" / "toolformat" / "live-stub-glm-cases.json").read_text())
        self.assertEqual(glm["family"], "glm")
        self.assertLessEqual({"format_definitions", "parse_calls", "format_result", "validate_format"}, set(glm))
        self.assertEqual([c["name"] for c in glm["parse_calls"]], ["single-tool-call", "parallel-tool-calls"])
        flat = json.loads(glm["parse_calls"][0]["input"])
        self.assertEqual(flat, [{"id": "chatcmpl-tool-1", "name": "get_weather", "arguments": '{"location": "Tokyo"}'}])
        self.assertNotIn("expected", glm["parse_calls"][0], "expected values come from Go")

        text = json.loads((out / "candidates" / "toolformat" / "live-stub-qwen-text-cases.json").read_text())
        self.assertEqual(text["family"], "qwen")
        self.assertEqual([c["name"] for c in text["parse_calls"]], ["parallel-tool-calls (content)"])

        reasoning = json.loads((out / "candidates" / "reasoning" / "live-stub-qwen-cases.json").read_text())
        self.assertEqual(len(reasoning["examples"]), 3)
        self.assertEqual(reasoning["examples"][0]["raw_response"]["id"], "chatcmpl-stub")

    def test_unreachable_endpoint_exits_2(self) -> None:
        self.server.base_url = "http://127.0.0.1:9/v1"
        self.assertEqual(run_capture(self.root, self.server, "--profile", "stub-glm"), 2)

    def test_unset_base_url_exits_2(self) -> None:
        (self.root / "profiles.yaml").write_text(PROFILES, encoding="utf-8")
        (self.root / "prompts.json").write_text(json.dumps(PROMPTS), encoding="utf-8")
        with mock.patch.dict("os.environ", {}, clear=True):
            code = capture.main(["capture", "--out", str(self.root / "out"), "--profiles", str(self.root / "profiles.yaml"), "--prompts", str(self.root / "prompts.json")])
        self.assertEqual(code, 2)


class StageTest(unittest.TestCase):
    def test_refuses_to_overwrite_and_cleans_up(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "fixtures" / "toolformat").mkdir(parents=True)
            (root / "c" / "toolformat").mkdir(parents=True)
            a = root / "c" / "toolformat" / "live-a-cases.json"
            b = root / "c" / "toolformat" / "live-b-cases.json"
            a.write_text("{}")
            b.write_text("{}")
            (root / "fixtures" / "toolformat" / "live-b-cases.json").write_text("keep")
            with self.assertRaises(FileExistsError):
                capture.stage([a, b], root / "fixtures")
            self.assertFalse((root / "fixtures" / "toolformat" / "live-a-cases.json").exists())
            self.assertEqual((root / "fixtures" / "toolformat" / "live-b-cases.json").read_text(), "keep")


if __name__ == "__main__":
    unittest.main()
