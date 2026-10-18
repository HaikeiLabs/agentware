"""KEI harness contract definition.

This module defines the contract between a third-party harness and the
pedro-agentware library. The contract specifies what a harness MUST implement
versus what the library provides.

Security model: fail-closed. Unknown policy decisions, unreachable proxies,
missing credentials, and expired tokens all result in DENY.
"""

from typing import Any, Protocol, runtime_checkable

from haikeilabs.agentware.middleware import AuditedToolClient
from haikeilabs.agentware.middleware.types import Action, Decision

from .auth import SecretProvider
from .proxy import ProxyProcess


class _DenyAllEvaluator:
    """Fail-closed default: denies every tool call."""

    def evaluate(self, tool_name: str, args: dict[str, Any], caller: Any) -> Decision:
        return Decision(
            action=Action.DENY,
            rule="default-deny",
            reason="no policy evaluator configured (fail-closed)",
        )


class ToolNotFoundError(Exception):
    """Raised when a requested tool doesn't exist."""

    pass


class ToolExecutionError(Exception):
    """Raised when tool execution fails."""

    pass


@runtime_checkable
class ToolExecutor(Protocol):
    """Protocol for tool execution.

    A harness must provide a tool executor that can run named tools with
    given arguments. This is the execution backend that the middleware
    wraps with policy enforcement and audit logging.
    """

    def execute(self, tool_name: str, args: dict[str, Any]) -> Any:
        """Execute a tool by name with the given arguments.

        Args:
            tool_name: Name of the tool to execute
            args: Arguments to pass to the tool

        Returns:
            Tool execution result (any JSON-serializable type)

        Raises:
            ToolNotFoundError: If tool doesn't exist
            ToolExecutionError: If tool fails to execute
        """
        ...


class HarnessContract:
    """Complete harness contract.

    A third-party harness must implement the following to integrate with
    pedro-agentware:

    REQUIRED (must be provided by harness):
    - auth_provider: AuthProvider - provides identity tokens for KEI API
    - tool_executor: ToolExecutor - executes tools on behalf of agents
    - secret_provider: SecretProvider - sources the bootstrap secret

    OPTIONAL (library provides sensible defaults):
    - policy_evaluator: PolicyEvaluator | None - enforces policy on tool calls;
      omitted means deny all (fail closed)
    - auditor: Auditor | None - records all tool call decisions
    - proxy_process: ProxyProcess | None - manages local KEI proxy

    The library provides:
    - AuditedToolClient - wraps tool execution with policy + audit
    - CallerContext delegation - tracks human identity across subagents

    FAIL-CLOSED RULES:
    - Unknown policy decision -> DENY
    - Unreachable proxy -> DENY
    - Missing credential -> DENY
    - Expired token -> DENY

    CALLERCONTEXT DELEGATION RULE:
    - invoking_subject is the HUMAN and is carried UNCHANGED across every
      delegation hop
    - parent_span and delegation_depth record position in the chain
    - A subagent running as a service account still resolves to the person
    """

    def __init__(
        self,
        auth_provider: Any,
        tool_executor: ToolExecutor,
        secret_provider: SecretProvider,
        policy_evaluator: Any = None,
        auditor: Any = None,
        proxy_process: ProxyProcess | None = None,
    ) -> None:
        """Initialize harness contract.

        Args:
            auth_provider: Authentication provider for KEI API (AuthProvider protocol)
            tool_executor: Tool execution backend (ToolExecutor protocol)
            secret_provider: Secret source for bootstrap token (SecretProvider protocol)
            policy_evaluator: Optional policy evaluator (default: deny all)
            auditor: Optional audit logger (default: in-memory)
            proxy_process: Optional proxy process manager
        """
        self.auth_provider = auth_provider
        self.tool_executor = tool_executor
        self.secret_provider = secret_provider
        self.policy_evaluator = policy_evaluator or _DenyAllEvaluator()
        self.auditor = auditor
        self.proxy_process = proxy_process

    def create_tool_client(self, source: str = "pedro-agentware") -> AuditedToolClient:
        """Create an audited client wired to this contract's components."""
        return AuditedToolClient(
            source=source,
            evaluator=self.policy_evaluator,
            auditor=self.auditor,
            executor=self.tool_executor.execute,
        )


def validate_contract(contract: HarnessContract) -> list[str]:
    """Validate a harness contract configuration.

    Fails closed on missing required components. Uses duck typing to verify
    that required methods exist rather than explicit type checking.

    Args:
        contract: The contract to validate

    Returns:
        List of validation errors (empty if valid)
    """
    errors: list[str] = []

    required_methods = {
        "auth_provider": ["get_token", "invalidate", "get_token_type"],
        "tool_executor": ["execute"],
        "secret_provider": ["get_secret"],
    }

    for attr, methods in required_methods.items():
        obj = getattr(contract, attr, None)
        if obj is None:
            errors.append(f"{attr} is required but not set")
            continue
        for method in methods:
            if not hasattr(obj, method):
                errors.append(f"{attr} must implement method {method}()")

    if contract.policy_evaluator is not None:
        required = ["evaluate"]
        for method in required:
            if not hasattr(contract.policy_evaluator, method):
                errors.append(f"policy_evaluator must implement method {method}()")

    if contract.auditor is not None:
        required = ["record", "query"]
        for method in required:
            if not hasattr(contract.auditor, method):
                errors.append(f"auditor must implement method {method}()")

    if contract.proxy_process is not None:
        required = ["start", "stop", "is_running", "get_url"]
        for method in required:
            if not hasattr(contract.proxy_process, method):
                errors.append(f"proxy_process must implement method {method}()")

    return errors
