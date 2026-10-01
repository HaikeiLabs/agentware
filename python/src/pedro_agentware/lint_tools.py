"""Shared rules for validating Kei tool manifest v2 exports."""

import json
import re
import sys
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parents[3]
_CAPABILITIES = json.loads((_ROOT / "fixtures/kei/connector-capabilities.v0.5.0.json").read_text())
_IDENTIFIER = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]*$")
_RETIRED = {"allow", "approval", "approval_id", "approval_required", "requires_approval"}


def lint_manifest(raw: str) -> list[str]:
    """Return deterministic diagnostics for a v2 manifest string."""
    errors: list[str] = []
    try:
        manifest = json.loads(raw)
    except json.JSONDecodeError as exc:
        return [f"invalid JSON: {exc.msg}"]
    if not isinstance(manifest, dict) or manifest.get("schema") != "kei.tool-manifest/v2":
        return ["schema must be kei.tool-manifest/v2"]
    tools = manifest.get("tools")
    if not isinstance(tools, list):
        return ["tools must be an array"]
    seen: set[str] = set()
    for index, tool in enumerate(tools):
        prefix = f"tools[{index}]"
        if not isinstance(tool, dict):
            errors.append(f"{prefix} must be an object")
            continue
        name = tool.get("name")
        if isinstance(name, str):
            if name in seen:
                errors.append(f"duplicate tool name: {name}")
            seen.add(name)
        source, caps = tool.get("source"), tool.get("required_capabilities")
        if not isinstance(source, str) or not source:
            errors.append(f"{prefix} is missing source")
        if not isinstance(caps, list) or not caps:
            errors.append(f"{prefix} is missing required_capabilities")
        elif isinstance(source, str):
            if caps != sorted(caps, key=str):
                errors.append(f"{prefix} required_capabilities are not sorted")
            for cap in caps:
                if cap not in _CAPABILITIES.get(source, []):
                    errors.append(
                        f"{prefix} capability {cap!r} is not declared for source {source!r}"
                    )
        resources = tool.get("resource_types", [])
        if not isinstance(resources, list):
            errors.append(f"{prefix}.resource_types must be an array")
        else:
            normalized_resources = sorted(
                resources,
                key=lambda item: (
                    (str(item.get("type", "")), str(item.get("parent_type", "")))
                    if isinstance(item, dict)
                    else ("", "")
                ),
            )
            if resources != normalized_resources:
                errors.append(f"{prefix} resource_types are not sorted")
            for resource in resources:
                if not isinstance(resource, dict):
                    errors.append(f"{prefix} resource type must be an object")
                    continue
                for key in ("type", "parent_type"):
                    value = resource.get(key)
                    if key == "parent_type" and value is None:
                        continue
                    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
                        errors.append(f"{prefix} invalid resource type {key}: {value!r}")
        if tool.get("operation_class") not in ("read", "write"):
            errors.append(f"{prefix} operation_class must be read or write")
        if _has_retired_field(tool):
            errors.append(f"{prefix} contains allow or a retired approval field")
    names = [item.get("name", "") for item in tools if isinstance(item, dict)]
    if names != sorted(names):
        errors.append("tools are not sorted by name")
    return sorted(errors)


def _has_retired_field(value: Any) -> bool:
    if isinstance(value, dict):
        return any(
            key.lower() in _RETIRED or "approval" in key.lower() or _has_retired_field(child)
            for key, child in value.items()
        )
    if isinstance(value, list):
        return any(_has_retired_field(child) for child in value)
    if isinstance(value, str) and value.lower() == "allow":
        return True
    return False


def main() -> int:
    if len(sys.argv) != 2:
        print(
            json.dumps(
                {
                    "valid": False,
                    "errors": ["usage: python -m pedro_agentware.lint_tools MANIFEST.json"],
                }
            )
        )
        return 1
    try:
        raw = Path(sys.argv[1]).read_text(encoding="utf-8")
        errors = lint_manifest(raw)
    except OSError as exc:
        errors = [str(exc)]
    print(json.dumps({"valid": not errors, "errors": errors}, ensure_ascii=False))
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
