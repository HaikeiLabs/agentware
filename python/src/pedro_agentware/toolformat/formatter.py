"""Tool formatter - Model-specific tool formatting."""

import json
import re
from dataclasses import dataclass
from typing import Any, Protocol

from ..tools import Result, Tool


@dataclass
class ParsedToolCall:
    """A parsed tool call from model output."""

    id: str
    name: str
    args: dict[str, Any]
    raw: str


class ToolFormatter(Protocol):
    """Protocol for tool formatters."""

    def format_tool_definitions(self, tools: list[Tool]) -> str:
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


def _get_input_schema(tool: Tool) -> dict[str, Any] | None:
    """Get input schema from a tool if available."""
    attr = getattr(tool, "input_schema", None)
    if attr is not None:
        if callable(attr):
            result = attr()
            if isinstance(result, dict):
                return result
            return None
        if isinstance(attr, dict):
            return attr
        return None
    return None


class GenericFormatter:
    """Generic OpenAI-compatible JSON tool formatter."""

    def format_tool_definitions(self, tools: list[Tool]) -> str:
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
            args_raw = c.get("arguments")
            if isinstance(args_raw, dict):
                args = args_raw
            elif isinstance(args_raw, str):
                try:
                    args = json.loads(args_raw)
                except json.JSONDecodeError:
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

    def format_tool_definitions(self, tools: list[Tool]) -> str:
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
    """Qwen XML tool-call format formatter."""

    _tool_call_re = re.compile(
        r'<tool_call>\s*<tool name="([^"]+)">\s*(.*?)\s*</tool>\s*</tool_call>',
        re.DOTALL,
    )

    def format_tool_definitions(self, tools: list[Tool]) -> str:
        if not tools:
            return ""
        parts = []
        for tool in tools:
            part = (
                f"<tool_description>\n"
                f"<tool_name>{tool.name}</tool_name>\n"
                f"<description>{tool.description}</description>\n"
            )
            schema = _get_input_schema(tool)
            if schema is not None:
                part += f"<parameters>{json.dumps(schema, separators=(',', ':'))}</parameters>\n"
            part += "</tool_description>\n"
            parts.append(part)
        return "".join(parts)

    def parse_tool_calls(self, response: str) -> list[ParsedToolCall]:
        if not response:
            return []
        matches = self._tool_call_re.findall(response)
        if not matches:
            return []
        result = []
        for name, args_raw in matches:
            args_raw = args_raw.strip()
            try:
                args = json.loads(args_raw) if args_raw else {}
            except json.JSONDecodeError:
                args = {"_raw": args_raw}
            if not isinstance(args, dict):
                args = {"_raw": args_raw}
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
            return (
                f"<tool_response>\n"
                f"<tool_name>{name}</tool_name>\n"
                f"<result>{output}</result>\n"
                f"</tool_response>"
            )
        return (
            f"<tool_response>\n"
            f"<tool_name>{name}</tool_name>\n"
            f"<error>{result.error}</error>\n"
            f"</tool_response>"
        )

    def model_family(self) -> str:
        return "qwen"

    def validate_format(self, response: str) -> str | None:
        if not response:
            return None
        # Check for tool tags lacking a name attribute
        for m in re.finditer(r"<tool\b([^>]*)>", response):
            attrs = m.group(1)
            tmatch = re.search(r'name\s*=\s*"([^"]*)"', attrs)
            if tmatch is None:
                return "tool call missing function name"
        matches = self._tool_call_re.findall(response)
        for name, args_raw in matches:
            if not name:
                return "tool call missing function name"
            args_raw = args_raw.strip()
            if not args_raw:
                return f"tool call {name!r} missing arguments"
            try:
                json.loads(args_raw)
            except json.JSONDecodeError as e:
                return f"tool call {name!r} has invalid JSON arguments: {e}"
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
