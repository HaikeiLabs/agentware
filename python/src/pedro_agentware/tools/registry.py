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
        allowed = {
            "type",
            "properties",
            "required",
            "additionalProperties",
            "minimum",
            "maximum",
            "minLength",
            "maxLength",
            "minItems",
            "maxItems",
            "items",
            "pattern",
            "enum",
        }

        def visit(node: Any, depth: int = 0) -> None:
            if depth > 16 or not isinstance(node, dict) or set(node) - allowed:
                raise ValueError("unsupported schema keyword or nesting exceeds 16")
            if node.get("type") not in {
                "object",
                "array",
                "string",
                "integer",
                "number",
                "boolean",
            }:
                raise ValueError("unsupported or missing schema type")
            if node["type"] == "object":
                if node.get("additionalProperties") is not False or not isinstance(
                    node.get("properties"), dict
                ):
                    raise ValueError("object schema must be closed with properties")
                required = node.get("required", [])
                if not isinstance(required, list) or any(
                    x not in node["properties"] for x in required
                ):
                    raise ValueError("invalid required fields")
                for child in node["properties"].values():
                    visit(child, depth + 1)
            if node["type"] == "array":
                if (
                    not isinstance(node.get("maxItems"), int)
                    or not 0 <= node["maxItems"] <= 256
                    or not isinstance(node.get("items"), dict)
                ):
                    raise ValueError("array schema requires bounded maxItems and items")
                visit(node["items"], depth + 1)

        visit(schema)
        if schema.get("type") != "object":
            raise ValueError("must be a closed object schema")

    @classmethod
    def _validate_plan(
        cls,
        plan: dict[str, Any],
        args: dict[str, Any],
        caps: list[str],
        resources: list[dict[str, str]],
    ) -> None:
        context_schema = plan["context_schema"]
        for schema in (args, context_schema):
            if len(json.dumps(schema, separators=(",", ":")).encode()) > 65536:
                raise ValueError("serialized schema exceeds 64 KiB")
        ops = plan.get("operations", [])
        if not isinstance(ops, list) or not 1 <= len(ops) <= 32:
            raise ValueError("operations must contain 1..32 entries")
        args_props, context_props = args["properties"], context_schema["properties"]

        def ref(v: Any) -> None:
            if not isinstance(v, dict) or set(v) - {"from", "pointer", "field", "type"}:
                raise ValueError("invalid typed ref")
            if v.get("from") == "args":
                p = v.get("pointer", "")
                if (
                    set(v) != {"from", "pointer", "type"}
                    or not p.startswith("/")
                    or p.count("/") != 1
                ):
                    raise ValueError("args refs require direct typed pointer")
                key, props = p[1:].replace("~1", "/").replace("~0", "~"), args_props
            elif v.get("from") == "context":
                if set(v) != {"from", "field", "type"}:
                    raise ValueError("context refs require typed field")
                key, props = v["field"], context_props
            else:
                raise ValueError("reference from must be args or context")
            prop = props.get(key)
            if (
                not isinstance(prop, dict)
                or v["type"] != prop.get("type")
                or v["type"] in {"object", "array"}
            ):
                raise ValueError("reference type does not match scalar schema property")

        ids, used = set(), set()

        def template(v: Any, depth: int = 0, nodes: list[int] | None = None) -> None:
            nodes = nodes if nodes is not None else [0]
            nodes[0] += 1
            if depth > 16 or nodes[0] > 256:
                raise ValueError("provider_input bounds exceeded")
            if isinstance(v, dict):
                if "ref" in v:
                    if set(v) != {"ref"}:
                        raise ValueError("provider_input ref wrapper has unknown keys")
                    ref(v["ref"])
                elif "from" in v:
                    raise ValueError("provider_input refs must use ref wrapper")
                else:
                    for x in v.values():
                        template(x, depth + 1, nodes)
            elif isinstance(v, list):
                for x in v:
                    template(x, depth + 1, nodes)
            elif v is not None and not isinstance(v, (str, bool, int, float)):
                raise ValueError("unsupported provider_input value")

        def validate_value(value: Any, schema: dict[str, Any]) -> None:
            typ = schema["type"]
            valid = {
                "string": lambda x: isinstance(x, str),
                "integer": lambda x: isinstance(x, int) and not isinstance(x, bool),
                "number": lambda x: isinstance(x, (int, float)) and not isinstance(x, bool),
                "boolean": lambda x: isinstance(x, bool),
                "object": lambda x: isinstance(x, dict),
                "array": lambda x: isinstance(x, list),
            }[typ](value)
            if not valid:
                raise ValueError("provider_input value does not match schema")
            if typ == "object":
                props = schema["properties"]
                if set(value) - set(props) or set(schema.get("required", [])) - set(value):
                    raise ValueError("provider_input object violates schema")
                for k, x in value.items():
                    validate_value(x, props[k])
            elif typ == "array":
                if len(value) > schema["maxItems"]:
                    raise ValueError("provider_input array exceeds maxItems")
                for x in value:
                    validate_value(x, schema["items"])
            elif typ == "string":
                if len(value) < schema.get("minLength", 0) or len(value) > schema.get(
                    "maxLength", 2**31
                ):
                    raise ValueError("provider_input string violates bounds")
            elif typ in {"integer", "number"}:
                if value < schema.get("minimum", float("-inf")) or value > schema.get(
                    "maximum", float("inf")
                ):
                    raise ValueError("provider_input number violates bounds")

        for op in ops:
            if (
                not isinstance(op, dict)
                or set(op)
                - {"id", "capability", "resource", "provider_resource_template", "provider_input"}
                or not op.get("id")
                or op["id"] in ids
                or op.get("capability") not in caps
            ):
                raise ValueError("invalid operation id/capability/keys")
            ids.add(op["id"])
            used.add(op["capability"])
            res, text = op.get("resource"), op.get("provider_resource_template", "")
            if res is None:
                if text:
                    raise ValueError("resource-less operation must omit provider_resource_template")
            else:
                parent = res.get("parent")
                pair = {
                    "type": res.get("type"),
                    **({"parent_type": parent["type"]} if parent else {}),
                }
                if pair not in resources:
                    raise ValueError("resource/parent pair is not declared")
                if res.get("id") is not None:
                    ref(res["id"])
                if parent and parent.get("id") is not None:
                    ref(parent["id"])
                if res.get("id") is None and "{resource.id}" in text:
                    raise ValueError("unresolved resource id")
                if (not parent or parent.get("id") is None) and "{parent.id}" in text:
                    raise ValueError("unresolved parent id")
                if res.get("id") is None and not parent:
                    raise ValueError("resource operation requires id or parent")
                residual = text.replace("{resource.id}", "").replace("{parent.id}", "")
                if "{" in residual or "}" in residual or (res.get("id") is not None and not text):
                    raise ValueError("invalid provider resource template")
            provider_input = op.get("provider_input")
            if len(json.dumps(provider_input, separators=(",", ":")).encode()) > 65536:
                raise ValueError("provider_input exceeds 64 KiB")
            template(provider_input)

            # Validate literals and refs recursively against their declared scalar schema.
            def check_template_value(v: Any) -> None:
                if isinstance(v, dict) and set(v) == {"ref"}:
                    return
                if isinstance(v, dict):
                    for x in v.values():
                        check_template_value(x)
                elif isinstance(v, list):
                    for x in v:
                        check_template_value(x)

            check_template_value(provider_input)
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
