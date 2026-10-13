
from haikeilabs.agentware.middleware.guardrails.nudge import NudgeKind
from haikeilabs.agentware.middleware.guardrails.response_validator import (
    ResponseValidator,
    ToolCall,
)


class TestResponseValidator:
    """Tests for ResponseValidator."""

    def test_new_response_validator(self):
        """Test creating a new ResponseValidator."""
        tools = ["tool1", "tool2"]
        rv = ResponseValidator(tools, rescue_enabled=True)

        assert len(rv.tool_names) == 2
        assert rv.rescue_enabled is True

    def test_validate_tool_calls_valid(self):
        """Test validating valid tool calls."""
        tools = ["tool1", "tool2"]
        rv = ResponseValidator(tools, rescue_enabled=False)

        calls = [ToolCall(tool="tool1", args={"key": "value"})]
        result = rv.validate_tool_calls(calls)

        assert result.needs_retry is False
        assert result.nudge is None
        assert len(result.tool_calls) == 1

    def test_validate_tool_calls_invalid(self):
        """Test validating invalid tool calls."""
        tools = ["tool1", "tool2"]
        rv = ResponseValidator(tools, rescue_enabled=False)

        calls = [ToolCall(tool="unknown_tool", args={})]
        result = rv.validate_tool_calls(calls)

        assert result.needs_retry is True
        assert result.nudge is not None
        assert result.nudge.kind == NudgeKind.UNKNOWN_TOOL

    def test_validate_text_response_with_rescue(self):
        """Test rescuing a text response into tool calls."""
        tools = ["tool1", "tool2"]
        rv = ResponseValidator(tools, rescue_enabled=True)

        response = '{"tool": "tool1", "args": {"key": "value"}}'
        result = rv.validate_text_response(response)

        assert result.needs_retry is False
        assert len(result.tool_calls) == 1

    def test_validate_text_response_without_rescue(self):
        """Test text response without rescue enabled."""
        tools = ["tool1", "tool2"]
        rv = ResponseValidator(tools, rescue_enabled=False)

        response = "This is just text."
        result = rv.validate_text_response(response)

        assert result.needs_retry is True
        assert result.nudge is not None
        assert result.nudge.kind == NudgeKind.RETRY

    def test_validate_text_response_empty(self):
        """Test empty text response."""
        tools = ["tool1", "tool2"]
        rv = ResponseValidator(tools, rescue_enabled=True)

        result = rv.validate_text_response("")

        assert result.needs_retry is True

    def test_rescue_tool_call_strips_think_tags(self):
        """Test that think tags are stripped."""
        tools = ["tool1", "tool2"]
        rv = ResponseValidator(tools, rescue_enabled=True)

        response = "[THINK] thinking [/THINK]\n{\"tool\": \"tool1\", \"args\": {}}"
        calls = rv._rescue_tool_call(response)

        assert len(calls) == 1

    def test_rescue_tool_call_strips_python_tag(self):
        """Test that python tags are stripped."""
        tools = ["tool1", "tool2"]
        rv = ResponseValidator(tools, rescue_enabled=True)

        response = "<|python_tag|>{\"tool\": \"tool1\", \"args\": {}}"
        calls = rv._rescue_tool_call(response)

        assert len(calls) == 1

    def test_extract_json_tool_calls_with_code_fence(self):
        """Test extracting JSON from code fences."""
        tools = ["tool1", "tool2"]
        rv = ResponseValidator(tools, rescue_enabled=True)

        response = '```json\n{"tool": "tool1", "args": {"key": "value"}}\n```'
        calls = rv._extract_json_tool_calls(response)

        assert len(calls) == 1

    def test_extract_rehearsal_tool_calls(self):
        """Test extracting rehearsal format tool calls."""
        tools = ["tool1", "tool2"]
        rv = ResponseValidator(tools, rescue_enabled=True)

        response = 'tool1[ARGS]{"key": "value"}'
        calls = rv._extract_rehearsal_tool_calls(response)

        assert len(calls) == 1

    def test_try_parse_tool_call_valid(self):
        """Test parsing valid JSON tool call."""
        tools = ["tool1", "tool2"]
        rv = ResponseValidator(tools, rescue_enabled=True)

        json_str = '{"tool": "tool1", "args": {"key": "value"}}'
        call = rv._try_parse_tool_call(json_str)

        assert call is not None
        assert call.tool == "tool1"

    def test_try_parse_tool_call_invalid_json(self):
        """Test parsing invalid JSON."""
        tools = ["tool1", "tool2"]
        rv = ResponseValidator(tools, rescue_enabled=True)

        call = rv._try_parse_tool_call("not valid json")

        assert call is None

    def test_try_parse_tool_call_unknown_tool(self):
        """Test parsing with unknown tool."""
        tools = ["tool1"]
        rv = ResponseValidator(tools, rescue_enabled=True)

        json_str = '{"tool": "unknown", "args": {}}'
        call = rv._try_parse_tool_call(json_str)

        assert call is None

    def test_extract_qwen_xml_tool_calls(self):
        """Test Qwen XML extraction (not implemented)."""
        tools = ["tool1", "tool2"]
        rv = ResponseValidator(tools, rescue_enabled=True)

        calls = rv._extract_qwen_xml_tool_calls("<function=tool1>content</function>")

        assert len(calls) == 0
