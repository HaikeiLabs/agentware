"""
General tool calling test cases.
"""

from haikeilabs.agentware.evals.runner import EvalCase

GENERAL_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "calculator",
            "description": "Perform mathematical calculations",
            "parameters": {
                "type": "object",
                "properties": {
                    "expression": {
                        "type": "string",
                        "description": "Mathematical expression to evaluate",
                    }
                },
                "required": ["expression"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "Get weather information for a location",
            "parameters": {
                "type": "object",
                "properties": {
                    "location": {"type": "string", "description": "City name"},
                    "units": {
                        "type": "string",
                        "enum": ["celsius", "fahrenheit"],
                        "description": "Temperature units",
                    },
                },
                "required": ["location"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "translate",
            "description": "Translate text between languages",
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "Text to translate"},
                    "target_lang": {"type": "string", "description": "Target language code"},
                    "source_lang": {
                        "type": "string",
                        "description": "Source language code (optional, auto-detect if not provided)",
                    },
                },
                "required": ["text", "target_lang"],
            },
        },
    },
]

GENERAL_CASES = [
    EvalCase(
        name="calculator_add",
        description="Simple addition calculation",
        system_prompt="You are a helpful assistant with access to tools. Use them when needed.",
        user_message="What is 123 + 456?",
        tools=GENERAL_TOOLS,
        expected_tool="calculator",
    ),
    EvalCase(
        name="calculator_complex",
        description="Complex mathematical expression",
        system_prompt="You are a helpful assistant with access to tools. Use them when needed.",
        user_message="Calculate (15 * 8) + (100 / 4) - 50",
        tools=GENERAL_TOOLS,
        expected_tool="calculator",
    ),
    EvalCase(
        name="get_weather",
        description="Get weather for a city",
        system_prompt="You are a helpful assistant with access to tools. Use them when needed.",
        user_message="What's the weather like in Tokyo?",
        tools=GENERAL_TOOLS,
        expected_tool="get_weather",
    ),
    EvalCase(
        name="translate_english_to_spanish",
        description="Translate text to Spanish",
        system_prompt="You are a helpful assistant with access to tools. Use them when needed.",
        user_message="Translate 'Hello, how are you?' to Spanish",
        tools=GENERAL_TOOLS,
        expected_tool="translate",
    ),
    EvalCase(
        name="translate_with_source",
        description="Translate with specified source language",
        system_prompt="You are a helpful assistant with access to tools. Use them when needed.",
        user_message="Translate 'Bonjour' from French to English",
        tools=GENERAL_TOOLS,
        expected_tool="translate",
    ),
]
