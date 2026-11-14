#!/usr/bin/env python3
"""Offline stand-in for the first-agent examples: a scripted model and Hacker News.

One local HTTP server answers both:

- POST /v1/chat/completions: an OpenAI-compatible scripted model. Until the
  conversation holds a tool result it asks for hn_top_stories; after that it
  returns FINAL_ANSWER, built from the titles in the tool result. A tool result
  without the fixture titles gets an answer that says so, which fails the CI
  assertion.
- GET /v0/topstories.json and /v0/item/<id>.json: Hacker News API fixtures
  from hn-stories.json.

The server binds 127.0.0.1 on a free port and writes the port to --port-file
once it is listening. It never calls the network.

Usage: fake-openai-hn.py --port-file PATH [--fixture PATH]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

FINAL_ANSWER = "Offline summary: Fixture story one leads today."
MISSING_ANSWER = "Offline summary: the tool result did not contain the fixture stories."

HERE = Path(__file__).resolve().parent


def model_reply(messages: list[dict[str, Any]]) -> dict[str, Any]:
    tool_results = [m for m in messages if m.get("role") == "tool"]
    if not tool_results:
        message: dict[str, Any] = {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "hn_top_stories", "arguments": "{}"},
                }
            ],
        }
        finish = "tool_calls"
    else:
        content = str(tool_results[-1].get("content", ""))
        ok = "Fixture story one" in content and "Fixture story five" in content
        message = {"role": "assistant", "content": FINAL_ANSWER if ok else MISSING_ANSWER}
        finish = "stop"
    return {
        "id": "chatcmpl-offline",
        "object": "chat.completion",
        "model": "offline-scripted",
        "choices": [{"index": 0, "message": message, "finish_reason": finish}],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


def make_handler(fixture: dict[str, Any]) -> type[BaseHTTPRequestHandler]:
    item_path = re.compile(r"^/v0/item/(\d+)\.json$")

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:
            sys.stderr.write("fake-openai-hn: " + (format % args) + "\n")

        def send_json(self, status: int, body: Any) -> None:
            data = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:
            if self.path == "/v0/topstories.json":
                self.send_json(200, fixture["topstories"])
                return
            match = item_path.match(self.path)
            if match and match.group(1) in fixture["items"]:
                self.send_json(200, fixture["items"][match.group(1)])
                return
            self.send_json(404, {"error": "not found"})

        def do_POST(self) -> None:
            if self.path != "/v1/chat/completions":
                self.send_json(404, {"error": "not found"})
                return
            length = int(self.headers.get("Content-Length", "0"))
            request = json.loads(self.rfile.read(length) or b"{}")
            self.send_json(200, model_reply(request.get("messages", [])))

    return Handler


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port-file", required=True)
    parser.add_argument("--fixture", default=str(HERE / "hn-stories.json"))
    args = parser.parse_args()

    fixture = json.loads(Path(args.fixture).read_text(encoding="utf-8"))
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(fixture))
    tmp = args.port_file + ".tmp"
    Path(tmp).write_text(str(server.server_address[1]), encoding="utf-8")
    os.replace(tmp, args.port_file)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
