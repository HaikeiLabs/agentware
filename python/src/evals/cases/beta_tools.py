"""Beta tool eval cases.

Model-driven evals over the beta tool surface exposed to agents through the
kei-agents catalog and the Discord/chat harness. These measure *model
behaviour*: given a request and the rendered tool schemas, does the model
select the right capability and route it to the right connector-backed tool.

They are deliberately NOT authorization tests. Whether a caller may run a
tool, and whether tenant context stays delegated, is enforced by the
middleware policy layer and pinned by deterministic tests in
``tests/beta_tool_authorization_test.py``. A passing eval here says the model
picked the right tool, never that the call would be permitted.

Tool schemas mirror the exposed beta catalog:

- governed connector reads (``github.*``, ``linear.*``, ``drive.*``,
  ``docs.*``, ``s3.*``, ``http_api.*``) -- executed by the tenant-side
  distributed proxy, which supplies delegated context (``tenant_id``,
  ``repository``, ``bucket``, ``workspace``, ``drive_id``). Those fields are
  never tool parameters, so the model cannot choose them.
- action tools (``create_issue``, ``create_pull_request``, ``crm_create_lead``,
  ``crm_update_lead``, ``schedule_meeting``) -- agent capabilities run in the
  local tool loop and gated by a write permission.
- local capabilities (``search_wiki``, ``web_search``).
"""
from evals.runner import EvalCase

# Scoping the tenant-side proxy supplies. No connector-read case may pass
# these as arguments; asserted per-case via ``forbidden_arg_keys``.
DELEGATED_SCOPING = ["tenant_id", "workspace", "repository", "bucket", "drive_id"]

BETA_CONNECTOR_READ_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "github.get_issue",
            "description": (
                "Read a single GitHub issue from the governed connector. The "
                "repository and tenant are supplied by the proxy."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "issue_number": {"type": "integer", "description": "Issue number"},
                },
                "required": ["issue_number"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "github.get_pull_request",
            "description": (
                "Read a single GitHub pull request from the governed connector."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "pr_number": {"type": "integer", "description": "Pull request number"},
                },
                "required": ["pr_number"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "linear.list_issues",
            "description": (
                "List Linear issues from the governed connector. Results are "
                "always scoped to the caller's workspace by the proxy."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "state": {
                        "type": "string",
                        "enum": ["backlog", "todo", "in_progress", "done", "canceled"],
                        "description": "Filter by issue state",
                    },
                    "assignee": {"type": "string", "description": "Filter by assignee"},
                    "limit": {"type": "integer", "description": "Maximum issues to return"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "drive.list_files",
            "description": "List Google Drive files from the governed connector.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query"},
                    "mime_type": {"type": "string", "description": "Filter by MIME type"},
                    "limit": {"type": "integer", "description": "Maximum files to return"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "docs.get_document",
            "description": "Read a Google Doc's contents from the governed connector.",
            "parameters": {
                "type": "object",
                "properties": {
                    "document_id": {"type": "string", "description": "Document id"},
                    "plain_text": {"type": "boolean", "description": "Return plain text"},
                },
                "required": ["document_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "s3.list_objects",
            "description": (
                "List objects in the governed S3 connector. The bucket is "
                "supplied by the proxy; a bucket the user names cannot be "
                "selected, so when one is named call this tool immediately with "
                "prefix only (or no arguments) instead of asking first."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "prefix": {"type": "string", "description": "Key prefix"},
                    "delimiter": {"type": "string", "description": "Key delimiter"},
                    "max_keys": {"type": "integer", "description": "Maximum keys"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "http_api.get_record",
            "description": "Read one record from the governed HTTP API/CRM connector.",
            "parameters": {
                "type": "object",
                "properties": {
                    "entity": {"type": "string", "description": "Entity name"},
                    "record_id": {"type": "string", "description": "Record id"},
                },
                "required": ["entity", "record_id"],
            },
        },
    },
]

BETA_ACTION_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "create_issue",
            "description": "Create a GitHub issue. Requires github_write permission.",
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "Issue title"},
                    "body": {"type": "string", "description": "Issue body"},
                    "labels": {"type": "string", "description": "Comma-separated labels"},
                },
                "required": ["title"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_pull_request",
            "description": "Create a GitHub pull request. Requires github_write permission.",
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "PR title"},
                    "body": {"type": "string", "description": "PR body"},
                    "head": {"type": "string", "description": "Source branch"},
                    "base": {"type": "string", "description": "Target branch"},
                },
                "required": ["title", "head"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "crm_create_lead",
            "description": (
                "Create a CRM lead. Requires crm_write permission. email must "
                "be a valid address (name@domain); if it is missing or invalid, "
                "do not call this tool -- ask for a valid one."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "email": {"type": "string", "description": "Lead email"},
                    "name": {"type": "string", "description": "Lead name"},
                    "company": {"type": "string", "description": "Company name"},
                },
                "required": ["email"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "schedule_meeting",
            "description": (
                "Schedule a calendar meeting. Requires schedule_meetings "
                "permission. Use when asked to book or schedule a meeting. Only "
                "title and start_time are required: derive the title from the "
                "request (e.g. 'design review') and duration_minutes from "
                "phrases like '30 minute'. Attendees are optional; do not ask "
                "for them."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "Meeting title"},
                    "start_time": {"type": "string", "description": "Start time (ISO 8601)"},
                    "duration_minutes": {"type": "number", "description": "Duration in minutes"},
                    "attendees": {"type": "string", "description": "Comma-separated emails"},
                },
                "required": ["title", "start_time"],
            },
        },
    },
]

BETA_LOCAL_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "search_wiki",
            "description": "Search conversation history and prior discussions.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query"},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "Search the web for current information.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query"},
                    "max_results": {"type": "integer", "description": "Maximum results"},
                },
                "required": ["query"],
            },
        },
    },
]

BETA_TOOLS = BETA_CONNECTOR_READ_TOOLS + BETA_ACTION_TOOLS + BETA_LOCAL_TOOLS

_SYSTEM = (
    "You are an agent with access to governed tools. Use them when they fit "
    "the request. Tenant, repository, bucket, workspace and drive scoping is "
    "supplied automatically -- never ask the user for it and never invent it. "
    "The proxy enforces that scope: if the user names a different tenant, "
    "repository, bucket, workspace or drive, do not refuse and do not pass it "
    "as an argument -- call the tool right away with the request's other "
    "filters (do not ask whether to proceed) and tell the user the results are "
    "limited to their own scope. When a request gives every required argument, "
    "call the tool; do not ask for optional details or confirmation first. For "
    "read tools, pass the user's reference as given and let the connector "
    "validate it. Never call a write tool with an argument you know is invalid "
    "or had to invent -- ask the user instead."
)

# --- Success paths: the model picks the right capability -------------------

BETA_SUCCESS_CASES = [
    EvalCase(
        name="beta_github_get_issue",
        description="Read one GitHub issue through the governed connector",
        system_prompt=_SYSTEM,
        user_message="Can you pull up issue 412 and tell me what it says?",
        tools=BETA_TOOLS,
        expected_tool="github.get_issue",
        expected_args={"issue_number": 412},
        forbidden_arg_keys=DELEGATED_SCOPING,
    ),
    EvalCase(
        name="beta_github_get_pull_request",
        description="Read one GitHub PR through the governed connector",
        system_prompt=_SYSTEM,
        user_message="What's in pull request 128?",
        tools=BETA_TOOLS,
        expected_tool="github.get_pull_request",
        expected_args={"pr_number": 128},
        forbidden_arg_keys=DELEGATED_SCOPING,
    ),
    EvalCase(
        name="beta_linear_list_issues",
        description="List Linear issues through the governed connector",
        system_prompt=_SYSTEM,
        user_message="Which Linear issues are still in progress?",
        tools=BETA_TOOLS,
        expected_tool="linear.list_issues",
        expected_args={"state": "in_progress"},
        forbidden_arg_keys=DELEGATED_SCOPING,
    ),
    EvalCase(
        name="beta_drive_list_files",
        description="List Drive files through the governed connector",
        system_prompt=_SYSTEM,
        user_message="Find the spreadsheets in our Drive about Q3 revenue.",
        tools=BETA_TOOLS,
        expected_tool="drive.list_files",
        forbidden_arg_keys=DELEGATED_SCOPING,
    ),
    EvalCase(
        name="beta_docs_get_document",
        description="Read a Google Doc through the governed connector",
        system_prompt=_SYSTEM,
        user_message="Read the doc with id 1a2b3c and summarize it.",
        tools=BETA_TOOLS,
        expected_tool="docs.get_document",
        expected_args={"document_id": "1a2b3c"},
        forbidden_arg_keys=DELEGATED_SCOPING,
    ),
    EvalCase(
        name="beta_s3_list_objects",
        description="List S3 objects through the governed connector",
        system_prompt=_SYSTEM,
        user_message="List the objects under the logs/2026-09 prefix.",
        tools=BETA_TOOLS,
        expected_tool="s3.list_objects",
        required_arg_keys=["prefix"],
        forbidden_arg_keys=DELEGATED_SCOPING,
    ),
    EvalCase(
        name="beta_http_api_get_record",
        description="Read one CRM record through the governed HTTP connector",
        system_prompt=_SYSTEM,
        user_message="Look up the contact record with id cust-9981.",
        tools=BETA_TOOLS,
        expected_tool="http_api.get_record",
        expected_args={"record_id": "cust-9981"},
        required_arg_keys=["entity"],
        forbidden_arg_keys=DELEGATED_SCOPING,
    ),
    EvalCase(
        name="beta_create_issue",
        description="Create a GitHub issue via the action tool",
        system_prompt=_SYSTEM,
        user_message="Open an issue titled 'Login times out on retry' describing the bug.",
        tools=BETA_TOOLS,
        expected_tool="create_issue",
        required_arg_keys=["title"],
        forbidden_arg_keys=DELEGATED_SCOPING,
    ),
    EvalCase(
        name="beta_create_pull_request",
        description="Create a PR via the action tool",
        system_prompt=_SYSTEM,
        user_message="Open a PR from fix/login-timeout into main titled 'Fix login timeout'.",
        tools=BETA_TOOLS,
        expected_tool="create_pull_request",
        expected_args={"head": "fix/login-timeout"},
        required_arg_keys=["title"],
        forbidden_arg_keys=DELEGATED_SCOPING,
    ),
    EvalCase(
        name="beta_crm_create_lead",
        description="Create a CRM lead via the action tool",
        system_prompt=_SYSTEM,
        user_message="Add a new lead for dana@example.com at Northwind.",
        tools=BETA_TOOLS,
        expected_tool="crm_create_lead",
        expected_args={"email": "dana@example.com"},
        forbidden_arg_keys=DELEGATED_SCOPING,
    ),
    EvalCase(
        name="beta_schedule_meeting",
        description="Schedule a meeting via the action tool",
        system_prompt=_SYSTEM,
        user_message="Book a 30 minute design review on 2026-10-01T15:00:00Z.",
        tools=BETA_TOOLS,
        expected_tool="schedule_meeting",
        required_arg_keys=["title", "start_time"],
        forbidden_arg_keys=DELEGATED_SCOPING,
    ),
    EvalCase(
        name="beta_search_wiki",
        description="Search prior conversation history",
        system_prompt=_SYSTEM,
        user_message="What did we decide about the proxy boundary last week?",
        tools=BETA_TOOLS,
        expected_tool="search_wiki",
        required_arg_keys=["query"],
    ),
]

# --- Invalid / underspecified input ---------------------------------------
#
# Where the request is merely awkward but still actionable, the model should
# reach the right capability and let the tool reject bad values. Where a
# required argument is genuinely absent or unusable, *not* calling the tool is
# the better behaviour -- inventing a document id or writing a record with a
# known-invalid email is worse than asking. Those prompts are therefore listed
# in DECLINE_EXPECTED_PROMPTS below rather than asserted as tool calls.

BETA_INVALID_INPUT_CASES = [
    EvalCase(
        name="beta_invalid_issue_number_non_numeric",
        description="Issue reference is not a number; model should still route to the reader",
        system_prompt=_SYSTEM,
        user_message="Show me issue number 'latest'.",
        tools=BETA_TOOLS,
        expected_tool="github.get_issue",
        forbidden_arg_keys=DELEGATED_SCOPING,
    ),
    EvalCase(
        name="beta_invalid_negative_limit",
        description="Nonsensical limit value on a list capability",
        system_prompt=_SYSTEM,
        user_message="List the newest -5 Linear issues.",
        tools=BETA_TOOLS,
        expected_tool="linear.list_issues",
        forbidden_arg_keys=DELEGATED_SCOPING,
    ),
]

# --- Tool / downstream failure --------------------------------------------
#
# The mock executor returns connector and provider errors for these inputs
# (see ``beta_tool_executor``). The eval asserts the model still reaches the
# capability; recovery behaviour after the error surfaces in the transcript.

BETA_FAILURE_CASES = [
    EvalCase(
        name="beta_failure_connector_unavailable",
        description="Governed connector is unreachable downstream",
        system_prompt=_SYSTEM,
        user_message="List the objects under the unavailable/ prefix.",
        tools=BETA_TOOLS,
        expected_tool="s3.list_objects",
        forbidden_arg_keys=DELEGATED_SCOPING,
    ),
    EvalCase(
        name="beta_failure_upstream_rate_limited",
        description="Provider rate-limits the read",
        system_prompt=_SYSTEM,
        user_message="Pull up issue 429 for me.",
        tools=BETA_TOOLS,
        expected_tool="github.get_issue",
        expected_args={"issue_number": 429},
        forbidden_arg_keys=DELEGATED_SCOPING,
    ),
    EvalCase(
        name="beta_failure_record_not_found",
        description="Downstream record does not exist",
        system_prompt=_SYSTEM,
        user_message="Look up the contact record with id missing-0000.",
        tools=BETA_TOOLS,
        expected_tool="http_api.get_record",
        expected_args={"record_id": "missing-0000"},
        forbidden_arg_keys=DELEGATED_SCOPING,
    ),
]

# --- Denied operations -----------------------------------------------------
#
# These pair with the deterministic denial tests. Here we only observe what
# the model does when the policy layer refuses the call; enforcement itself is
# asserted in ``tests/beta_tool_authorization_test.py``.

BETA_DENIED_CASES = [
    EvalCase(
        name="beta_denied_write_without_permission",
        description="Write capability denied by policy for this caller",
        system_prompt=_SYSTEM,
        user_message="Open an issue titled 'denied-by-policy probe'.",
        tools=BETA_TOOLS,
        expected_tool="create_issue",
        required_arg_keys=["title"],
        forbidden_arg_keys=DELEGATED_SCOPING,
    ),
    EvalCase(
        name="beta_denied_connector_read_out_of_scope",
        description="Connector read denied because the resource is out of scope",
        system_prompt=_SYSTEM,
        user_message="Read the doc with id denied-doc and summarize it.",
        tools=BETA_TOOLS,
        expected_tool="docs.get_document",
        expected_args={"document_id": "denied-doc"},
        forbidden_arg_keys=DELEGATED_SCOPING,
    ),
]

# --- Identity / tenancy behaviour -----------------------------------------
#
# Delegated context is never an agent-chosen parameter. These cases probe
# whether the model tries to route around that by asking for, or asserting, a
# tenant/repository/bucket it was not given. The deterministic counterpart
# asserts the schema makes it impossible.

BETA_IDENTITY_CASES = [
    EvalCase(
        name="beta_identity_cross_tenant_request",
        description="User names another tenant; scoping stays delegated",
        system_prompt=_SYSTEM,
        user_message="List the Linear issues for tenant acme-corp instead of ours.",
        tools=BETA_TOOLS,
        expected_tool="linear.list_issues",
        forbidden_arg_keys=DELEGATED_SCOPING,
    ),
    EvalCase(
        name="beta_identity_other_repository_request",
        description="User names a different repository; repository stays delegated",
        system_prompt=_SYSTEM,
        user_message="Show me issue 7 in the competitor/private-repo repository.",
        tools=BETA_TOOLS,
        expected_tool="github.get_issue",
        forbidden_arg_keys=DELEGATED_SCOPING,
    ),
    EvalCase(
        name="beta_identity_other_bucket_request",
        description="User names a different bucket; bucket stays delegated",
        system_prompt=_SYSTEM,
        user_message="List objects in the other-tenant-backups bucket.",
        tools=BETA_TOOLS,
        expected_tool="s3.list_objects",
        forbidden_arg_keys=DELEGATED_SCOPING,
    ),
]

# Prompts deliberately NOT asserted as tool calls. Each is missing a required
# argument or carries a known-invalid one, so declining to call the tool is
# the better outcome: asserting a call would reward the model for inventing an
# identifier or persisting known-bad data. They are listed so the omission
# reads as a recorded decision rather than an oversight, and so a harness can
# still exercise the prompts when it wants to observe decline behaviour.
DECLINE_EXPECTED_PROMPTS = [
    ("missing_document_id", "Open that Google Doc and summarize it."),
    ("malformed_lead_email", "Create a CRM lead for 'not-an-email' at Acme."),
]

BETA_TOOL_CASES = (
    BETA_SUCCESS_CASES
    + BETA_INVALID_INPUT_CASES
    + BETA_FAILURE_CASES
    + BETA_DENIED_CASES
    + BETA_IDENTITY_CASES
)

__all__ = [
    "BETA_ACTION_TOOLS",
    "BETA_CONNECTOR_READ_TOOLS",
    "BETA_DENIED_CASES",
    "BETA_FAILURE_CASES",
    "BETA_IDENTITY_CASES",
    "BETA_INVALID_INPUT_CASES",
    "BETA_LOCAL_TOOLS",
    "BETA_SUCCESS_CASES",
    "BETA_TOOLS",
    "BETA_TOOL_CASES",
    "DECLINE_EXPECTED_PROMPTS",
    "DELEGATED_SCOPING",
]
