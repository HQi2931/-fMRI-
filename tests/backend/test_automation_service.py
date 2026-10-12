from __future__ import annotations

import pytest

from neuroagent.application.automation.models import (
    AuthorityReferences,
    EventConsumptionDecision,
    TaskBudget,
    TaskStatus,
    WorkEventIdentity,
    WorkGoal,
    WorkTask,
)
from neuroagent.application.errors import InputValidationError
from neuroagent.bootstrap import build_service


def make_task() -> WorkTask:
    return WorkTask(
        task_id="application-task-1",
        goal=WorkGoal(
            description="Run the requested resting-state workflow",
            completion_conditions=("approved workflow has been reviewed",),
        ),
        authority_refs=AuthorityReferences(workflow_run_id="run-application"),
        budget=TaskBudget(
            max_steps=10,
            max_model_calls=4,
            max_tokens=5000,
            max_elapsed_seconds=1200,
        ),
    )


def test_application_service_reads_persisted_task_after_service_restart(service) -> None:
    task = make_task()
    service.automation.create_task(task)
    settings = service.settings
    service.close()

    restarted = build_service(settings)
    try:
        restored = restarted.automation.get_task(task.task_id)
        assert restored == task
    finally:
        restarted.close()


def test_replaying_workflow_event_does_not_advance_task_twice(service) -> None:
    task = make_task()
    service.automation.create_task(task)
    event = WorkEventIdentity(
        stream_key="workflow:run-application",
        workflow_run_id="run-application",
        event_id=77,
        dedupe_key="workflow:run-application:notice:77",
    )

    first_decision, first_state = service.automation.consume_workflow_event(task.task_id, event)
    replay_decision, replay_state = service.automation.consume_workflow_event(task.task_id, event)

    assert first_decision is EventConsumptionDecision.CONSUME
    assert replay_decision is EventConsumptionDecision.DUPLICATE
    assert first_state.version == task.version + 1
    assert replay_state.version == first_state.version
    assert replay_state.event_cursors[0].event_id == 77
    # Event receipts do not assert a Run status or advance task lifecycle based
    # on an untrusted/possibly stale event payload.
    assert replay_state.status is TaskStatus.QUEUED


def test_application_service_transition_checks_expected_version(service) -> None:
    task = make_task()
    service.automation.create_task(task)
    updated = service.automation.transition_task(
        task.task_id,
        expected_version=task.version,
        target=TaskStatus.RUNNING,
    )
    assert updated.status is TaskStatus.RUNNING
    assert updated.version == task.version + 1


def test_workflow_event_must_match_task_run_reference(service) -> None:
    task = make_task()
    service.automation.create_task(task)
    event = WorkEventIdentity(
        stream_key="workflow:unrelated-run",
        workflow_run_id="unrelated-run",
        event_id=1,
        dedupe_key="unrelated-run:event:1",
    )
    with pytest.raises(InputValidationError) as error:
        service.automation.consume_workflow_event(task.task_id, event)
    assert error.value.code == "workflow_event_run_mismatch"
    assert service.automation.get_task(task.task_id).version == task.version
