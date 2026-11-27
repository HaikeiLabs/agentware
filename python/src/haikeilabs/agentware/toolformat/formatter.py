"""Tool formatter - Model-specific tool formatting."""

import json
import re
from dataclasses import dataclass
from typing import Any, Literal, Protocol

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

    def format_tool_result(self, name: str, result: Result, call_id: str | None = None) -> str:
        """Format tool result for model input.

        ``call_id`` is the id of the tool call this result answers, when the
        caller has one. Families whose result format carries no id accept and
        ignore it; the Anthropic formatter renders it as ``tool_use_id``.
        """
        ...

    def model_family(self) -> str:
        """Return the model family name."""
        ...

    def validate_format(self, response: str) -> str | None:
        """Validate tool call format. Returns None if valid, error message if invalid."""
        ...


def _result_text(data: Any) -> str:
    """Render a successful tool result's data as model-facing text.

    One rule for every formatter: a string is returned verbatim; ``None`` is
    the empty string; dicts, lists, numbers and booleans are compact JSON
    that keeps dict insertion order and non-ASCII text. A value JSON cannot
    encode falls back to ``str()``.
    """
    if data is None:
        return ""
    if isinstance(data, str):
        return data
    try:
        return json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError):
        return str(data)


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

    def format_tool_result(self, name: str, result: Result, call_id: str | None = None) -> str:
        if result.success:
            return _result_text(result.data)
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
    """Anthropic Claude tool use, rendered as text.

    Definitions are Anthropic tool definitions, ``{"name", "description",
    "input_schema"}``. Calls are Anthropic ``tool_use`` blocks, ``{"type":
    "tool_use", "id", "name", "input"}``, given as one block or as a content
    array (its other blocks, such as text, are ignored); each call keeps the
    block's id. When the response holds no ``tool_use`` block, the legacy
    ``<function_calls><invoke tool="name">{json}</invoke></function_calls>``
    form is parsed instead, and those calls have no id. Results are
    ``tool_result`` blocks, ``{"type": "tool_result", "tool_use_id",
    "content", "is_error"}``. Without a call id, ``tool_name`` takes the place
    of ``tool_use_id``; that block is a text rendering only, not a valid
    Messages API block.

    See https://platform.claude.com/docs/en/agents-and-tools/tool-use/overview
    """

    _invoke_re = re.compile(r'<invoke\s+tool="([^"]*)"\s*>(.*?)</invoke>')

    def format_tool_definitions(self, tools: list[ToolFormatInput]) -> str:
        if not tools:
            return ""
        definitions: list[dict[str, Any]] = []
        for tool in tools:
            schema = _get_input_schema(tool)
            definitions.append(
                {
                    "name": tool.name,
                    "description": tool.description,
                    # The Messages API requires input_schema; a tool without
                    # parameters takes an empty object schema.
                    "input_schema": schema
                    if schema is not None
                    else {"type": "object", "properties": {}},
                }
            )
        return json.dumps(definitions, separators=(",", ":"), ensure_ascii=False)

    @staticmethod
    def _tool_use_blocks(response: str) -> list[dict[str, Any]]:
        """Return the tool_use blocks of a JSON block or content array."""
        text = response.strip()
        if not text.startswith(("{", "[")):
            return []
        try:
            value = json.loads(text)
        except ValueError:
            return []
        items = value if isinstance(value, list) else [value]
        return [b for b in items if isinstance(b, dict) and b.get("type") == "tool_use"]

    @staticmethod
    def _tool_use_error(block: dict[str, Any]) -> str | None:
        """Return why a tool_use block is malformed, or None when it is valid."""
        name = block.get("name")
        if not isinstance(name, str) or not name:
            return "tool_use block missing name"
        call_id = block.get("id")
        if not isinstance(call_id, str) or not call_id:
            return f"tool_use block {name!r} missing id"
        if not isinstance(block.get("input"), dict):
            return f"tool_use block {name!r} input must be a JSON object"
        return None

    def parse_tool_calls(self, response: str) -> list[ParsedToolCall]:
        if not response:
            return []
        blocks = self._tool_use_blocks(response)
        if not blocks:
            return self._parse_invoke(response)
        # Fail closed: a malformed tool_use block is never returned as a call
        # (validate_format names the problem).
        return [
            ParsedToolCall(id=block["id"], name=block["name"], args=block["input"], raw=response)
            for block in blocks
            if self._tool_use_error(block) is None
        ]

    def _parse_invoke(self, response: str) -> list[ParsedToolCall]:
        """Parse the legacy <function_calls><invoke> form (calls carry no id)."""
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

    def format_tool_result(self, name: str, result: Result, call_id: str | None = None) -> str:
        block: dict[str, Any] = {"type": "tool_result"}
        if call_id:
            block["tool_use_id"] = call_id
        else:
            block["tool_name"] = name
        if result.success:
            block["content"] = _result_text(result.data)
        else:
            block["content"] = result.error or ""
            block["is_error"] = True
        return json.dumps(block, separators=(",", ":"), ensure_ascii=False)

    def model_family(self) -> str:
        return "anthropic"

    def validate_format(self, response: str) -> str | None:
        if not response:
            return None
        blocks = self._tool_use_blocks(response)
        if not blocks:
            return self._validate_invoke(response)
        seen: set[str] = set()
        for block in blocks:
            error = self._tool_use_error(block)
            if error is not None:
                return error
            if block["id"] in seen:
                return f"duplicate tool_use id {block['id']!r}"
            seen.add(block["id"])
        return None

    def _validate_invoke(self, response: str) -> str | None:
        """Validate the legacy <function_calls><invoke> form."""
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

    def format_tool_result(self, name: str, result: Result, call_id: str | None = None) -> str:
        # The official Qwen chat template emits a tool message's content
        # verbatim between the tool_response tags, so a string is not quoted.
        if result.success:
            text = _result_text(result.data)
        else:
            text = json.dumps({"error": result.error}, ensure_ascii=False, separators=(",", ":"))
        return f"<tool_response>\n{text}\n</tool_response>"

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


MiniMaxVersion = Literal["m2", "m3"]

#: The marker MiniMax M3 puts before every structural tag of a tool call.
MINIMAX_M3_NAMESPACE = "]<]minimax[>["
_MM3_TOOL_CALL = MINIMAX_M3_NAMESPACE + "<tool_call>"
_MM3_INVOKE = MINIMAX_M3_NAMESPACE + "<invoke name="
_MM3_INVOKE_END = MINIMAX_M3_NAMESPACE + "</invoke>"
_MM3_ELEMENT = MINIMAX_M3_NAMESPACE + "<"
_MM3_MAX_DEPTH = 128
_MM3_MIXED_TEXT = "$text"

_MM_TOOLS_HEADER = (
    "# Tools\nYou may call one or more tools to assist with the user query.\n"
    "Here are the tools available in JSONSchema format:\n\n<tools>\n"
)
_MM2_INSTRUCTIONS = (
    "When making tool calls, use XML format to invoke tools and pass parameters:\n\n"
    '<minimax:tool_call>\n<invoke name="tool-name-1">\n'
    '<parameter name="param-key-1">param-value-1</parameter>\n'
    '<parameter name="param-key-2">param-value-2</parameter>\n'
    "...\n</invoke>\n</minimax:tool_call>"
)


def _mm3_instructions() -> str:
    ns = MINIMAX_M3_NAMESPACE
    return (
        f"To call tools, wrap all invocations in a single {ns}<tool_call>{ns}</tool_call> block. "
        "Parameter values containing nested objects or arrays are recursively expanded into "
        "XML elements. Example:\n\n"
        f"{ns}<tool_call>\n"
        f'{ns}<invoke name="tool-name-1">{ns}<param-1>value-1{ns}</param-1>{ns}<param-2>'
        f"{ns}<item>{ns}<key-a>val-a{ns}</key-a>{ns}<key-b>val-b{ns}</key-b>{ns}</item>"
        f"{ns}</param-2>{ns}</invoke>\n"
        f'{ns}<invoke name="tool-name-2">{ns}<param-1>value-1{ns}</param-1>{ns}</invoke>\n'
        f"{ns}</tool_call>"
    )


_MM2_BLOCK = "<minimax:tool_call>"
_MM2_INVOKE_OPEN = "<invoke name="
_mm2_invoke_re = re.compile(
    r"""<invoke name=(?:"([^"]*)"|'([^']*)'|([^>\s]*))>(.*?)</invoke>""", re.DOTALL
)
_mm2_param_open_re = re.compile(
    r"""<\s*parameter\s+name\s*=\s*(?:"([^"]*)"|'([^']*)'|([^>\s]+))\s*>""", re.DOTALL
)
_mm2_param_close_re = re.compile(r"<\s*/\s*parameter\s*>")

# An M3 element value: its text, or its (name, value) children in order.
_MM3Element = str | list[tuple[str, Any]]


def _mm2_params(body: str) -> tuple[dict[str, Any], bool]:
    """Parse M2 parameter tags; the flag is False if one was dropped unterminated.

    A value runs to its closing tag or, as in vLLM, to the next parameter tag.
    Values stay verbatim strings: the template writes strings as is and other
    values as JSON, and with no schema here the two cannot be told apart.
    """
    openers = list(_mm2_param_open_re.finditer(body))
    params: dict[str, Any] = {}
    complete = True
    for i, opener in enumerate(openers):
        end = openers[i + 1].start() if i + 1 < len(openers) else len(body)
        region = body[opener.end() : end]
        close = _mm2_param_close_re.search(region)
        if close is not None:
            value = region[: close.start()]
        elif i + 1 < len(openers):
            value = region
        else:
            complete = False
            continue
        name = (opener[1] or opener[2] or opener[3] or "").strip()
        if name:
            params[name] = value
    return params, complete


def _mm3_element(text: str, pos: int, depth: int) -> tuple[str, _MM3Element, int] | None:
    """Parse the M3 element starting at ``pos``; ``None`` if malformed."""
    if depth > _MM3_MAX_DEPTH:
        return None
    name_start = pos + len(_MM3_ELEMENT)
    name_end = text.find(">", name_start)
    if name_end <= name_start:
        return None
    name = text[name_start:name_end]
    if name.startswith("/") or not name.strip():
        return None
    close_tag = f"{MINIMAX_M3_NAMESPACE}</{name}>"
    pos = name_end + 1
    text_parts: list[str] = []
    children: list[tuple[str, Any]] = []
    while True:
        marker = text.find(MINIMAX_M3_NAMESPACE, pos)
        if marker < 0:
            return None
        text_parts.append(text[pos:marker])
        if text.startswith(close_tag, marker):
            pos = marker + len(close_tag)
            break
        if not text.startswith(_MM3_ELEMENT, marker):
            return None
        child = _mm3_element(text, marker, depth + 1)
        if child is None:
            return None
        children.append((child[0], child[1]))
        pos = child[2]
    body_text = "".join(text_parts)
    if not children:
        return name, body_text, pos
    if body_text.strip():
        field = _MM3_MIXED_TEXT
        while any(child_name == field for child_name, _ in children):
            field = "$" + field
        children.append((field, body_text))
    return name, children, pos


def _mm3_params(body: str) -> tuple[list[tuple[str, Any]], bool]:
    """Parse an M3 invoke body; the flag is False if parsing stopped early.

    Parsing stops at the first malformed element and keeps the ones before it.
    """
    params: list[tuple[str, Any]] = []
    pos = 0
    while True:
        while pos < len(body) and body[pos].isspace():
            pos += 1
        if pos == len(body):
            return params, True
        if not body.startswith(_MM3_ELEMENT, pos):
            return params, False
        element = _mm3_element(body, pos, 1)
        if element is None:
            return params, False
        params.append((element[0], element[1]))
        pos = element[2]


def _mm3_object(elements: list[tuple[str, Any]]) -> dict[str, Any]:
    """Elements to an object without a schema; a repeated name collects a list."""
    result: dict[str, Any] = {}
    repeated: set[str] = set()
    for name, element in elements:
        value = element if isinstance(element, str) else _mm3_object(element)
        if name not in result:
            result[name] = value
        elif name in repeated:
            result[name].append(value)
        else:
            result[name] = [result[name], value]
            repeated.add(name)
    return result


@dataclass
class _MiniMaxInvoke:
    name: str
    args: dict[str, Any]
    raw: str
    complete: bool


def _mm3_invokes(response: str) -> tuple[list[_MiniMaxInvoke], str | None]:
    """M3 invokes after the tool_call opener, and the first format error."""
    start = response.find(_MM3_TOOL_CALL)
    if start < 0:
        if _MM3_INVOKE in response:
            return [], "MiniMax M3 invoke outside a tool_call block"
        return [], None
    invokes: list[_MiniMaxInvoke] = []
    pos = start + len(_MM3_TOOL_CALL)
    while True:
        begin = response.find(_MM3_INVOKE, pos)
        if begin < 0:
            break
        name_start = begin + len(_MM3_INVOKE)
        quote = response[name_start : name_start + 1]
        if quote in ('"', "'"):
            name_start += 1
            name_end = response.find(quote + ">", name_start)
            body_start = name_end + 2
        else:
            name_end = response.find(">", name_start)
            body_start = name_end + 1
        if name_end < 0:
            return invokes, "unterminated MiniMax invoke"
        end = response.find(_MM3_INVOKE_END, body_start)
        if end < 0:
            return invokes, "unterminated MiniMax invoke"
        params, complete = _mm3_params(response[body_start:end])
        pos = end + len(_MM3_INVOKE_END)
        invokes.append(
            _MiniMaxInvoke(
                name=response[name_start:name_end].strip(),
                args=_mm3_object(params),
                raw=response[begin:pos],
                complete=complete,
            )
        )
    if not invokes:
        return [], "MiniMax tool_call block has no invoke"
    return invokes, None


def _mm2_invokes(response: str) -> tuple[list[_MiniMaxInvoke], str | None]:
    """M2 invokes anywhere in the response (as vLLM), and the first format error."""
    invokes = []
    for match in _mm2_invoke_re.finditer(response):
        args, complete = _mm2_params(match[4])
        name = (match[1] or match[2] or match[3] or "").strip()
        invokes.append(_MiniMaxInvoke(name=name, args=args, raw=match[0], complete=complete))
    if response.count(_MM2_INVOKE_OPEN) != len(invokes):
        return invokes, "unterminated MiniMax invoke"
    if not invokes and _MM2_BLOCK in response:
        return [], "MiniMax tool_call block has no invoke"
    return invokes, None


def _minimax_invokes(response: str) -> tuple[list[_MiniMaxInvoke], str | None]:
    """A response with the M3 namespace marker is M3; any other is M2."""
    if MINIMAX_M3_NAMESPACE in response:
        return _mm3_invokes(response)
    return _mm2_invokes(response)


class MiniMaxFormatter:
    """MiniMax M2 and M3 tool calls, as the official chat templates render them.

    ``version`` picks the tool-definition prompt (M2 parameter tags or M3
    namespaced elements). Parsing and validation accept both syntaxes, and a
    tool result is a ``response`` block in both. Parameter values are not
    coerced to the tool schema: they arrive as strings (M3 nested elements as
    objects, repeated names as lists), the same as vLLM's parsers without a
    request schema.
    """

    def __init__(self, version: MiniMaxVersion = "m2") -> None:
        if version not in ("m2", "m3"):
            raise ValueError(f"unknown MiniMax version {version!r}; expected 'm2' or 'm3'")
        self.version: MiniMaxVersion = version

    def format_tool_definitions(self, tools: list[ToolFormatInput]) -> str:
        if not tools:
            return ""
        lines = []
        for tool in tools:
            function: dict[str, Any] = {"name": tool.name, "description": tool.description}
            schema = _get_input_schema(tool)
            if schema is not None:
                function["parameters"] = schema
            encoded = json.dumps(
                function, separators=(",", ":"), ensure_ascii=False, sort_keys=True
            )
            lines.append(f"<tool>{encoded}</tool>")
        instructions = _mm3_instructions() if self.version == "m3" else _MM2_INSTRUCTIONS
        return _MM_TOOLS_HEADER + "\n".join(lines) + "\n</tools>\n\n" + instructions

    def parse_tool_calls(self, response: str) -> list[ParsedToolCall]:
        invokes, _ = _minimax_invokes(response)
        return [
            ParsedToolCall(id="", name=invoke.name, args=invoke.args, raw=invoke.raw)
            for invoke in invokes
            if invoke.name
        ]

    def format_tool_result(self, name: str, result: Result, call_id: str | None = None) -> str:
        # Both chat templates emit a tool message's content verbatim between
        # the response tags, so a string is not quoted.
        if result.success:
            text = _result_text(result.data)
        else:
            text = json.dumps({"error": result.error}, ensure_ascii=False, separators=(",", ":"))
        return f"<response>{text}</response>"

    def model_family(self) -> str:
        return "minimax"

    def validate_format(self, response: str) -> str | None:
        invokes, error = _minimax_invokes(response)
        if error is not None:
            return error
        for invoke in invokes:
            if not invoke.name:
                return "MiniMax invoke missing tool name"
            if not invoke.complete:
                return f"MiniMax invoke {invoke.name!r} has a malformed or unterminated parameter"
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
    if "minimax-m3" in lower:
        return MiniMaxFormatter(version="m3")
    if "minimax-m2" in lower:
        return MiniMaxFormatter(version="m2")
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
