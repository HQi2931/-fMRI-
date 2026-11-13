from __future__ import annotations

from types import SimpleNamespace

import pytest

from neuroagent.application.errors import ConflictError
from neuroagent.domain.lifecycle import (
    WorkflowState,
)
from neuroagent.workflow.engine import WorkflowEngine


class RecordingWorkflowRepository:
    def __init__(self, state: WorkflowState = WorkflowState.QUEUED) -> None:
        self.run = SimpleNamespace(state=state, project_id="project-1", version=3)
        self.transitions: list[dict[str, object]] = []
        self.events: list[dict[str, object]] = []

    def get_run(self, run_id: str):
        assert run_id == "run-1"
        return self.run

    def transition_run(self, run_id: str, **kwargs: object):
        assert run_id == "run-1"
        self.transitions.append(kwargs)
        self.run = SimpleNamespace(
            state=kwargs["target"], project_id="project-1", version=self.run.version + 1
        )
        return self.run

    def append_event(self, **kwargs: object) -> None:
        self.events.append(kwargs)


@pytest.mark.parametrize(
    ("source", "target", "severity"),
    [
        (WorkflowState.QUEUED, WorkflowState.RUNNING, "info"),
        (WorkflowState.RUNNING, WorkflowState.FAILED_RETRYABLE, "error"),
    ],
)
def test_workflow_transition_records_actor_reason_and_real_state(
    source: WorkflowState, target: WorkflowState, severity: str
) -> None:
    repository = RecordingWorkflowRepository(state=source)

    updated = WorkflowEngine(repository).transition(
        "run-1",
        expected_version=3,
        target=target,
        actor="worker",
        reason="stage finished",
        error="MATLAB returned nonzero" if severity == "error" else None,
        increment_attempt=severity == "error",
    )

    assert updated.state is target
    assert repository.transitions == [
        {
            "expected_version": 3,
            "target": target,
            "error": "MATLAB returned nonzero" if severity == "error" else None,
            "increment_attempt": severity == "error",
        }
    ]
    assert repository.events == [
        {
            "project_id": "project-1",
            "run_id": "run-1",
            "event_type": "WorkflowTransitioned",
            "severity": severity,
            "payload": {
                "from_state": source.value,
                "to_state": target.value,
                "actor": "worker",
                "reason": "stage finished",
                "version": 4,
            },
        }
    ]


def test_workflow_transition_rejects_invalid_state_before_persisting() -> None:
    repository = RecordingWorkflowRepository(state=WorkflowState.SUCCEEDED)

    with pytest.raises(ConflictError) as invalid:
        WorkflowEngine(repository).transition(
            "run-1",
            expected_version=3,
            target=WorkflowState.RUNNING,
            actor="worker",
            reason="restarted a terminal run",
        )

    assert invalid.value.code == "invalid_workflow_transition"
    assert repository.transitions == []
    assert repository.events == []
