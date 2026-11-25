"""Tool formatter - Model-specific tool formatting."""

import json
import re
from dataclasses import dataclass
from typing import Any, Protocol

from ..tools import Result, schema_of


class ToolFormatInput(Protocol):
    """The model-facing declaration fields shared by tool and ToolSpec types."""

    name: str
    description: str


@dataclass
class ParsedToolCall:
    """A parsed tool call from model output."""

    id: str
    name: str
    args: dict[str, Any]
    raw: str


class ToolFormatter(Protocol):
    """Protocol for tool formatters."""

    def format_tool_definitions(self, tools: list[ToolFormatInput]) -> str:
        """Format tool definitions for model prompt."""
        ...

    def parse_tool_calls(self, response: str) -> list[ParsedToolCall]:
        """Parse tool calls from model output."""
        ...

    def format_tool_result(self, name: str, result: Result) -> str:
        """Format tool result for model input."""
        ...

    def model_family(self) -> str:
        """Return the model family name."""
        ...

    def validate_format(self, response: str) -> str | None:
        """Validate tool call format. Returns None if valid, error message if invalid."""
        ...


def _get_input_schema(tool: ToolFormatInput) -> dict[str, Any] | None:
    """Get input schema from a tool if available."""
    return schema_of(tool)


class GenericFormatter:
    """Generic OpenAI-compatible JSON tool formatter."""

    def format_tool_definitions(self, tools: list[ToolFormatInput]) -> str:
        if not tools:
            return ""
        schemas: list[dict[str, Any]] = []
        for tool in tools:
            entry: dict[str, Any] = {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description,
                },
            }
            schema = _get_input_schema(tool)
            if schema is not None:
                entry["function"]["parameters"] = schema
            schemas.append(entry)
        return json.dumps(schemas, separators=(",", ":"))

    def parse_tool_calls(self, response: str) -> list[ParsedToolCall]:
        if not response:
            return []
        try:
            calls = json.loads(response)
        except json.JSONDecodeError:
            return []
        if not isinstance(calls, list):
            return []
        result = []
        for c in calls:
            if not isinstance(c, dict):
                continue
            name = c.get("name", "")
            # Fail closed like the Go formatter, which errors on entries with
            # an empty name or arguments that do not decode to an object:
            # skip the entry instead of returning a malformed call.
            if not name:
                continue
            args_raw = c.get("arguments")
            if isinstance(args_raw, dict):
                args = args_raw
            elif isinstance(args_raw, str):
                try:
                    args = json.loads(args_raw)
                except json.JSONDecodeError:
                    continue
                if not isinstance(args, dict):
                    continue
            else:
                continue
            result.append(
                ParsedToolCall(
                    id=c.get("id", ""),
                    name=name,
                    args=args,
                    raw=response,
                )
            )
        return result

    def format_tool_result(self, name: str, result: Result) -> str:
        if result.success:
            return str(result.data) if result.data is not None else ""
        return f"Error: {result.error}"

    def model_family(self) -> str:
        return "generic"

    def validate_format(self, response: str) -> str | None:
        if not response:
            return None
        try:
            calls = json.loads(response)
        except json.JSONDecodeError as e:
            return f"invalid JSON format: {e}"
        if not isinstance(calls, list):
            return "expected a JSON array"
        for c in calls:
            if not isinstance(c, dict):
                return "tool call is not an object"
            name = c.get("name")
            if not name:
                return "tool call missing 'name' field"
            args = c.get("arguments")
            if args is None:
                return f"tool call {name!r} missing 'arguments'"
        return None


class OpenAIFOrmatter(GenericFormatter):
    """OpenAI-compatible JSON tool formatter (OpenAI models)."""

    def model_family(self) -> str:
        return "openai"


class AnthropicFormatter:
    """Anthropic Claude XML tool-use format formatter."""

    _invoke_re = re.compile(r'<invoke\s+tool="([^"]*)"\s*>(.*?)</invoke>')

    def format_tool_definitions(self, tools: list[ToolFormatInput]) -> str:
        if not tools:
            return ""
        schemas: list[dict[str, Any]] = []
        for tool in tools:
            entry: dict[str, Any] = {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description,
                },
            }
            schema = _get_input_schema(tool)
            if schema is not None:
                entry["function"]["parameters"] = schema
            schemas.append(entry)
        return json.dumps(schemas, separators=(",", ":"))

    def parse_tool_calls(self, response: str) -> list[ParsedToolCall]:
        if not response:
            return []
        matches = self._invoke_re.findall(response)
        if not matches:
            return []
        result = []
        for name, args_raw in matches:
            # Fail closed like the Go formatter: skip entries with an empty
            # tool name instead of returning a malformed call.
            if not name:
                continue
            args_raw = args_raw.strip()
            if not args_raw:
                continue
            try:
                args = json.loads(args_raw)
            except json.JSONDecodeError:
                continue
            if not isinstance(args, dict):
                continue
            result.append(
                ParsedToolCall(
                    id="",
                    name=name,
                    args=args,
                    raw=response,
                )
            )
        return result

    def format_tool_result(self, name: str, result: Result) -> str:
        if result.success:
            output = str(result.data) if result.data is not None else ""
        else:
            output = f"Error: {result.error}"
        return f'<function_results><result tool="{name}">{output}</result></function_results>'

    def model_family(self) -> str:
        return "anthropic"

    def validate_format(self, response: str) -> str | None:
        if not response:
            return None
        # Check for invoke tags lacking a tool attribute
        for m in re.finditer(r"<invoke\b([^>]*)>", response):
            attrs = m.group(1)
            tmatch = re.search(r'tool="([^"]*)"', attrs)
            if tmatch is None:
                return "tool call missing 'tool' attribute"
        # Validate captured calls
        for name, args_raw in self._invoke_re.findall(response):
            if not name:
                return "tool call missing 'tool' attribute"
            if not args_raw.strip():
                return f"tool call {name!r} missing arguments"
            try:
                json.loads(args_raw)
            except json.JSONDecodeError as e:
                return f"tool call {name!r} has invalid JSON arguments: {e}"
        return None


class QwenFormatter:
    """Qwen Hermes tool calls, with deprecated legacy XML parsing."""

    _span_re = re.compile(r"<tool_call>(.*?)</tool_call>", re.DOTALL)
    _legacy_re = re.compile(r'<tool name="([^"]+)">\s*(.*?)\s*</tool>', re.DOTALL)

    def format_tool_definitions(self, tools: list[ToolFormatInput]) -> str:
        if not tools:
            return ""
        lines = []
        for tool in tools:
            function: dict[str, Any] = {"name": tool.name, "description": tool.description}
            schema = _get_input_schema(tool)
            if schema is not None:
                function["parameters"] = schema
            lines.append(
                json.dumps(
                    {"type": "function", "function": function},
                    separators=(",", ":"),
                    ensure_ascii=False,
                    sort_keys=True,
                )
            )
        return (
            "# Tools\n\nYou may call one or more functions to assist with the user query.\n\n"
            "You are provided with function signatures within <tools></tools> XML tags:\n<tools>\n"
            + "\n".join(lines)
            + "\n</tools>\n\n"
            "For each function call, return a json object with function name and arguments within <tool_call></tool_call> XML tags:\n"
            '<tool_call>\n{"name": <function-name>, "arguments": <args-json-object>}\n</tool_call>'
        )

    def _decode(self, body: str) -> tuple[str, dict[str, Any]] | None:
        try:
            call = json.loads(body)
            if (
                not isinstance(call, dict)
                or not isinstance(call.get("name"), str)
                or not call["name"]
            ):
                return None
            args = call.get("arguments")
            if isinstance(args, str):
                args = json.loads(args)
            if not isinstance(args, dict):
                return None
            return call["name"], args
        except (ValueError, TypeError):
            return None

    def parse_tool_calls(self, response: str) -> list[ParsedToolCall]:
        spans = self._span_re.findall(response)
        hermes = [body for body in spans if not body.lstrip().startswith("<tool")]
        calls = []
        if hermes:
            for body in hermes:
                decoded = self._decode(body)
                if decoded is not None:
                    calls.append(
                        ParsedToolCall(id="", name=decoded[0], args=decoded[1], raw=response)
                    )
        else:
            for body in spans:
                match = self._legacy_re.fullmatch(body.strip())
                if match:
                    try:
                        args = json.loads(match[2]) if match[2] else {}
                    except ValueError:
                        continue
                    if isinstance(args, dict):
                        calls.append(ParsedToolCall(id="", name=match[1], args=args, raw=response))
        return calls

    def format_tool_result(self, name: str, result: Result) -> str:
        value = result.data if result.success else {"error": result.error}
        return f"<tool_response>\n{json.dumps(value, ensure_ascii=False, separators=(',', ':'), sort_keys=True)}\n</tool_response>"

    def model_family(self) -> str:
        return "qwen"

    def validate_format(self, response: str) -> str | None:
        spans = self._span_re.findall(response)
        hermes = [body for body in spans if not body.lstrip().startswith("<tool")]
        if hermes:
            if any(self._decode(body) is None for body in hermes):
                return "invalid JSON tool call or arguments"
        else:
            for body in spans:
                match = self._legacy_re.fullmatch(body.strip())
                if not match:
                    return "tool call missing function name"
                if not match[2]:
                    return "tool call missing arguments"
                try:
                    if not isinstance(json.loads(match[2]), dict):
                        return "invalid JSON arguments"
                except ValueError:
                    return "invalid JSON arguments"
        return None


class DeepSeekFormatter(GenericFormatter):
    """DeepSeek uses standard OpenAI-compatible JSON function-calling format."""

    def model_family(self) -> str:
        return "deepseek"


class GLMFormatter(GenericFormatter):
    """GLM-4 uses standard OpenAI-compatible JSON function-calling format."""

    def model_family(self) -> str:
        return "glm"


def get_formatter(model_name: str) -> ToolFormatter:
    """Select the appropriate formatter for a given model name."""
    lower = model_name.lower()
    if "anthropic" in lower or "claude" in lower:
        return AnthropicFormatter()
    if "qwq" in lower or "qwen" in lower:
        return QwenFormatter()
    if "deepseek" in lower:
        return DeepSeekFormatter()
    if "chatglm" in lower or "glm" in lower:
        return GLMFormatter()
    if "o1" in lower or "o3" in lower or "gpt" in lower:
        return OpenAIFOrmatter()
    return GenericFormatter()
