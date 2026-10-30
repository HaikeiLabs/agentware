#!/usr/bin/env python3
"""Live model captures and the cross-language drift check (docs/testing.md).

Two steps, run by .github/workflows/live-captures.yml and runnable locally:

``capture``
    Sends the fixed prompts in prompts.json to each model profile in
    evals/model-profiles.yaml (the profile's sampling, seed and thinking
    setting on every request) and writes, under ``--out``:

    - ``raw/<profile>/<prompt>.json``: the request (without the endpoint URL)
      and the server's response, verbatim;
    - ``candidates/toolformat/live-<profile>-cases.json`` and
      ``candidates/reasoning/live-<profile>-cases.json``: the responses as
      fixture cases in the shape of fixtures/toolformat and
      fixtures/reasoning, without expected values.

``compare``
    Fills the expected values from Go, the reference implementation
    (``TestFillLiveCaptures`` in go/toolformat and go/reasoning), stages the
    filled candidates next to the shared fixtures, runs every language's
    fixture tests and removes the staged files again. A language whose
    fixture tests fail disagrees with Go on a captured response, and the step
    exits 1. It writes ``report.json``, ``summary.md`` and one log per
    language under ``--captures``.

Captures are candidates: nothing here commits them. To adopt one, copy the
case into the fixture file and say where it came from (see docs/testing.md,
"Adding a fixture case").

Usage:
    python3 scripts/live-captures/capture.py capture --out _captures [--profile glm-5.3-flash]
    python3 scripts/live-captures/capture.py compare --captures _captures

``capture`` needs PyYAML (it reads the profiles with python/src/evals).
``compare`` needs the toolchains of the languages it runs.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
FAMILIES = ("toolformat", "reasoning")

# Each language's shared-fixture runners, the same tests conformance.yml runs.
LANGUAGES: dict[str, tuple[str, list[str]]] = {
    "go": ("go", ["go", "test", "-count=1", "-run", "^(TestToolformatFixtures|TestReasoningFixtures)$", "./toolformat/", "./reasoning/"]),
    "python": ("python", ["uv", "run", "--extra", "dev", "pytest", "-q", "tests/toolformat_fixtures_test.py", "tests/reasoning_fixtures_test.py"]),
    "typescript": ("typescript", ["node", "--experimental-vm-modules", "node_modules/jest/bin/jest.js", "toolformat-reasoning-fixtures"]),
    "rust": (".", ["cargo", "test", "--manifest-path", "rust/Cargo.toml", "-p", "agentware-kei", "--test", "toolformat_fixtures", "--test", "reasoning_fixtures"]),
}


class CaptureError(RuntimeError):
    """A model endpoint did not answer; the capture is incomplete, not drift."""


# ---------------------------------------------------------------------------
# capture
# ---------------------------------------------------------------------------


def load_tools(path: Path) -> list[dict[str, Any]]:
    """The shared tool definitions as OpenAI-style function tools."""
    tools = json.loads(path.read_text(encoding="utf-8"))["tools"]
    return [
        {
            "type": "function",
            "function": {"name": t["name"], "description": t["description"], "parameters": t["input_schema"]},
        }
        for t in tools
    ]


def build_request(profile: Any, prompt: dict[str, Any], tools: list[dict[str, Any]], max_tokens: int) -> dict[str, Any]:
    """The chat-completions payload, sampled the way the eval runner samples."""
    from evals.models import thinking_template_kwargs

    payload: dict[str, Any] = {
        "model": profile.model,
        "messages": prompt["messages"],
        "temperature": profile.temperature,
        "max_tokens": max_tokens,
    }
    if profile.seed is not None:
        payload["seed"] = profile.seed
    if profile.top_p is not None:
        payload["top_p"] = profile.top_p
    if profile.top_k is not None:
        payload["top_k"] = profile.top_k
    if profile.thinking is not None:
        payload["chat_template_kwargs"] = thinking_template_kwargs(profile.thinking)
    if prompt.get("tools"):
        payload["tools"] = tools
    return payload


def post(base_url: str, payload: dict[str, Any], timeout: float) -> dict[str, Any]:
    req = urllib.request.Request(
        f"{base_url}/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        # The message names the profile, never the URL: captures are uploaded
        # from a public repository.
        raise CaptureError(f"{payload['model']}: chat/completions failed: {exc.__class__.__name__}") from exc


def message_of(response: dict[str, Any]) -> dict[str, Any]:
    choices = response.get("choices") or [{}]
    message = choices[0].get("message") if isinstance(choices[0], dict) else None
    return message if isinstance(message, dict) else {}


def toolformat_cases(prompt_id: str, message: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Cases for the structured-call family and for the text-call family.

    Structured tool_calls are flattened to {id, name, arguments} with
    arguments left as the server sent them (a JSON string), as the GLM-5.3
    cases are. Content that holds <tool_call> markup is a text-call case.
    """
    structured: list[dict[str, Any]] = []
    text: list[dict[str, Any]] = []

    calls = message.get("tool_calls") or []
    if calls:
        flat = []
        for call in calls:
            fn = call.get("function") or {}
            entry = {"name": fn.get("name"), "arguments": fn.get("arguments")}
            if call.get("id"):
                entry = {"id": call["id"], **entry}
            flat.append(entry)
        payload = json.dumps(flat, ensure_ascii=False, separators=(",", ":"))
        structured.append({"section": "parse_calls", "name": prompt_id, "input": payload})
        structured.append({"section": "validate_format", "name": prompt_id, "input": payload})

    content = message.get("content")
    if isinstance(content, str) and content.strip():
        structured.append({"section": "validate_format", "name": f"{prompt_id} (content)", "input": content})
        if "<tool_call>" in content:
            text.append({"section": "parse_calls", "name": f"{prompt_id} (content)", "input": content})
            text.append({"section": "validate_format", "name": f"{prompt_id} (content)", "input": content})
    return structured, text


def toolformat_fixture(family: str, description: str, source: dict[str, Any], cases: list[dict[str, Any]]) -> dict[str, Any]:
    fx: dict[str, Any] = {
        "family": family,
        "description": description,
        "source": source,
        "format_definitions": [],
        "parse_calls": [],
        "format_result": [],
        "validate_format": [],
    }
    for case in cases:
        fx[case["section"]].append({"name": case["name"], "input": case["input"]})
    return fx


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def capture_profile(
    name: str,
    profile: Any,
    settings: dict[str, Any],
    prompts: dict[str, Any],
    tools: list[dict[str, Any]],
    out: Path,
    timeout: float,
    environ: dict[str, str],
) -> dict[str, Any]:
    base_url = profile.base_url(environ)
    captured_at = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    source = {
        "profile": name,
        "model": profile.model,
        "backend": profile.backend.value,
        "captured_at": captured_at,
        "temperature": profile.temperature,
        "top_p": profile.top_p,
        "top_k": profile.top_k,
        "seed": profile.seed,
        "thinking": profile.thinking,
    }

    structured: list[dict[str, Any]] = []
    text: list[dict[str, Any]] = []
    examples: list[dict[str, Any]] = []
    for prompt in prompts["prompts"]:
        payload = build_request(profile, prompt, tools, prompts.get("max_tokens", 2048))
        started = time.monotonic()
        response = post(base_url, payload, timeout)
        elapsed_ms = int((time.monotonic() - started) * 1000)
        write_json(
            out / "raw" / name / f"{prompt['id']}.json",
            {"source": source, "prompt": prompt["id"], "elapsed_ms": elapsed_ms, "request": payload, "response": response},
        )
        s, t = toolformat_cases(prompt["id"], message_of(response))
        structured += s
        text += t
        examples.append({"name": prompt["id"], "raw_response": response})

    description = f"Live capture from {profile.model} ({profile.backend.value}, profile {name}) at {captured_at}. Candidate only: expected values come from Go."
    candidates = out / "candidates"
    family = settings.get("toolformat_family", "openai")
    if structured:
        write_json(candidates / "toolformat" / f"live-{name}-cases.json", toolformat_fixture(family, description, source, structured))
    text_family = settings.get("text_toolformat_family")
    if text and text_family:
        write_json(candidates / "toolformat" / f"live-{name}-text-cases.json", toolformat_fixture(text_family, description, source, text))
    write_json(
        candidates / "reasoning" / f"live-{name}-cases.json",
        {"family": settings.get("reasoning_family", name), "description": description, "source": source, "examples": examples},
    )
    return {"profile": name, "prompts": len(examples), "toolformat_cases": len(structured) + len(text)}


def cmd_capture(args: argparse.Namespace) -> int:
    sys.path.insert(0, str(REPO / "python" / "src"))
    from evals.profiles import ProfileError, load_profiles

    prompts = json.loads(args.prompts.read_text(encoding="utf-8"))
    tools = load_tools(args.tools)
    try:
        profiles = load_profiles(args.profiles)
    except ProfileError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    names = args.profile or sorted(profiles)
    unknown = [n for n in names if n not in profiles]
    if unknown:
        print(f"error: unknown profile(s) {unknown}; known: {sorted(profiles)}", file=sys.stderr)
        return 1

    results = []
    for name in names:
        settings = prompts.get("profiles", {}).get(name, {})
        try:
            results.append(capture_profile(name, profiles[name], settings, prompts, tools, args.out, args.timeout, dict(os.environ)))
        except (ProfileError, CaptureError) as exc:
            # Exit 2, as the eval runner does: unreachable is never drift.
            print(f"error: {name}: {exc}", file=sys.stderr)
            return 2
    for r in results:
        print(f"{r['profile']}: {r['prompts']} prompt(s), {r['toolformat_cases']} toolformat case(s)")
    return 0


# ---------------------------------------------------------------------------
# compare
# ---------------------------------------------------------------------------


def run(cmd: list[str], cwd: Path, env: dict[str, str], log: Path) -> int:
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("w", encoding="utf-8") as fh:
        fh.write(f"$ (cd {cwd.relative_to(REPO) if cwd.is_relative_to(REPO) else cwd} && {' '.join(cmd)})\n")
        fh.flush()
        try:
            return subprocess.run(cmd, cwd=cwd, env=env, stdout=fh, stderr=subprocess.STDOUT, check=False).returncode
        except FileNotFoundError as exc:
            fh.write(f"{exc}\n")
            return 127


def candidate_files(candidates: Path) -> list[Path]:
    return sorted(p for family in FAMILIES for p in (candidates / family).glob("*-cases.json"))


def fill_from_go(candidates: Path, logs: Path) -> int:
    env = {**os.environ, "AGENTWARE_LIVE_CAPTURE_DIR": str(candidates.resolve())}
    cmd = ["go", "test", "-count=1", "-v", "-run", "^TestFillLiveCaptures$", "./toolformat/", "./reasoning/"]
    return run(cmd, REPO / "go", env, logs / "go-fill.log")


def stage(files: list[Path], fixtures: Path) -> list[Path]:
    """Copies the candidates next to the shared fixtures, refusing to overwrite."""
    staged: list[Path] = []
    try:
        for src in files:
            dest = fixtures / src.parent.name / src.name
            if dest.exists():
                raise FileExistsError(f"{dest} already exists; remove it or rename the capture")
            shutil.copyfile(src, dest)
            staged.append(dest)
    except Exception:
        unstage(staged)
        raise
    return staged


def unstage(staged: list[Path]) -> None:
    for path in staged:
        path.unlink(missing_ok=True)


def summarize(report: dict[str, Any]) -> str:
    lines = ["## Live model captures: cross-language drift", ""]
    lines.append("| Candidate fixture | Cases | Rejected by Go |")
    lines.append("| --- | --- | --- |")
    for c in report["candidates"]:
        lines.append(f"| `{c['file']}` | {c['cases']} | {c['go_rejected']} |")
    lines += ["", "| Language | Result |", "| --- | --- |"]
    for lang, result in report["languages"].items():
        mark = "agrees with Go" if result == "pass" else f"**{result}** (see `logs/{lang}.log`)"
        lines.append(f"| {lang} | {mark} |")
    lines += ["", f"**{'DRIFT' if report['drift'] else 'No drift'}**", ""]
    return "\n".join(lines)


def cmd_compare(args: argparse.Namespace) -> int:
    captures: Path = args.captures
    candidates = captures / "candidates"
    logs = captures / "logs"
    files = candidate_files(candidates)
    if not files:
        print(f"error: no candidate fixtures under {candidates}", file=sys.stderr)
        return 1

    if fill_from_go(candidates, logs) != 0:
        print(f"error: Go could not fill the candidates; see {logs / 'go-fill.log'}", file=sys.stderr)
        return 1

    report: dict[str, Any] = {"candidates": [], "languages": {}}
    for path in files:
        fx = json.loads(path.read_text(encoding="utf-8"))
        sections = ("examples",) if path.parent.name == "reasoning" else ("parse_calls", "validate_format")
        report["candidates"].append(
            {
                "file": f"{path.parent.name}/{path.name}",
                "cases": sum(len(fx.get(s) or []) for s in sections),
                "go_rejected": len(fx.get("go_rejected") or []),
            }
        )

    staged = stage(files, REPO / "fixtures")
    try:
        for lang in args.languages:
            cwd, cmd = LANGUAGES[lang]
            code = run(cmd, REPO / cwd, dict(os.environ), logs / f"{lang}.log")
            report["languages"][lang] = "pass" if code == 0 else f"fail (exit {code})"
            print(f"{lang}: {report['languages'][lang]}")
    finally:
        unstage(staged)

    report["drift"] = any(r != "pass" for r in report["languages"].values())
    write_json(captures / "report.json", report)
    summary = summarize(report)
    (captures / "summary.md").write_text(summary, encoding="utf-8")
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as fh:
            fh.write(summary)
    print(summary)
    return 1 if report["drift"] else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)

    cap = sub.add_parser("capture", help="send the fixed prompts to the live models")
    cap.add_argument("--out", type=Path, required=True)
    cap.add_argument("--profile", action="append", help="profile to capture (repeatable; default: every profile)")
    cap.add_argument("--profiles", type=Path, default=REPO / "evals" / "model-profiles.yaml")
    cap.add_argument("--prompts", type=Path, default=HERE / "prompts.json")
    cap.add_argument("--tools", type=Path, default=REPO / "fixtures" / "toolformat" / "tools.json")
    cap.add_argument("--timeout", type=float, default=300.0, help="seconds per request")
    cap.set_defaults(func=cmd_capture)

    cmp = sub.add_parser("compare", help="fill expected values from Go and replay them in every language")
    cmp.add_argument("--captures", type=Path, required=True)
    cmp.add_argument("--languages", type=lambda s: s.split(","), default=list(LANGUAGES))
    cmp.set_defaults(func=cmd_compare)

    args = parser.parse_args(argv)
    if args.command == "compare":
        unknown = [lang for lang in args.languages if lang not in LANGUAGES]
        if unknown:
            parser.error(f"unknown language(s) {unknown}; known: {list(LANGUAGES)}")
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
