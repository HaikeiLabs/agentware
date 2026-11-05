"""Minimal offline Pydantic AI template using Forge quality middleware.

Install ``haikeilabs-agentware[forge-pydantic]``. TestModel is synthetic; replace
it with your provider after installing application authorization for each tool.
"""

from dataclasses import dataclass

from pydantic_ai import Agent, FunctionToolset
from pydantic_ai.models.test import TestModel

from haikeilabs.agentware.integrations.forge import ForgeSession
from haikeilabs.agentware.integrations.pydantic_forge import ForgeToolset
from haikeilabs.agentware.middleware.guardrails import ProgressTracker


def read_example() -> str:
    """Return a synthetic observation."""
    return "verified"


def finish() -> str:
    """Finish the synthetic workflow."""
    return "finished"


@dataclass
class RunDeps:
    """Fresh quality state for this run only."""

    quality: ForgeSession


def main() -> None:
    """Run one synthetic workflow without contacting a provider."""
    progress = ProgressTracker(["observation_verified"])
    quality = ForgeSession(
        ["read_example", "finish"],
        terminal_tool="finish",
        progress=progress,
        observe_result=lambda name, args, result: [("observation_verified", result == "verified")],
    )
    tools = ForgeToolset(FunctionToolset([read_example, finish]), lambda ctx: ctx.deps.quality)
    agent = Agent(TestModel(call_tools=["read_example"]), deps_type=RunDeps, toolsets=[tools])
    agent.run_sync("Call the tool.", deps=RunDeps(quality))
    print("verified:", progress.snapshot().ready)
    suggestion = quality.finish_nudge()
    print("finish suggestion:", suggestion.nudge if suggestion else "none")


if __name__ == "__main__":
    main()
