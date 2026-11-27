"""
GitHub tool test cases.
"""

from haikeilabs.agentware.evals.runner import EvalCase

GITHUB_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "list_prs",
            "description": "List open pull requests in a repository",
            "parameters": {
                "type": "object",
                "properties": {
                    "owner": {"type": "string", "description": "Repository owner"},
                    "repo": {"type": "string", "description": "Repository name"},
                    "state": {
                        "type": "string",
                        "enum": ["open", "closed", "all"],
                        "description": "PR state",
                    },
                },
                "required": ["owner", "repo"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_issues",
            "description": "List issues in a repository",
            "parameters": {
                "type": "object",
                "properties": {
                    "owner": {"type": "string", "description": "Repository owner"},
                    "repo": {"type": "string", "description": "Repository name"},
                    "state": {
                        "type": "string",
                        "enum": ["open", "closed", "all"],
                        "description": "Issue state",
                    },
                },
                "required": ["owner", "repo"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_issue",
            "description": "Create a new issue",
            "parameters": {
                "type": "object",
                "properties": {
                    "owner": {"type": "string", "description": "Repository owner"},
                    "repo": {"type": "string", "description": "Repository name"},
                    "title": {"type": "string", "description": "Issue title"},
                    "body": {"type": "string", "description": "Issue body"},
                },
                "required": ["owner", "repo", "title"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_workflow_status",
            "description": "Get CI/CD workflow status",
            "parameters": {
                "type": "object",
                "properties": {
                    "owner": {"type": "string", "description": "Repository owner"},
                    "repo": {"type": "string", "description": "Repository name"},
                    "workflow_id": {"type": "string", "description": "Workflow ID or name"},
                },
                "required": ["owner", "repo"],
            },
        },
    },
]

GITHUB_CASES = [
    EvalCase(
        name="list_prs",
        description="List open pull requests",
        system_prompt="You are a helpful assistant with GitHub access. Use tools to help the user.",
        user_message="Show me the open pull requests in soypete/pedro-agentware",
        tools=GITHUB_TOOLS,
        expected_tool="list_prs",
    ),
    EvalCase(
        name="list_issues",
        description="List open issues",
        system_prompt="You are a helpful assistant with GitHub access. Use tools to help the user.",
        user_message="What bugs are open in soypete/pedro-agentware?",
        tools=GITHUB_TOOLS,
        expected_tool="list_issues",
    ),
    EvalCase(
        name="create_issue",
        description="Create an issue",
        system_prompt="You are a helpful assistant with GitHub access. Use tools to help the user.",
        user_message="File a bug about the login issue in soypete/pedro-agentware",
        tools=GITHUB_TOOLS,
        expected_tool="create_issue",
    ),
    EvalCase(
        name="workflow_status",
        description="Check CI status",
        system_prompt="You are a helpful assistant with GitHub access. Use tools to help the user.",
        user_message="What's the CI status for soypete/pedro-agentware?",
        tools=GITHUB_TOOLS,
        expected_tool="get_workflow_status",
    ),
]
