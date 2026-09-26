# Changelog

Notable changes to pedro-agentware (Go, Python, and TypeScript). Releases are
coordinated separately; entries collect under **Unreleased** until then.

## [0.4.0] - 2026-09-26

## Unreleased

### Added

- RuntimeLink: `RuntimeIdentity` exposes the installation's assigned agents
  from the runtime `identity` event (HAI-172). New fields: TypeScript
  `defaultAgentId?: string` and `agents: AssignedAgent[]`; Python
  `default_agent_id: str | None` and `agents: tuple[AssignedAgent, ...]`; Go
  `DefaultAgentID string` and `Agents []AssignedAgent`. A harness reads its
  agent from `link.identity()` and needs only `KEI_RUNTIME_TOKEN` — no
  agent-ID env var.
  - Tolerant of older runtimes: missing `agent_id` / `agents` parse as no
    default and an empty list.
  - Malformed `agent_id`, a non-list `agents`, or malformed entries are dropped
    and counted in `consecutive_fails` like other dropped child lines; the
    identity itself is still accepted.
  - When `agent_id` is absent, the first `is_default` entry supplies the
    default.
  - Parsers report the drop count: TypeScript `ChildEvent.droppedAgents`, Go
    `ChildEvent.DroppedAgents`, Python `parse_child_line_counted()`.
  - Shared contract fixture `testing/contracts/runtime-link/wire/child-events.json`
    gains agent cases; existing identity expectations now include
    `agent_id: ""` and `agents: []`.
