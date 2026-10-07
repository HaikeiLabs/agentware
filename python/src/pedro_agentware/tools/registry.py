"""Tool registry and deterministic Kei tool-manifest exporters."""

import json
from typing import Any, TypedDict

from .tool import GovernedTool


class KeiToolRegistration(TypedDict, total=False):
    """Trusted route metadata attached to the registry dispatch entry."""

    service: str
    source: str
    operation_class: str
    route: dict[str, dict[str, str]]
    required_capabilities: list[str]
    resource_types: list[dict[str, str]]
    plan: dict[str, Any]


class ToolRegistry:
    """Registry for dispatchable tools, with optional explicit route metadata."""

    def __init__(self) -> None:
        self._tools: dict[str, Any] = {}
        self._registrations: dict[str, KeiToolRegistration] = {}

    def register(self, tool: Any, registration: KeiToolRegistration | None = None) -> None:
        """Register a tool and, optionally, its trusted route registration."""
        self._tools[tool.name] = tool
        self._registrations.pop(tool.name, None)
        if registration is not None:
            self._registrations[tool.name] = registration

    def get(self, name: str) -> tuple[Any, bool]:
        tool = self._tools.get(name)
        return tool, tool is not None

    def all(self) -> list[Any]:
        return [self._tools[name] for name in sorted(self._tools)]

    def names(self) -> list[str]:
        return sorted(self._tools)

    def schemas(self) -> dict[str, dict[str, Any]]:
        return {
            name: tool.input_schema()
            for name, tool in self._tools.items()
            if hasattr(tool, "input_schema")
        }

    def export_kei_tool_manifest(self, *, version: int = 2) -> str:
        """Export v1/v2 governed tools or explicitly registered v3 routes."""
        if version not in (1, 2, 3, 4):
            raise ValueError("manifest version must be 1, 2, 3, or 4")
        if version == 4:
            return self._export_v4()
        if version == 3:
            entries = []
            for name in sorted(self._registrations):
                registration = self._registrations[name]
                self._validate_registration(registration)
                entry: dict[str, Any] = {
                    "name": name,
                    "service": registration["service"],
                    "source": registration["source"],
                    "operation_class": registration["operation_class"],
                    "route": registration["route"],
                    "description": self._tools[name].description,
                    "enabled": True,
                }
                if "connector_binding" in registration["route"]:
                    entry["required_capabilities"] = sorted(registration["required_capabilities"])
                    if registration.get("resource_types"):
                        entry["resource_types"] = sorted(
                            (dict(item) for item in registration["resource_types"]),
                            key=lambda item: (item["type"], item.get("parent_type", "")),
                        )
                entries.append(entry)
            manifest = {"schema": "kei.tool-manifest/v3", "tools": entries}
            return json.dumps(manifest, indent=2, ensure_ascii=False)

        entries = []
        names = sorted(name for name, tool in self._tools.items() if isinstance(tool, GovernedTool))
        for name in names:
            tool = self._tools[name]
            scope = tool.kei_scope()
            if version == 1:
                entries.append(
                    {
                        "name": name,
                        "service": scope.service or scope.source,
                        "description": tool.description,
                        "action": scope.operation_class,
                        "resources": [item["type"] for item in scope.resource_types],
                        "enabled": True,
                    }
                )
            else:
                entry = {
                    "name": name,
                    "source": scope.source,
                    "required_capabilities": sorted(scope.required_capabilities),
                    "resource_types": sorted(
                        (dict(item) for item in scope.resource_types),
                        key=lambda item: (item["type"], item.get("parent_type", "")),
                    ),
                    "operation_class": scope.operation_class,
                }
                if scope.service:
                    entry["service"] = scope.service
                entry["description"] = tool.description
                entry["enabled"] = True
                entries.append(entry)
        manifest = (
            {"tools": entries}
            if version == 1
            else {"schema": "kei.tool-manifest/v2", "tools": entries}
        )
        return json.dumps(manifest, indent=2, ensure_ascii=False)

    def _export_v4(self) -> str:
        entries = []
        for name in sorted(self._registrations):
            reg = self._registrations[name]
            self._validate_registration(reg)
            route = reg["route"]
            entry: dict[str, Any] = {
                "name": name,
                "service": reg["service"],
                "source": reg["source"],
                "operation_class": reg["operation_class"],
                "route": route,
                "description": self._tools[name].description,
                "enabled": True,
            }
            if "harness_executor" in route:
                if "plan" in reg or "required_capabilities" in reg or "resource_types" in reg:
                    raise ValueError("harness route omits connector plan and fields")
                h = route["harness_executor"]
                if not h.get("executor", "").strip() or not h.get("registration", "").strip():
                    raise ValueError("invalid harness route")
            else:
                binding = route["connector_binding"]
                caps, resources, plan = (
                    reg.get("required_capabilities", []),
                    reg.get("resource_types", []),
                    reg.get("plan"),
                )
                if (
                    not binding.get("agent_id", "").strip()
                    or not binding.get("connector_id", "").strip()
                    or not caps
                    or not plan
                ):
                    raise ValueError("v4 connector route requires binding, capabilities and plan")
                tool = self._tools[name]
                if not hasattr(tool, "input_schema"):
                    raise ValueError("v4 connector requires ExtendedTool input schema")
                args_schema = tool.input_schema()
                self._closed_schema(args_schema)
                self._closed_schema(plan.get("context_schema"))
                self._validate_plan(plan, args_schema, caps, resources)
                entry["required_capabilities"] = sorted(caps)
                if resources:
                    entry["resource_types"] = sorted(
                        (dict(x) for x in resources),
                        key=lambda x: (x["type"], x.get("parent_type", "")),
                    )
                entry["plan"] = {"args_schema": args_schema, **plan}
            entries.append(entry)
        return json.dumps(
            {"schema": "kei.tool-manifest/v4", "tools": entries}, indent=2, ensure_ascii=False
        )

    @staticmethod
    def _closed_schema(schema: Any) -> None:
        if (
            not isinstance(schema, dict)
            or schema.get("type") != "object"
            or schema.get("additionalProperties") is not False
            or not isinstance(schema.get("properties"), dict)
        ):
            raise ValueError("must be a closed object schema")

    @classmethod
    def _validate_plan(
        cls,
        plan: dict[str, Any],
        args: dict[str, Any],
        caps: list[str],
        resources: list[dict[str, str]],
    ) -> None:
        if len(json.dumps(args, separators=(",", ":")).encode()) > 65536:
            raise ValueError("args schema exceeds 64 KiB")
        ops = plan.get("operations", [])
        if not isinstance(ops, list) or not 1 <= len(ops) <= 32:
            raise ValueError("operations must contain 1..32 entries")
        context = plan["context_schema"]["properties"]

        def ref(v: Any) -> None:
            if not isinstance(v, dict):
                raise ValueError("invalid typed ref")
            if v.get("from") == "args":
                p = v.get("pointer", "")
                if (
                    set(v) != {"from", "pointer"}
                    or not p.startswith("/")
                    or p.count("/") != 1
                    or p[1:] not in args["properties"]
                ):
                    raise ValueError("invalid direct args ref")
            elif v.get("from") == "context":
                if set(v) != {"from", "field"} or v["field"] not in context:
                    raise ValueError("invalid context ref")
            else:
                raise ValueError("reference from must be args or context")

        ids, used = set(), set()

        def template(v: Any, depth: int = 0, nodes: list[int] | None = None) -> None:
            nodes = nodes if nodes is not None else [0]
            nodes[0] += 1
            if depth > 16 or nodes[0] > 256:
                raise ValueError("provider_input bounds exceeded")
            if isinstance(v, dict):
                if "from" in v:
                    ref(v)
                else:
                    for x in v.values():
                        template(x, depth + 1, nodes)
            elif isinstance(v, list):
                for x in v:
                    template(x, depth + 1, nodes)
            elif v is not None and not isinstance(v, (str, bool, int, float)):
                raise ValueError("unsupported provider_input value")

        for op in ops:
            if (
                not isinstance(op, dict)
                or not op.get("id")
                or op["id"] in ids
                or op.get("capability") not in caps
            ):
                raise ValueError("invalid operation id/capability")
            ids.add(op["id"])
            used.add(op["capability"])
            res = op["resource"]
            parent = res.get("parent")
            pair = {"type": res.get("type"), **({"parent_type": parent["type"]} if parent else {})}
            if pair not in resources:
                raise ValueError("resource/parent pair is not declared")
            ref(res["id"])
            if parent:
                ref(parent["id"])
            text = op.get("provider_resource_template", "")
            if (
                "{resource.id}" not in text
                or any(x in text for x in ("{args", "{context", "}"))
                and text.replace("{resource.id}", "").replace("{parent.id}", "").find("{") >= 0
            ):
                raise ValueError("invalid provider resource template")
            provider_input = op.get("provider_input")
            if len(json.dumps(provider_input, separators=(",", ":")).encode()) > 65536:
                raise ValueError("provider_input exceeds 64 KiB")
            template(provider_input)
        if used != set(caps):
            raise ValueError("operation capability set must exactly cover registered capabilities")

    @staticmethod
    def _validate_registration(value: KeiToolRegistration) -> None:
        if not value.get("service", "").strip() or not value.get("source", "").strip():
            raise ValueError("v3 registration requires non-empty service and source")
        if value.get("operation_class") not in ("read", "write"):
            raise ValueError("invalid operation_class")
        route = value.get("route") or {}
        connector = route.get("connector_binding")
        harness = route.get("harness_executor")
        if (connector is None) == (harness is None):
            raise ValueError("route must contain exactly one branch")
        if connector is not None:
            caps = value.get("required_capabilities")
            if (
                not connector.get("agent_id", "").strip()
                or not connector.get("connector_id", "").strip()
                or not caps
                or any(not item.strip() for item in caps)
            ):
                raise ValueError("connector route requires binding and non-empty capabilities")
            return
        if (
            harness is None
            or not harness.get("executor", "").strip()
            or not harness.get("registration", "").strip()
        ):
            raise ValueError("harness route requires explicit identity")
        if "required_capabilities" in value or "resource_types" in value:
            raise ValueError("harness route omits connector-only fields")

    def clear(self) -> None:
        self._tools.clear()
        self._registrations.clear()
