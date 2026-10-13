"""MCP stdio client for the Go wiki-memory core."""

import json
import os
import subprocess
from dataclasses import dataclass, field
from typing import Any

MEMORY_TOOLS = [
    "memory_ingest",
    "memory_write_page",
    "memory_query",
    "memory_get_claims",
    "memory_lint",
]

DIAGNOSTICS_MARKER = "diagnostics: "


class MemoryServerError(RuntimeError):
    """Transport or JSON-RPC failure talking to the memory server."""


@dataclass
class DiagnosticViolation:
    """Structured ontology violation from a DENY decision."""

    constraint: str
    term: str
    message: str
    nearest: list[str] = field(default_factory=list)


def parse_diagnostics(error_text: str) -> list[DiagnosticViolation]:
    """Extract structured violations from a deny reason, if present."""
    idx = error_text.find(DIAGNOSTICS_MARKER)
    if idx < 0:
        return []
    payload = error_text[idx + len(DIAGNOSTICS_MARKER) :]
    try:
        raw = json.loads(payload)
    except json.JSONDecodeError:
        return []
    return [
        DiagnosticViolation(
            constraint=v.get("constraint", ""),
            term=v.get("term", ""),
            message=v.get("message", ""),
            nearest=v.get("nearest", []) or [],
        )
        for v in raw
    ]


class WikiMemory:
    """Composable wiki-memory enablement for one user.

    Spawns ``memctl serve`` (the Go core) as a subprocess speaking MCP over
    stdio. The subprocess is scoped to ``user_id`` for its lifetime; the
    server's policy tier denies any in-band attempt to change scope.

    Implements the ToolExecutor protocol
    (``execute(tool_name, args) -> (result, success, error)``), so it can be
    passed to ``MiddlewareImpl`` directly::

        memory = WikiMemory(user_id="alice", root="/var/memory",
                            tbox_paths=[...])
        mw = MiddlewareImpl(executor=memory).with_auditor(auditor)

    # Providing Your Own Ontology

    The T-box (schema) is fully composable — pass paths to your own TTL files.
    Multiple files are merged. Use absolute paths or paths relative to your
    working directory.

    Example TTL file (my-ontology.ttl)::

        @prefix ex: <http://example.org/> .
        @prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
        @prefix owl: <http://www.w3.org/2002/07/owl#> .

        ex:Project a owl:Class ;
            rdfs:label "Project"@en ;
            rdfs:comment "A software project" .

        ex:Task a owl:Class ;
            rdfs:label "Task"@en ;
            rdfs:subClassOf ex:Project .

        ex:hasStatus a owl:ObjectProperty ;
            rdfs:domain ex:Project ;
            rdfs:range ex:Status .

        ex:Status a owl:Class ;
            rdfs:label "Status"@en .

    Example usage::

        wiki = WikiMemory(
            user_id="alice",
            root="./memory",
            tbox_paths=[
                "./my-ontology.ttl",    # your custom ontology
                "./shared-skills.ttl",  # another TTL file
            ],
        )

    The repo includes example ontologies in ``ontologies/``:
        - ontologies/education/TBOX_LEARNING_SOFTWARE.ttl (learning software concepts)
        - ontologies/social/twitch_topics.ttl (SKOS concepts)
        - ontologies/thesaurus/*.ttl (SKOS testing)
    """

    def __init__(
        self,
        user_id: str,
        root: str,
        tbox_paths: list[str],
        memctl_path: str | None = None,
        session_id: str = "python-sdk",
    ) -> None:
        if not user_id:
            raise ValueError("user_id is required")
        if not tbox_paths:
            raise ValueError("tbox_paths is required (the T-box is a parameter)")
        self.user_id = user_id
        binary = memctl_path or os.environ.get("PEDRO_MEMCTL", "memctl")
        cmd = [binary, "serve", "-root", root, "-user", user_id, "-session", session_id]
        for path in tbox_paths:
            cmd.extend(["-tbox", path])
        try:
            self._proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
        except OSError as exc:
            raise MemoryServerError(f"failed to spawn {binary}: {exc}") from exc
        self._next_id = 0
        self._request("initialize", {})
        self._notify("notifications/initialized")

    # -- MCP transport -------------------------------------------------

    def _send(self, message: dict[str, Any]) -> None:
        proc = self._proc
        if proc.stdin is None or proc.poll() is not None:
            raise MemoryServerError(self._death_reason())
        proc.stdin.write(json.dumps(message) + "\n")
        proc.stdin.flush()

    def _notify(self, method: str) -> None:
        self._send({"jsonrpc": "2.0", "method": method})

    def _request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self._next_id += 1
        self._send({"jsonrpc": "2.0", "id": self._next_id, "method": method, "params": params})
        stdout = self._proc.stdout
        if stdout is None:
            raise MemoryServerError("server stdout closed")
        line = stdout.readline()
        if not line:
            raise MemoryServerError(self._death_reason())
        response: dict[str, Any] = json.loads(line)
        if "error" in response and response["error"] is not None:
            err = response["error"]
            raise MemoryServerError(f"rpc {err.get('code')}: {err.get('message')}")
        result: dict[str, Any] = response.get("result", {})
        return result

    def _death_reason(self) -> str:
        stderr = ""
        if self._proc.stderr is not None and self._proc.poll() is not None:
            stderr = self._proc.stderr.read().strip()
        return f"memory server exited (code={self._proc.poll()}): {stderr}"

    # -- ToolExecutor protocol -----------------------------------------

    def execute(self, tool_name: str, args: dict[str, Any]) -> tuple[Any, bool, str]:
        """Execute a memory tool. Returns (output_text, success, error)."""
        result = self._request("tools/call", {"name": tool_name, "arguments": args})
        blocks = result.get("content", [])
        text = "".join(b.get("text", "") for b in blocks if b.get("type") == "text")
        if result.get("isError"):
            return None, False, text
        return text, True, ""

    # -- Native API ----------------------------------------------------

    def tools(self) -> list[dict[str, Any]]:
        """List the memory tools served by the core."""
        result = self._request("tools/list", {})
        tools: list[dict[str, Any]] = result.get("tools", [])
        return tools

    def ingest(self, source_id: str, text: str) -> tuple[Any, bool, str]:
        return self.execute("memory_ingest", {"source_id": source_id, "text": text})

    def write_page(self, content: str) -> tuple[Any, bool, str]:
        return self.execute("memory_write_page", {"content": content})

    def query(self, question: str) -> tuple[Any, bool, str]:
        output, ok, err = self.execute("memory_query", {"question": question})
        return (json.loads(output) if ok and output else output), ok, err

    def get_claims(self, page_id: str | None = None) -> tuple[Any, bool, str]:
        args: dict[str, Any] = {}
        if page_id:
            args["page_id"] = page_id
        output, ok, err = self.execute("memory_get_claims", args)
        return (json.loads(output) if ok and output else output), ok, err

    def lint(self) -> tuple[Any, bool, str]:
        output, ok, err = self.execute("memory_lint", {})
        return (json.loads(output) if ok and output else output), ok, err

    # -- Lifecycle -----------------------------------------------------

    def close(self) -> None:
        """Terminate the memory server subprocess."""
        if self._proc.poll() is None:
            if self._proc.stdin is not None:
                self._proc.stdin.close()
            try:
                self._proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._proc.kill()
                self._proc.wait(timeout=5)

    def __enter__(self) -> "WikiMemory":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()
