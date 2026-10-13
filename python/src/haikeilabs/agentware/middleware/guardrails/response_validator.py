"""Response validator for parsing and validating LLM tool call responses."""

import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from .nudge import Nudge, retry_nudge, unknown_tool_nudge


@dataclass
class ToolCall:
    """A tool call extracted from an LLM response."""

    tool: str
    args: dict[str, Any] = field(default_factory=dict)


@dataclass
class ValidationResult:
    """Result of validating an LLM response."""

    tool_calls: list[ToolCall]
    nudge: Nudge | None = None
    needs_retry: bool = False


class ResponseValidator:
    """Validates LLM text responses and extracts tool calls."""

    def __init__(
        self,
        tool_names: list[str],
        rescue_enabled: bool = True,
        retry_nudge_fn: Callable[[str, list[str]], Nudge] | None = None,
    ):
        """Initialize the ResponseValidator.

        Args:
            tool_names: List of valid tool names.
            rescue_enabled: Whether to attempt to rescue text responses into tool calls.
            retry_nudge_fn: Custom function to generate retry nudges.
        """
        self.tool_names = set(tool_names)
        self.rescue_enabled = rescue_enabled
        self.retry_nudge_fn = retry_nudge_fn or retry_nudge

        self._think_pattern = re.compile(r"(?i)\[THINK\].*?\[/THINK\]|<think>.*?</think>")
        self._python_tag_pattern = re.compile(r"(?i)<\|python_tag\|>")
        self._code_fence_pattern = re.compile(r"```(?:json)?\s*\n?")
        self._rehearsal_pattern = re.compile(r"(\w+)\[ARGS\](\{.*?\})")
        self._qwen_function_pattern = re.compile(r"<function=([^>\s]+)>(.*?)</function>")

    def validate_text_response(self, response: str) -> ValidationResult:
        """Validate a text response and attempt to rescue tool calls."""
        if self.rescue_enabled:
            rescued = self._rescue_tool_call(response)
            if rescued:
                return ValidationResult(tool_calls=rescued, nudge=None, needs_retry=False)

        tool_names_list = list(self.tool_names)
        nudge = self.retry_nudge_fn(response, tool_names_list)
        return ValidationResult(tool_calls=[], nudge=nudge, needs_retry=True)

    def validate_tool_calls(self, tool_calls: list[ToolCall]) -> ValidationResult:
        """Validate a list of tool calls against available tools."""
        unknown = []
        valid_calls = []

        for tc in tool_calls:
            if tc.tool not in self.tool_names:
                unknown.append(tc.tool)
            else:
                valid_calls.append(tc)

        if unknown:
            tool_names_list = list(self.tool_names)
            nudge = unknown_tool_nudge(unknown[0], tool_names_list)
            return ValidationResult(tool_calls=[], nudge=nudge, needs_retry=True)

        return ValidationResult(tool_calls=valid_calls, nudge=None, needs_retry=False)

    def _rescue_tool_call(self, response: str) -> list[ToolCall]:
        """Attempt to extract tool calls from text response."""
        cleaned = self._think_pattern.sub("", response)
        cleaned = self._python_tag_pattern.sub("", cleaned)
        cleaned = cleaned.strip()

        if not cleaned:
            return []

        calls = self._extract_json_tool_calls(cleaned)
        if calls:
            return calls

        calls = self._extract_rehearsal_tool_calls(cleaned)
        if calls:
            return calls

        return self._extract_qwen_xml_tool_calls(cleaned)

    def _extract_json_tool_calls(self, text: str) -> list[ToolCall]:
        """Extract tool calls from JSON objects in text."""
        cleaned = self._code_fence_pattern.sub("", text)
        cleaned = cleaned.strip()

        calls = []
        i = 0
        while i < len(cleaned):
            if cleaned[i] == "{":
                depth = 0
                j = i
                while j < len(cleaned):
                    if cleaned[j] == "{":
                        depth += 1
                    elif cleaned[j] == "}":
                        depth -= 1
                        if depth == 0:
                            candidate = cleaned[i : j + 1]
                            call = self._try_parse_tool_call(candidate)
                            if call:
                                calls.append(call)
                            i = j + 1
                            break
                    j += 1
                if depth != 0:
                    i += 1
            else:
                i += 1

        return calls

    def _try_parse_tool_call(self, json_str: str) -> ToolCall | None:
        """Try to parse a JSON string as a tool call."""
        try:
            data = json.loads(json_str)
        except json.JSONDecodeError:
            return None

        tool_name = data.get("tool") or data.get("name")
        if not tool_name:
            return None

        if tool_name not in self.tool_names:
            return None

        args = data.get("args") or data.get("arguments") or {}

        return ToolCall(tool=tool_name, args=args)

    def _extract_rehearsal_tool_calls(self, text: str) -> list[ToolCall]:
        """Extract tool calls from rehearsal format: toolName[ARGS]{...}."""
        matches = self._rehearsal_pattern.findall(text)
        calls = []

        for tool_name, args_str in matches:
            if tool_name not in self.tool_names:
                continue

            try:
                args = json.loads(args_str)
                if isinstance(args, dict):
                    calls.append(ToolCall(tool=tool_name, args=args))
            except json.JSONDecodeError:
                continue

        return calls

    def _extract_qwen_xml_tool_calls(self, text: str) -> list[ToolCall]:
        """Extract tool calls from Qwen XML format (not implemented)."""
        return []
