"""Comprehensive tests for toolformat module."""

import json
import sys

sys.path.insert(0, "src")

from pedro_agentware.toolformat.formatter import (
    AnthropicFormatter,
    DeepSeekFormatter,
    GLMFormatter,
    GenericFormatter,
    OpenAIFOrmatter,
    ParsedToolCall,
    QwenFormatter,
    get_formatter,
)
from pedro_agentware.tools import Result, Tool


# ---------------------------------------------------------------------------
# Mock helpers
# ---------------------------------------------------------------------------


class _MockTool:
    """Minimal tool implementing the Tool protocol."""

    def __init__(self, name: str = "test_tool", description: str = "A test tool"):
        self._name = name
        self._description = description

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return self._description

    def execute(self, args: dict) -> Result:
        return Result(success=True, data=f"executed {self._name}")


class _MockToolWithSchema:
    """Tool that also exposes an input_schema attribute."""

    def __init__(
        self,
        name: str = "schema_tool",
        description: str = "Tool with schema",
        schema: dict | None = None,
    ):
        self._name = name
        self._description = description
        self._schema = schema or {
            "type": "object",
            "properties": {"location": {"type": "string", "description": "City name"}},
            "required": ["location"],
        }

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return self._description

    @property
    def input_schema(self) -> dict:
        return self._schema

    def execute(self, args: dict) -> Result:
        return Result(success=True, data=f"executed {self._name}")


def _assert_parsed_tool_call_eq(
    actual: ParsedToolCall,
    *,
    name: str,
    args: dict,
    id: str = "",
):
    assert actual.name == name, f"expected name {name!r}, got {actual.name!r}"
    assert actual.args == args, f"expected args {args}, got {actual.args}"
    if id:
        assert actual.id == id, f"expected id {id!r}, got {actual.id!r}"


# ===========================================================================
# GenericFormatter tests
# ===========================================================================


class TestGenericFormatter:
    def test_format_tool_definitions_empty(self):
        fmt = GenericFormatter()
        assert fmt.format_tool_definitions([]) == ""

    def test_format_tool_definitions_single(self):
        fmt = GenericFormatter()
        tool = _MockTool(name="get_weather", description="Get weather")
        result = fmt.format_tool_definitions([tool])
        parsed = json.loads(result)
        assert isinstance(parsed, list)
        assert len(parsed) == 1
        entry = parsed[0]
        assert entry["type"] == "function"
        assert entry["function"]["name"] == "get_weather"
        assert entry["function"]["description"] == "Get weather"

    def test_format_tool_definitions_with_schema(self):
        fmt = GenericFormatter()
        tool = _MockToolWithSchema(
            name="get_weather",
            description="Get weather",
            schema={
                "type": "object",
                "properties": {"loc": {"type": "string"}},
                "required": ["loc"],
            },
        )
        result = fmt.format_tool_definitions([tool])
        parsed = json.loads(result)
        assert parsed[0]["function"]["parameters"]["properties"]["loc"]["type"] == "string"

    def test_format_tool_definitions_multiple(self):
        fmt = GenericFormatter()
        t1 = _MockTool(name="tool_a", description="First tool")
        t2 = _MockTool(name="tool_b", description="Second tool")
        result = fmt.format_tool_definitions([t1, t2])
        parsed = json.loads(result)
        assert len(parsed) == 2
        assert parsed[0]["function"]["name"] == "tool_a"
        assert parsed[1]["function"]["name"] == "tool_b"

    def test_parse_tool_calls_empty(self):
        fmt = GenericFormatter()
        assert fmt.parse_tool_calls("") == []

    def test_parse_tool_calls_valid(self):
        fmt = GenericFormatter()
        response = '[{"id":"c1","name":"get_weather","arguments":{"loc":"NYC"}}]'
        calls = fmt.parse_tool_calls(response)
        assert len(calls) == 1
        _assert_parsed_tool_call_eq(calls[0], name="get_weather", args={"loc": "NYC"}, id="c1")
        assert calls[0].raw == response

    def test_parse_tool_calls_invalid_json(self):
        fmt = GenericFormatter()
        assert fmt.parse_tool_calls("not json") == []

    def test_parse_tool_calls_not_a_list(self):
        fmt = GenericFormatter()
        assert fmt.parse_tool_calls('{"not":"a list"}') == []

    def test_parse_tool_calls_args_as_string(self):
        fmt = GenericFormatter()
        response = '[{"id":"c1","name":"get_weather","arguments":"{\\"loc\\":\\"NYC\\"}"}]'
        calls = fmt.parse_tool_calls(response)
        assert len(calls) == 1
        assert calls[0].args == {"loc": "NYC"}

    def test_parse_tool_calls_args_as_bad_string(self):
        fmt = GenericFormatter()
        response = '[{"id":"c1","name":"get_weather","arguments":"not valid json"}]'
        calls = fmt.parse_tool_calls(response)
        assert len(calls) == 0

    def test_parse_tool_calls_skips_non_dict_items(self):
        fmt = GenericFormatter()
        response = '[42, "string", null]'
        calls = fmt.parse_tool_calls(response)
        assert len(calls) == 0

    def test_parse_tool_calls_missing_args(self):
        fmt = GenericFormatter()
        response = '[{"id":"c1","name":"get_weather"}]'
        calls = fmt.parse_tool_calls(response)
        assert len(calls) == 0

    def test_format_tool_result_success(self):
        fmt = GenericFormatter()
        result = Result(success=True, data="Sunny, 22°C")
        output = fmt.format_tool_result("get_weather", result)
        assert output == "Sunny, 22°C"

    def test_format_tool_result_success_none_data(self):
        fmt = GenericFormatter()
        result = Result(success=True, data=None)
        output = fmt.format_tool_result("get_weather", result)
        assert output == ""

    def test_format_tool_result_error(self):
        fmt = GenericFormatter()
        result = Result(success=False, error="API unavailable")
        output = fmt.format_tool_result("get_weather", result)
        assert output == "Error: API unavailable"

    def test_model_family(self):
        fmt = GenericFormatter()
        assert fmt.model_family() == "generic"

    def test_validate_format_valid(self):
        fmt = GenericFormatter()
        assert (
            fmt.validate_format('[{"id":"c1","name":"get_weather","arguments":{"loc":"NYC"}}]')
            is None
        )

    def test_validate_format_empty(self):
        fmt = GenericFormatter()
        assert fmt.validate_format("") is None

    def test_validate_format_invalid_json(self):
        fmt = GenericFormatter()
        error = fmt.validate_format("not json at all")
        assert error is not None
        assert "invalid JSON" in error

    def test_validate_format_not_a_list(self):
        fmt = GenericFormatter()
        error = fmt.validate_format('{"not":"a list"}')
        assert error is not None
        assert "expected a JSON array" in error

    def test_validate_format_missing_name(self):
        fmt = GenericFormatter()
        error = fmt.validate_format('[{"id":"c1","arguments":{"loc":"NYC"}}]')
        assert error is not None
        assert "missing 'name'" in error

    def test_validate_format_missing_arguments(self):
        fmt = GenericFormatter()
        error = fmt.validate_format('[{"id":"c1","name":"get_weather"}]')
        assert error is not None
        assert "missing 'arguments'" in error

    def test_validate_format_non_dict_item(self):
        fmt = GenericFormatter()
        error = fmt.validate_format("[42]")
        assert error is not None
        assert "not an object" in error


# ===========================================================================
# OpenAIFOrmatter tests
# ===========================================================================


class TestOpenAIFOrmatter:
    def test_model_family(self):
        fmt = OpenAIFOrmatter()
        assert fmt.model_family() == "openai"

    def test_inherits_format_tool_definitions(self):
        fmt = OpenAIFOrmatter()
        tool = _MockTool(name="my_tool", description="Does something")
        result = fmt.format_tool_definitions([tool])
        parsed = json.loads(result)
        assert len(parsed) == 1
        assert parsed[0]["function"]["name"] == "my_tool"

    def test_inherits_parse_tool_calls(self):
        fmt = OpenAIFOrmatter()
        calls = fmt.parse_tool_calls('[{"id":"c1","name":"test","arguments":{"a":1}}]')
        assert len(calls) == 1
        assert calls[0].name == "test"

    def test_inherits_format_tool_result(self):
        fmt = OpenAIFOrmatter()
        r = Result(success=True, data="done")
        assert fmt.format_tool_result("t", r) == "done"

    def test_inherits_validate_format(self):
        fmt = OpenAIFOrmatter()
        assert fmt.validate_format('[{"id":"c1","name":"t","arguments":{}}]') is None
        assert fmt.validate_format("bad") is not None


# ===========================================================================
# AnthropicFormatter tests
# ===========================================================================


class TestAnthropicFormatter:
    def test_format_tool_definitions_empty(self):
        fmt = AnthropicFormatter()
        assert fmt.format_tool_definitions([]) == ""

    def test_format_tool_definitions(self):
        fmt = AnthropicFormatter()
        tool = _MockTool(name="get_weather", description="Get weather")
        result = fmt.format_tool_definitions([tool])
        parsed = json.loads(result)
        assert isinstance(parsed, list)
        assert len(parsed) == 1
        assert parsed[0]["type"] == "function"
        assert parsed[0]["function"]["name"] == "get_weather"

    def test_format_tool_definitions_with_schema(self):
        fmt = AnthropicFormatter()
        tool = _MockToolWithSchema(
            name="search",
            description="Search",
            schema={"type": "object", "properties": {"q": {"type": "string"}}, "required": ["q"]},
        )
        result = fmt.format_tool_definitions([tool])
        parsed = json.loads(result)
        assert parsed[0]["function"]["parameters"]["properties"]["q"]["type"] == "string"

    def test_parse_tool_calls_empty(self):
        fmt = AnthropicFormatter()
        assert fmt.parse_tool_calls("") == []

    def test_parse_tool_calls_valid(self):
        fmt = AnthropicFormatter()
        response = (
            '<function_calls><invoke tool="get_weather">{"loc":"NYC"}</invoke></function_calls>'
        )
        calls = fmt.parse_tool_calls(response)
        assert len(calls) == 1
        _assert_parsed_tool_call_eq(calls[0], name="get_weather", args={"loc": "NYC"})
        assert calls[0].raw == response

    def test_parse_tool_calls_multiple(self):
        fmt = AnthropicFormatter()
        response = (
            "<function_calls>"
            '<invoke tool="get_weather">{"loc":"Paris"}</invoke>'
            '<invoke tool="search_web">{"query":"forecast"}</invoke>'
            "</function_calls>"
        )
        calls = fmt.parse_tool_calls(response)
        assert len(calls) == 2
        assert calls[0].name == "get_weather"
        assert calls[0].args == {"loc": "Paris"}
        assert calls[1].name == "search_web"
        assert calls[1].args == {"query": "forecast"}

    def test_parse_tool_calls_no_calls(self):
        fmt = AnthropicFormatter()
        assert fmt.parse_tool_calls("I'll look that up for you.") == []

    def test_parse_tool_calls_no_function_calls_block(self):
        fmt = AnthropicFormatter()
        assert fmt.parse_tool_calls("Just some text.") == []

    def test_parse_tool_calls_empty_args(self):
        fmt = AnthropicFormatter()
        response = '<function_calls><invoke tool="my_tool"></invoke></function_calls>'
        calls = fmt.parse_tool_calls(response)
        assert len(calls) == 0

    def test_parse_tool_calls_bad_json_args(self):
        fmt = AnthropicFormatter()
        response = '<function_calls><invoke tool="my_tool">{bad}</invoke></function_calls>'
        calls = fmt.parse_tool_calls(response)
        assert len(calls) == 0

    def test_parse_tool_calls_non_dict_json(self):
        fmt = AnthropicFormatter()
        response = (
            '<function_calls><invoke tool="my_tool">"just a string"</invoke></function_calls>'
        )
        calls = fmt.parse_tool_calls(response)
        assert len(calls) == 0

    def test_format_tool_result_success(self):
        fmt = AnthropicFormatter()
        result = Result(success=True, data="Sunny, 22°C")
        output = fmt.format_tool_result("get_weather", result)
        assert (
            output
            == '<function_results><result tool="get_weather">Sunny, 22°C</result></function_results>'
        )

    def test_format_tool_result_success_none_data(self):
        fmt = AnthropicFormatter()
        result = Result(success=True, data=None)
        output = fmt.format_tool_result("get_weather", result)
        assert output == '<function_results><result tool="get_weather"></result></function_results>'

    def test_format_tool_result_error(self):
        fmt = AnthropicFormatter()
        result = Result(success=False, error="API unavailable")
        output = fmt.format_tool_result("get_weather", result)
        assert (
            output
            == '<function_results><result tool="get_weather">Error: API unavailable</result></function_results>'
        )

    def test_model_family(self):
        fmt = AnthropicFormatter()
        assert fmt.model_family() == "anthropic"

    def test_validate_format_valid(self):
        fmt = AnthropicFormatter()
        assert (
            fmt.validate_format(
                '<function_calls><invoke tool="get_weather">{}</invoke></function_calls>'
            )
            is None
        )

    def test_validate_format_empty(self):
        fmt = AnthropicFormatter()
        assert fmt.validate_format("") is None

    def test_validate_format_missing_tool_attribute(self):
        fmt = AnthropicFormatter()
        error = fmt.validate_format("<function_calls><invoke>{}</invoke></function_calls>")
        assert error is not None
        assert "missing 'tool' attribute" in error

    def test_validate_format_empty_tool_name(self):
        fmt = AnthropicFormatter()
        error = fmt.validate_format(
            '<function_calls><invoke tool="">{"a":1}</invoke></function_calls>'
        )
        assert error is not None
        assert "missing 'tool' attribute" in error

    def test_validate_format_missing_arguments(self):
        fmt = AnthropicFormatter()
        error = fmt.validate_format(
            '<function_calls><invoke tool="my_tool"></invoke></function_calls>'
        )
        assert error is not None
        assert "missing arguments" in error

    def test_validate_format_bad_json_args(self):
        fmt = AnthropicFormatter()
        error = fmt.validate_format(
            '<function_calls><invoke tool="my_tool">{bad}</invoke></function_calls>'
        )
        assert error is not None
        assert "invalid JSON arguments" in error


# ===========================================================================
# QwenFormatter tests
# ===========================================================================


class TestQwenFormatter:
    def test_format_tool_definitions_empty(self):
        fmt = QwenFormatter()
        assert fmt.format_tool_definitions([]) == ""

    def test_format_tool_definitions(self):
        fmt = QwenFormatter()
        tool = _MockTool(name="get_weather", description="Get current weather")
        result = fmt.format_tool_definitions([tool])
        assert "<tool_description>" in result
        assert "<tool_name>get_weather</tool_name>" in result
        assert "<description>Get current weather</description>" in result

    def test_format_tool_definitions_with_schema(self):
        fmt = QwenFormatter()
        tool = _MockToolWithSchema(
            name="search",
            description="Search tool",
            schema={"type": "object", "properties": {"q": {"type": "string"}}, "required": ["q"]},
        )
        result = fmt.format_tool_definitions([tool])
        assert "<parameters>" in result
        assert '"q"' in result

    def test_format_tool_definitions_no_schema(self):
        fmt = QwenFormatter()
        tool = _MockTool(name="simple", description="No schema tool")
        result = fmt.format_tool_definitions([tool])
        assert "<tool_description>" in result
        assert "<parameters>" not in result

    def test_format_tool_definitions_multiple(self):
        fmt = QwenFormatter()
        t1 = _MockTool(name="tool_a", description="First")
        t2 = _MockTool(name="tool_b", description="Second")
        result = fmt.format_tool_definitions([t1, t2])
        assert result.count("<tool_description>") == 2
        assert result.count("<tool_name>") == 2
        assert "<tool_name>tool_a</tool_name>" in result
        assert "<tool_name>tool_b</tool_name>" in result

    def test_parse_tool_calls_empty(self):
        fmt = QwenFormatter()
        assert fmt.parse_tool_calls("") == []

    def test_parse_tool_calls_valid(self):
        fmt = QwenFormatter()
        response = '<tool_call><tool name="get_weather">{"loc":"NYC"}</tool></tool_call>'
        calls = fmt.parse_tool_calls(response)
        assert len(calls) == 1
        _assert_parsed_tool_call_eq(calls[0], name="get_weather", args={"loc": "NYC"})
        assert calls[0].raw == response

    def test_parse_tool_calls_multiple(self):
        fmt = QwenFormatter()
        response = (
            '<tool_call><tool name="get_weather">{"loc":"Paris"}</tool></tool_call>'
            '<tool_call><tool name="search_web">{"query":"forecast"}</tool></tool_call>'
        )
        calls = fmt.parse_tool_calls(response)
        assert len(calls) == 2
        assert calls[0].name == "get_weather"
        assert calls[0].args == {"loc": "Paris"}
        assert calls[1].name == "search_web"
        assert calls[1].args == {"query": "forecast"}

    def test_parse_tool_calls_no_calls(self):
        fmt = QwenFormatter()
        assert fmt.parse_tool_calls("Let me check the weather.") == []

    def test_parse_tool_calls_empty_args(self):
        fmt = QwenFormatter()
        response = '<tool_call><tool name="my_tool"></tool></tool_call>'
        calls = fmt.parse_tool_calls(response)
        assert len(calls) == 1
        assert calls[0].args == {}

    def test_parse_tool_calls_bad_json_args(self):
        fmt = QwenFormatter()
        response = '<tool_call><tool name="my_tool">{bad}</tool></tool_call>'
        calls = fmt.parse_tool_calls(response)
        assert len(calls) == 1
        assert calls[0].args == {"_raw": "{bad}"}

    def test_format_tool_result_success(self):
        fmt = QwenFormatter()
        result = Result(success=True, data="Sunny, 22°C")
        output = fmt.format_tool_result("get_weather", result)
        assert "<tool_response>" in output
        assert "<tool_name>get_weather</tool_name>" in output
        assert "<result>Sunny, 22°C</result>" in output
        assert "</tool_response>" in output

    def test_format_tool_result_success_none_data(self):
        fmt = QwenFormatter()
        result = Result(success=True, data=None)
        output = fmt.format_tool_result("get_weather", result)
        assert "<result></result>" in output

    def test_format_tool_result_error(self):
        fmt = QwenFormatter()
        result = Result(success=False, error="API unavailable")
        output = fmt.format_tool_result("get_weather", result)
        assert "<tool_response>" in output
        assert "<tool_name>get_weather</tool_name>" in output
        assert "<error>API unavailable</error>" in output
        assert "</tool_response>" in output

    def test_model_family(self):
        fmt = QwenFormatter()
        assert fmt.model_family() == "qwen"

    def test_validate_format_valid(self):
        fmt = QwenFormatter()
        assert (
            fmt.validate_format('<tool_call><tool name="get_weather">{}</tool></tool_call>') is None
        )

    def test_validate_format_empty(self):
        fmt = QwenFormatter()
        assert fmt.validate_format("") is None

    def test_validate_format_missing_name(self):
        fmt = QwenFormatter()
        error = fmt.validate_format("<tool_call><tool>{}</tool></tool_call>")
        assert error is not None
        assert "missing function name" in error

    def test_validate_format_empty_name(self):
        """Qwen treats empty name="" as non-matching (tool_call_re requires [^"]+)."""
        fmt = QwenFormatter()
        assert fmt.validate_format('<tool_call><tool name="">{"a":1}</tool></tool_call>') is None

    def test_validate_format_missing_arguments(self):
        fmt = QwenFormatter()
        error = fmt.validate_format('<tool_call><tool name="my_tool"></tool></tool_call>')
        assert error is not None
        assert "missing arguments" in error

    def test_validate_format_malformed_json_args(self):
        fmt = QwenFormatter()
        error = fmt.validate_format('<tool_call><tool name="get_weather">{bad}</tool></tool_call>')
        assert error is not None
        assert "invalid JSON arguments" in error


# ===========================================================================
# DeepSeekFormatter tests
# ===========================================================================


class TestDeepSeekFormatter:
    def test_model_family(self):
        fmt = DeepSeekFormatter()
        assert fmt.model_family() == "deepseek"

    def test_inherits_format_tool_definitions(self):
        fmt = DeepSeekFormatter()
        tool = _MockTool(name="my_tool", description="Does something")
        result = fmt.format_tool_definitions([tool])
        parsed = json.loads(result)
        assert parsed[0]["function"]["name"] == "my_tool"

    def test_inherits_parse_tool_calls(self):
        fmt = DeepSeekFormatter()
        calls = fmt.parse_tool_calls('[{"id":"c1","name":"test","arguments":{"a":1}}]')
        assert len(calls) == 1
        assert calls[0].name == "test"

    def test_inherits_format_tool_result(self):
        fmt = DeepSeekFormatter()
        r = Result(success=True, data="done")
        assert fmt.format_tool_result("t", r) == "done"

    def test_inherits_validate_format(self):
        fmt = DeepSeekFormatter()
        assert fmt.validate_format('[{"id":"c1","name":"t","arguments":{}}]') is None
        assert fmt.validate_format("bad") is not None


# ===========================================================================
# GLMFormatter tests
# ===========================================================================


class TestGLMFormatter:
    def test_model_family(self):
        fmt = GLMFormatter()
        assert fmt.model_family() == "glm"

    def test_inherits_format_tool_definitions(self):
        fmt = GLMFormatter()
        tool = _MockTool(name="my_tool", description="Does something")
        result = fmt.format_tool_definitions([tool])
        parsed = json.loads(result)
        assert parsed[0]["function"]["name"] == "my_tool"

    def test_inherits_parse_tool_calls(self):
        fmt = GLMFormatter()
        calls = fmt.parse_tool_calls('[{"id":"c1","name":"test","arguments":{"a":1}}]')
        assert len(calls) == 1
        assert calls[0].name == "test"

    def test_inherits_format_tool_result(self):
        fmt = GLMFormatter()
        r = Result(success=True, data="done")
        assert fmt.format_tool_result("t", r) == "done"

    def test_inherits_validate_format(self):
        fmt = GLMFormatter()
        assert fmt.validate_format('[{"id":"c1","name":"t","arguments":{}}]') is None
        assert fmt.validate_format("bad") is not None


# ===========================================================================
# get_formatter selector tests
# ===========================================================================


class TestGetFormatter:
    def test_get_formatter_qwen(self):
        fmt = get_formatter("qwen2.5")
        assert isinstance(fmt, QwenFormatter)
        assert fmt.model_family() == "qwen"

    def test_get_formatter_qwq(self):
        fmt = get_formatter("qwq-32b")
        assert isinstance(fmt, QwenFormatter)
        assert fmt.model_family() == "qwen"

    def test_get_formatter_anthropic(self):
        fmt = get_formatter("claude-3")
        assert isinstance(fmt, AnthropicFormatter)
        assert fmt.model_family() == "anthropic"

    def test_get_formatter_anthropic_lower(self):
        fmt = get_formatter("anthropic.claude-v3")
        assert isinstance(fmt, AnthropicFormatter)

    def test_get_formatter_deepseek(self):
        fmt = get_formatter("deepseek-chat")
        assert isinstance(fmt, DeepSeekFormatter)
        assert fmt.model_family() == "deepseek"

    def test_get_formatter_deepseek_r1(self):
        fmt = get_formatter("DeepSeek-R1")
        assert isinstance(fmt, DeepSeekFormatter)

    def test_get_formatter_glm(self):
        fmt = get_formatter("glm-4")
        assert isinstance(fmt, GLMFormatter)
        assert fmt.model_family() == "glm"

    def test_get_formatter_chatglm(self):
        fmt = get_formatter("chatglm-3")
        assert isinstance(fmt, GLMFormatter)
        assert fmt.model_family() == "glm"

    def test_get_formatter_gpt(self):
        fmt = get_formatter("gpt-4")
        assert isinstance(fmt, OpenAIFOrmatter)
        assert fmt.model_family() == "openai"

    def test_get_formatter_gpt35(self):
        fmt = get_formatter("gpt-3.5-turbo")
        assert isinstance(fmt, OpenAIFOrmatter)

    def test_get_formatter_o1(self):
        fmt = get_formatter("o1-preview")
        assert isinstance(fmt, OpenAIFOrmatter)
        assert fmt.model_family() == "openai"

    def test_get_formatter_o3(self):
        fmt = get_formatter("o3-mini")
        assert isinstance(fmt, OpenAIFOrmatter)
        assert fmt.model_family() == "openai"

    def test_get_formatter_unknown(self):
        fmt = get_formatter("unknown-model")
        assert isinstance(fmt, GenericFormatter)
        assert fmt.model_family() == "generic"

    def test_get_formatter_llama(self):
        fmt = get_formatter("llama-3")
        assert isinstance(fmt, GenericFormatter)

    def test_get_formatter_mistral(self):
        fmt = get_formatter("mistral-large")
        assert isinstance(fmt, GenericFormatter)

    def test_get_formatter_case_insensitive(self):
        fmt = get_formatter("QWEN2.5")
        assert isinstance(fmt, QwenFormatter)
