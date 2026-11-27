"""
Calendar tool test cases.
"""

from haikeilabs.agentware.evals.runner import EvalCase

CALENDAR_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "schedule_meeting",
            "description": "Schedule a meeting on the calendar",
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "Meeting title"},
                    "start_time": {"type": "string", "description": "Start time in ISO format"},
                    "end_time": {"type": "string", "description": "End time in ISO format"},
                    "attendees": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Email addresses",
                    },
                    "description": {"type": "string", "description": "Meeting description"},
                },
                "required": ["title", "start_time", "end_time"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_events",
            "description": "List calendar events",
            "parameters": {
                "type": "object",
                "properties": {
                    "start_date": {"type": "string", "description": "Start date in ISO format"},
                    "end_date": {"type": "string", "description": "End date in ISO format"},
                },
                "required": ["start_date", "end_date"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "find_free_time",
            "description": "Find free time slots",
            "parameters": {
                "type": "object",
                "properties": {
                    "date": {"type": "string", "description": "Date in ISO format"},
                    "duration_minutes": {"type": "integer", "description": "Meeting duration"},
                },
                "required": ["date", "duration_minutes"],
            },
        },
    },
]

CALENDAR_CASES = [
    EvalCase(
        name="schedule_meeting",
        description="Schedule a meeting",
        system_prompt="You are a helpful assistant with calendar access. Use tools to help the user.",
        user_message="Schedule a meeting for tomorrow at 3pm with john@example.com",
        tools=CALENDAR_TOOLS,
        expected_tool="schedule_meeting",
    ),
    EvalCase(
        name="list_events",
        description="List today's events",
        system_prompt="You are a helpful assistant with calendar access. Use tools to help the user.",
        user_message="What's on my calendar today?",
        tools=CALENDAR_TOOLS,
        expected_tool="list_events",
    ),
    EvalCase(
        name="find_free_time",
        description="Find free time",
        system_prompt="You are a helpful assistant with calendar access. Use tools to help the user.",
        user_message="Find a 30 minute slot tomorrow",
        tools=CALENDAR_TOOLS,
        expected_tool="find_free_time",
    ),
]
