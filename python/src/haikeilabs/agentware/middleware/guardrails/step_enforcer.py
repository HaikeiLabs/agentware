


class StepNotAllowedError(Exception):
    def __init__(self, tool: str, missing_steps: list[str]):
        self.tool = tool
        self.missing_steps = missing_steps
        super().__init__(f"step not allowed: missing {missing_steps}")


class StepEnforcer:
    def __init__(self) -> None:
        self._step_definitions: dict[str, list[str]] = {}
        self._completed_steps: dict[str, dict[str, bool]] = {}
        self._allowed_terminals: dict[str, dict[str, bool]] = {}

    def add_step(self, tool: str, prerequisites: list[str] | None = None) -> None:
        self._step_definitions[tool] = prerequisites or []

    def add_terminal(self, tool: str, allowed: dict[str, bool] | None = None) -> None:
        self._allowed_terminals[tool] = allowed or {}

    def mark_step_complete(self, session_id: str, step: str) -> None:
        if session_id not in self._completed_steps:
            self._completed_steps[session_id] = {}
        self._completed_steps[session_id][step] = True

    def reset_session(self, session_id: str) -> None:
        self._completed_steps.pop(session_id, None)

    def can_execute(self, session_id: str, tool: str) -> tuple[bool, list[str]]:
        prereqs = self._step_definitions.get(tool)
        if prereqs is None:
            return True, []

        completed = self._completed_steps.get(session_id, {})
        missing = [p for p in prereqs if not completed.get(p, False)]

        return len(missing) == 0, missing

    def validate_execution(self, session_id: str, tool: str) -> None:
        allowed, missing = self.can_execute(session_id, tool)
        if allowed:
            return
        raise StepNotAllowedError(tool, missing)

    def is_terminal_allowed(self, session_id: str, terminal_tool: str) -> bool:
        allowed, _ = self.can_execute(session_id, terminal_tool)
        return allowed

    def get_allowed_terminals(self, session_id: str) -> list[str]:
        result = []
        for tool in self._step_definitions:
            allowed, _ = self.can_execute(session_id, tool)
            if allowed:
                result.append(tool)
        return result
