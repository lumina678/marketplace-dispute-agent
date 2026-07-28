from __future__ import annotations

from typing import Any


class WorkflowHooks:
    """Lifecycle hooks used by durable workers without coupling business logic to RQ."""

    def run_started(self, case_run_id: str, phase: str) -> None:
        pass

    def check_control(self) -> None:
        pass

    def stage_started(self, phase: str, agent_role: str, case_run_id: str) -> None:
        pass

    def stage_completed(
        self,
        phase: str,
        agent_role: str,
        case_run_id: str,
        summary: dict[str, Any] | None = None,
    ) -> None:
        pass

    def stage_failed(self, phase: str, agent_role: str, case_run_id: str, error: Exception) -> None:
        pass

    def phase_committed(self, phase: str, view: dict[str, Any]) -> None:
        pass

    def boundary_reached(self, boundary: str, view: dict[str, Any]) -> None:
        pass
