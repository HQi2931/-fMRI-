"""Optimistic-locking workflow transition coordinator."""

from __future__ import annotations

from typing import Protocol

from neuroagent.application.contracts import RunView, WorkflowState
from neuroagent.workflow.state import validate_workflow_transition


class WorkflowRepository(Protocol):
    def get_run(self, run_id: str) -> RunView: ...

    def transition_run(
        self,
        run_id: str,
        *,
        expected_version: int,
        target: WorkflowState,
        error: str | None = None,
        increment_attempt: bool = False,
    ) -> RunView: ...

    def append_event(
        self,
        *,
        project_id: str | None,
        run_id: str | None,
        event_type: str,
        severity: str,
        payload: dict[str, object],
    ) -> None: ...


class WorkflowEngine:
    def __init__(self, repository: WorkflowRepository) -> None:
        self._repository = repository

    def transition(
        self,
        run_id: str,
        *,
        expected_version: int,
        target: WorkflowState,
        actor: str,
        reason: str,
        error: str | None = None,
        increment_attempt: bool = False,
    ) -> RunView:
        current = self._repository.get_run(run_id)
        validate_workflow_transition(current.state, target)
        updated = self._repository.transition_run(
            run_id,
            expected_version=expected_version,
            target=target,
            error=error,
            increment_attempt=increment_attempt,
        )
        self._repository.append_event(
            project_id=updated.project_id,
            run_id=run_id,
            event_type="WorkflowTransitioned",
            severity="error" if target.value.startswith("failed") else "info",
            payload={
                "from_state": current.state.value,
                "to_state": target.value,
                "actor": actor,
                "reason": reason,
                "version": updated.version,
            },
        )
        return updated
