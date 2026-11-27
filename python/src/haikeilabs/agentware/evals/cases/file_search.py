"""
File search tool call test cases.
"""

from haikeilabs.agentware.evals.runner import EvalCase

FILE_SEARCH_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "glob",
            "description": "Find files matching a glob pattern in a directory",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "Glob pattern to match files"},
                    "directory": {"type": "string", "description": "Directory to search in"},
                },
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read contents of a file",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Path to the file to read"}
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_files",
            "description": "Search for text content in files",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Text to search for"},
                    "path": {"type": "string", "description": "Directory path to search in"},
                },
                "required": ["query"],
            },
        },
    },
]

FILE_SEARCH_CASES = [
    EvalCase(
        name="glob_python_files",
        description="Find all Python files in the current directory",
        system_prompt="You are a helpful assistant with file system access. Use the provided tools to help the user.",
        user_message="Find all Python files in the current directory",
        tools=FILE_SEARCH_TOOLS,
        expected_tool="glob",
    ),
    EvalCase(
        name="glob_md_files",
        description="Find all Markdown files",
        system_prompt="You are a helpful assistant with file system access. Use the provided tools to help the user.",
        user_message="List all markdown files (*.md) in this directory",
        tools=FILE_SEARCH_TOOLS,
        expected_tool="glob",
    ),
    EvalCase(
        name="search_code",
        description="Search for specific code pattern",
        system_prompt="You are a helpful assistant with file system access. Use the provided tools to help the user.",
        user_message="Search for all files containing 'func main'",
        tools=FILE_SEARCH_TOOLS,
        expected_tool="search_files",
    ),
    EvalCase(
        name="read_config",
        description="Read a configuration file",
        system_prompt="You are a helpful assistant with file system access. Use the provided tools to help the user.",
        user_message="Read the contents of go.mod",
        tools=FILE_SEARCH_TOOLS,
        expected_tool="read_file",
    ),
]
