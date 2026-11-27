"""GPT-OSS Harmony format: render prompts and parse raw completions.

GPT-OSS models are trained on the Harmony format. Over a chat-completions API
the server applies it; over raw ``/v1/completions`` the caller renders the
whole prompt and parses the completion text. This module does both:

- :class:`HarmonyFormatter` is the :class:`~.formatter.ToolFormatter` for the
  family. Tool definitions are the developer message's ``# Tools`` section, a
  TypeScript-style ``functions`` namespace. A call is an assistant message on
  the commentary channel addressed ``to=functions.NAME``. A result is a
  ``functions.NAME`` tool message addressed to the assistant.
- :func:`render_harmony_prompt` renders a whole conversation (system,
  developer, then the messages) and opens the next assistant turn.
- :func:`parse_harmony_completion` splits completion text into messages; the
  analysis channel is the reasoning and the final channel is the answer.

Harmony is opt-in: :func:`~.formatter.get_formatter` never returns
:class:`HarmonyFormatter`, so chat-completions callers keep their formatter;
raw-completions callers construct it themselves.

The rendering follows the reference renderer in openai/harmony
(``src/encoding.rs``) byte for byte, including its JSON Schema to TypeScript
mapping, and the shared fixture ``fixtures/toolformat/harmony-cases.json``
replays openai/harmony's test-data. See ``docs/model-format-reference.md``.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from ..tools import Result, schema_of
from .formatter import ParsedToolCall, ToolFormatInput, _result_text

START = "<|start|>"
END = "<|end|>"
MESSAGE = "<|message|>"
CHANNEL = "<|channel|>"
CONSTRAIN = "<|constrain|>"
CALL = "<|call|>"
RETURN = "<|return|>"

#: The content type of a JSON tool call: ``<|constrain|>json``.
HARMONY_JSON_CONTENT_TYPE = CONSTRAIN + "json"
#: Stop sequences for a raw completion: the end of a final answer and the end
#: of a tool call. Servers usually strip the stop sequence from the text,
#: which the parser accepts.
HARMONY_STOP_TOKENS: tuple[str, str] = (RETURN, CALL)

#: The model identity GPT-OSS was trained with.
DEFAULT_MODEL_IDENTITY = "You are ChatGPT, a large language model trained by OpenAI."

_ROLES = frozenset({"system", "developer", "user", "assistant", "tool"})
_EFFORTS = frozenset({"", "low", "medium", "high"})
_FUNCTIONS_PREFIX = "functions."
_TERMINATORS = (END, CALL, RETURN)


@dataclass(frozen=True)
class HarmonyMessage:
    """One Harmony message.

    ``role`` is system, developer, user, assistant or tool. A tool message's
    ``name`` is its author, such as ``functions.get_weather``; for other
    roles a ``name`` renders as ``role:name``. ``recipient`` is the
    ``to=`` target (``functions.NAME`` for a call). ``content_type`` follows
    the channel, such as :data:`HARMONY_JSON_CONTENT_TYPE`.
    """

    role: str
    content: str
    channel: str = ""
    recipient: str = ""
    content_type: str = ""
    name: str = ""

    def __post_init__(self) -> None:
        if self.role not in _ROLES:
            raise ValueError(f"unknown Harmony role {self.role!r}")
        if self.role == "tool" and not self.name:
            raise ValueError("a Harmony tool message needs a name, such as functions.get_weather")


@dataclass(frozen=True)
class HarmonySystem:
    """The system message. An empty field is left out of the rendering."""

    model_identity: str = DEFAULT_MODEL_IDENTITY
    knowledge_cutoff: str = "2024-06"
    current_date: str = ""
    reasoning_effort: str = "medium"
    valid_channels: tuple[str, ...] = ("analysis", "commentary", "final")

    def __post_init__(self) -> None:
        if self.reasoning_effort not in _EFFORTS:
            raise ValueError(
                f"reasoning_effort must be low, medium or high, not {self.reasoning_effort!r}"
            )


@dataclass
class HarmonyCompletion:
    """A parsed completion.

    ``reasoning`` joins the assistant's analysis-channel messages and
    ``content`` its final-channel (or channel-less) messages, each with a
    newline. ``tool_calls`` are the calls to the ``functions`` namespace whose
    arguments are a JSON object; others are left out (fail closed), and
    :meth:`HarmonyFormatter.validate_format` reports them.
    """

    messages: list[HarmonyMessage] = field(default_factory=list)
    reasoning: str = ""
    content: str = ""
    tool_calls: list[ParsedToolCall] = field(default_factory=list)


class HarmonyFormatter:
    """GPT-OSS Harmony tool definitions, calls and results."""

    def format_tool_definitions(self, tools: list[ToolFormatInput]) -> str:
        if not tools:
            return ""
        return _tools_section(tools)

    def parse_tool_calls(self, response: str) -> list[ParsedToolCall]:
        return parse_harmony_completion(response).tool_calls

    def format_tool_result(self, name: str, result: Result, call_id: str | None = None) -> str:
        # Harmony tool results carry no call id; the author names the tool.
        if result.success:
            text = _result_text(result.data)
        else:
            text = json.dumps({"error": result.error}, ensure_ascii=False, separators=(",", ":"))
        return render_harmony_message(
            HarmonyMessage(
                role="tool",
                content=text,
                channel="commentary",
                recipient="assistant",
                name=_FUNCTIONS_PREFIX + name,
            )
        )

    def model_family(self) -> str:
        return "gpt-oss"

    def validate_format(self, response: str) -> str | None:
        for message in _parse_messages(response):
            if message.role != "assistant" or not message.recipient.startswith(_FUNCTIONS_PREFIX):
                continue
            name = message.recipient[len(_FUNCTIONS_PREFIX) :]
            if not name:
                return "tool call missing function name"
            try:
                args = json.loads(message.content)
            except ValueError as e:
                return f"tool call {name!r} has invalid JSON arguments: {e}"
            if not isinstance(args, dict):
                return f"tool call {name!r} arguments must be a JSON object"
        return None


def render_harmony_message(message: HarmonyMessage) -> str:
    """Render one message. An assistant message with a recipient ends with
    the call token; every other message ends with the end token."""
    out = START
    if message.role == "tool":
        out += message.name
    else:
        out += message.role
        if message.name:
            out += ":" + message.name
    if message.recipient and message.recipient != "all":
        out += " to=" + message.recipient
    if message.channel:
        out += CHANNEL + message.channel
    if message.content_type:
        out += " " + message.content_type
    out += MESSAGE + message.content
    out += CALL if message.role == "assistant" and message.recipient else END
    return out


_DEFAULT_SYSTEM = HarmonySystem()


def render_harmony_prompt(
    messages: Iterable[HarmonyMessage],
    *,
    system: HarmonySystem | None = _DEFAULT_SYSTEM,
    instructions: str = "",
    tools: Sequence[ToolFormatInput] = (),
    auto_drop_analysis: bool = True,
    next_role: str = "assistant",
) -> str:
    """Render a conversation as a Harmony prompt for a raw completion.

    The system message comes first (``system=None`` leaves it out), then a
    developer message holding ``instructions`` and the ``tools`` namespace
    when either is given, then ``messages``. With ``auto_drop_analysis``,
    analysis messages before the first final message are dropped once the
    last assistant message is a final one, as the reference renderer does:
    the chain of thought of a finished answer is not sent back, while an
    unfinished tool-calling turn keeps it. ``next_role`` opens the turn to
    sample; an empty string renders the conversation alone.
    """
    out = ""
    tool_list = list(tools)
    if system is not None:
        out += render_harmony_message(
            HarmonyMessage(role="system", content=_system_text(system, bool(tool_list)))
        )
    if instructions or tool_list:
        sections = []
        if instructions:
            sections += ["# Instructions", instructions]
        if tool_list:
            sections.append(_tools_section(tool_list))
        out += render_harmony_message(HarmonyMessage(role="developer", content="\n\n".join(sections)))
    history = list(messages)
    if auto_drop_analysis:
        history = _drop_analysis(history)
    out += "".join(render_harmony_message(m) for m in history)
    if next_role:
        out += START + next_role
    return out


def parse_harmony_completion(text: str) -> HarmonyCompletion:
    """Parse completion text that follows a prompt ending in ``<|start|>assistant``.

    The first message may have no header role (the prompt opened it). A
    message ends at an end, call or return token, or at the end of the text,
    since servers strip the stop sequence. A call's recipient is read from
    either header position: after the role (the reference renderer) or after
    the channel (what the model emits).
    """
    messages = _parse_messages(text)
    completion = HarmonyCompletion(messages=messages)
    reasoning: list[str] = []
    content: list[str] = []
    for message in messages:
        if message.role != "assistant":
            continue
        if message.recipient.startswith(_FUNCTIONS_PREFIX):
            call = _decode_call(message, text)
            if call is not None:
                completion.tool_calls.append(call)
        elif message.channel == "analysis":
            reasoning.append(message.content)
        elif message.channel == "final" or (not message.channel and not message.recipient):
            content.append(message.content)
    completion.reasoning = "\n".join(reasoning)
    completion.content = "\n".join(content)
    return completion


def _decode_call(message: HarmonyMessage, raw: str) -> ParsedToolCall | None:
    name = message.recipient[len(_FUNCTIONS_PREFIX) :]
    if not name:
        return None
    try:
        args = json.loads(message.content)
    except ValueError:
        return None
    if not isinstance(args, dict):
        return None
    return ParsedToolCall(id="", name=name, args=args, raw=raw)


def _parse_messages(text: str) -> list[HarmonyMessage]:
    messages = []
    for index, chunk in enumerate(text.split(START)):
        if not chunk:
            continue
        message = _parse_chunk(chunk, first=index == 0)
        if message is not None:
            messages.append(message)
    return messages


def _parse_chunk(chunk: str, *, first: bool) -> HarmonyMessage | None:
    if MESSAGE not in chunk:
        # Text before any header is a bare answer; a header with no message
        # token was cut off and carries no content.
        if first and not any(token in chunk for token in (CHANNEL, CONSTRAIN, *_TERMINATORS)):
            return HarmonyMessage(role="assistant", content=chunk)
        return None
    header, body = chunk.split(MESSAGE, 1)
    cut = min((body.find(t) for t in _TERMINATORS if t in body), default=len(body))
    content = body[:cut]

    content_type = ""
    if CONSTRAIN in header:
        header, constrained = header.split(CONSTRAIN, 1)
        content_type = CONSTRAIN + constrained.strip()
    head, _, channel_part = header.partition(CHANNEL)
    recipient = ""
    role_words = []
    for word in head.split():
        if word.startswith("to="):
            recipient = word[3:]
        else:
            role_words.append(word)
    channel = ""
    extra = []
    channel_words = channel_part.split()
    if channel_words:
        channel = channel_words[0]
        for word in channel_words[1:]:
            if word.startswith("to="):
                recipient = word[3:]
            else:
                extra.append(word)
    if not content_type and extra:
        content_type = " ".join(extra)

    role, name = "assistant", ""
    if role_words:
        author = role_words[0]
        base, _, suffix = author.partition(":")
        if base in _ROLES and base != "tool":
            role, name = base, suffix
        else:
            role, name = "tool", author
    return HarmonyMessage(
        role=role,
        content=content,
        channel=channel,
        recipient=recipient,
        content_type=content_type,
        name=name,
    )


def _drop_analysis(messages: list[HarmonyMessage]) -> list[HarmonyMessage]:
    last_assistant_is_final = next(
        (m.channel == "final" for m in reversed(messages) if m.role == "assistant"), False
    )
    first_final = next((i for i, m in enumerate(messages) if m.channel == "final"), None)
    if not last_assistant_is_final or first_final is None:
        return messages
    return [m for i, m in enumerate(messages) if not (i < first_final and m.channel == "analysis")]


def _system_text(system: HarmonySystem, has_function_tools: bool) -> str:
    sections = []
    top = [system.model_identity] if system.model_identity else []
    if system.knowledge_cutoff:
        top.append(f"Knowledge cutoff: {system.knowledge_cutoff}")
    if system.current_date:
        top.append(f"Current date: {system.current_date}")
    if top:
        sections.append("\n".join(top))
    if system.reasoning_effort:
        sections.append(f"Reasoning: {system.reasoning_effort}")
    if system.valid_channels:
        header = (
            f"# Valid channels: {', '.join(system.valid_channels)}."
            " Channel must be included for every message."
        )
        if has_function_tools:
            header += "\nCalls to these tools must go to the commentary channel: 'functions'."
        sections.append(header)
    return "\n\n".join(sections)


def _tools_section(tools: Sequence[ToolFormatInput]) -> str:
    lines = ["## functions\n", "namespace functions {\n"]
    for tool in tools:
        lines += [f"// {line}" for line in _lines(tool.description)]
        schema = schema_of(tool)
        if schema is None:
            lines.append(f"type {tool.name} = () => any;\n")
        else:
            lines.append(f"type {tool.name} = (_: {_typescript(schema, '')}) => any;\n")
    lines.append("} // namespace functions")
    return "# Tools\n\n" + "\n".join(lines)


def _lines(text: str) -> list[str]:
    """Split like Rust's str::lines: no trailing empty line, \\r\\n allowed."""
    if not text:
        return []
    parts = text.split("\n")
    if parts[-1] == "":
        parts.pop()
    return [p[:-1] if p.endswith("\r") else p for p in parts]


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _is_enum(schema: Any) -> bool:
    return isinstance(schema, dict) and isinstance(schema.get("enum"), list) and bool(schema["enum"])


def _default_text(default: Any, schema: Any) -> str:
    """A default as the reference renders it beside a property: a string is
    quoted unless the schema is an enum."""
    if isinstance(default, str):
        return f"default: {default}" if _is_enum(schema) else f'default: "{default}"'
    return f"default: {_json(default)}"


def _get(schema: Any, key: str) -> Any:
    return schema.get(key) if isinstance(schema, dict) else None


def _nullable(type_text: str, schema: Any) -> str:
    if _get(schema, "nullable") is True and "null" not in type_text:
        return type_text + " | null"
    return type_text


def _typescript(schema: Any, indent: str) -> str:
    """Port of json_schema_to_typescript in openai/harmony src/encoding.rs."""
    one_of = _get(schema, "oneOf")
    if isinstance(one_of, list):
        out = ""
        for variant in one_of:
            out += f"\n{indent} | "
            out += _nullable(_typescript(variant, indent + "   "), variant)
            comments = []
            description = _get(variant, "description")
            if isinstance(description, str):
                comments.append(description)
            if isinstance(variant, dict) and "default" in variant:
                default = variant["default"]
                # Here the reference quotes a string default even for an enum.
                if isinstance(default, str) and not _is_enum(variant):
                    comments.append(f'default: "{default}"')
                else:
                    comments.append(f"default: {_json(default)}")
            if comments:
                out += " // " + " ".join(comments)
        return out

    kind = _get(schema, "type")
    if isinstance(kind, list):
        names = ["number" if k == "integer" else k for k in kind if isinstance(k, str)]
        if names:
            return " | ".join(names)
        return "any"
    if kind == "object":
        return _typescript_object(schema, indent)
    if kind == "string":
        enum = _get(schema, "enum")
        values = [f'"{v}"' for v in enum if isinstance(v, str)] if isinstance(enum, list) else []
        return " | ".join(values) if values else "string"
    if kind in ("number", "integer"):
        return "number"
    if kind == "boolean":
        return "boolean"
    if kind == "array":
        items = _get(schema, "items")
        if isinstance(schema, dict) and "items" in schema:
            return f"{_typescript(items, indent)}[]"
        return "Array<any>"
    return "any"


def _typescript_object(schema: dict[str, Any], indent: str) -> str:
    out = ""
    description = schema.get("description")
    if isinstance(description, str):
        out += f"{indent}// {description}\n"
    out += "{\n"
    properties = schema.get("properties")
    if isinstance(properties, dict):
        listed = schema.get("required")
        required = {r for r in listed if isinstance(r, str)} if isinstance(listed, list) else set()
        for key, value in properties.items():
            optional = "" if key in required else "?"
            title = _get(value, "title")
            if isinstance(title, str):
                out += f"{indent}// {title}\n{indent}//\n"
            has_one_of = isinstance(value, dict) and "oneOf" in value
            one_of = _get(value, "oneOf")
            prop_description = _get(value, "description")
            if not has_one_of and isinstance(prop_description, str):
                out += f"{indent}// {prop_description}\n"
            examples = _get(value, "examples")
            if isinstance(examples, list) and examples:
                out += f"{indent}// Examples:\n"
                for example in examples:
                    if isinstance(example, str):
                        out += f'{indent}// - "{example}"\n'
            if isinstance(one_of, list):
                out += _typescript_one_of_property(key, optional, value, one_of, indent)
                continue
            out += f"{indent}{key}{optional}: "
            out += _nullable(_typescript(value, indent + "    "), value) + ","
            if not has_one_of and isinstance(value, dict) and "default" in value:
                out += " // " + _default_text(value["default"], value)
            out += "\n"
    out += f"{indent}}}"
    return out


def _typescript_one_of_property(
    key: str, optional: str, value: dict[str, Any], variants: list[Any], indent: str
) -> str:
    out = ""
    description = value.get("description")
    prop_description = description if isinstance(description, str) else None
    first_description = _get(variants[0], "description") if variants else None
    skip_description = prop_description is not None and first_description == prop_description
    described_above = False
    if prop_description is not None and not skip_description:
        out += f"{indent}// {prop_description}\n"
        described_above = True
    if "default" in value:
        out += f"{indent}// {_default_text(value['default'], value)}\n"
    out += f"{indent}{key}{optional}:\n"
    for i, variant in enumerate(variants):
        out += f"{indent} | " + _nullable(_typescript(variant, indent + "   "), variant)
        comments = []
        variant_description = _get(variant, "description")
        if (
            not (i == 0 and described_above)
            and isinstance(variant_description, str)
            and variant_description != prop_description
        ):
            comments.append(variant_description)
        if isinstance(variant, dict) and "default" in variant:
            comments.append(_default_text(variant["default"], variant))
        if comments:
            out += " // " + " ".join(comments)
        out += "\n"
    out += f"{indent},\n"
    return out
