from __future__ import annotations

import pytest
from pydantic import ValidationError

from neuroagent.application.automation.models import (
    AuthorityReferences,
    EventConsumptionDecision,
    TaskBudget,
    TaskStatus,
    TaskStep,
    TaskStepStatus,
    WaitingCondition,
    WaitingKind,
    WorkEventIdentity,
    WorkGoal,
    WorkTask,
    advance_event_cursor,
    decide_event_consumption,
    transition_task,
    transition_task_step,
)


def make_task(**overrides: object) -> WorkTask:
    values: dict[str, object] = {
        "task_id": "task-1",
        "goal": WorkGoal(
            description="Prepare and analyze the declared dataset",
            completion_conditions=("requested report is available",),
        ),
        "budget": TaskBudget(
            max_steps=20,
            max_model_calls=8,
            max_tokens=12000,
            max_elapsed_seconds=3600,
        ),
    }
    values.update(overrides)
    return WorkTask.model_validate(values)


def test_task_waiting_states_require_matching_condition_and_terminal_is_final() -> None:
    task = make_task()
    wait = WaitingCondition(
        kind=WaitingKind.WORKFLOW_RUN,
        reference="run-42",
        reason="DPABI worker is processing the approved run",
    )
    waiting = transition_task(task, TaskStatus.WAITING_RUN, waiting_condition=wait)
    assert waiting.status is TaskStatus.WAITING_RUN
    assert waiting.version == task.version + 1

    with pytest.raises(ValueError, match="invalid task transition"):
        transition_task(waiting, TaskStatus.COMPLETED)
    with pytest.raises(ValidationError, match="waiting_condition"):
        make_task(status=TaskStatus.WAITING_USER)


def test_transition_rejects_scientific_status_mirroring_fields() -> None:
    task = make_task(authority_refs=AuthorityReferences(workflow_run_id="run-42"))
    assert task.authority_refs.workflow_run_id == "run-42"
    assert "run_status" not in WorkTask.model_fields


def test_budget_and_step_identity_boundaries_are_enforced() -> None:
    with pytest.raises(ValidationError, match="used_tokens exceeds max_tokens"):
        TaskBudget(
            max_steps=1,
            max_model_calls=1,
            max_tokens=100,
            max_elapsed_seconds=5,
            used_tokens=101,
        )
    with pytest.raises(ValidationError, match="step_id values must be unique"):
        make_task(
            steps=(
                TaskStep(step_id="same", sequence=0, kind="inspect"),
                TaskStep(step_id="same", sequence=1, kind="prepare"),
            )
        )


def test_task_step_transitions_are_versioned_and_terminal_step_cannot_reopen() -> None:
    step = TaskStep(step_id="inspect", sequence=0, kind="inspect")
    ready = transition_task_step(step, TaskStepStatus.READY)
    running = transition_task_step(ready, TaskStepStatus.RUNNING)
    done = transition_task_step(running, TaskStepStatus.SUCCEEDED)
    assert done.version == 4
    with pytest.raises(ValueError, match="invalid task step transition"):
        transition_task_step(done, TaskStepStatus.READY)


def test_event_consumer_distinguishes_dedupe_and_old_cursor_events() -> None:
    event = WorkEventIdentity(
        stream_key="run:run-42",
        workflow_run_id="run-42",
        event_id=8,
        dedupe_key="worker:run-42:attempt-1:done",
    )
    assert decide_event_consumption(event, cursor=7, dedupe_key_seen=False) is (
        EventConsumptionDecision.CONSUME
    )
    assert decide_event_consumption(event, cursor=7, dedupe_key_seen=True) is (
        EventConsumptionDecision.DUPLICATE
    )
    assert decide_event_consumption(event, cursor=8, dedupe_key_seen=False) is (
        EventConsumptionDecision.STALE
    )

    task = make_task()
    advanced = advance_event_cursor(task, event.stream_key, event.event_id)
    assert advanced.version == task.version + 1
    assert advanced.event_cursors[0].event_id == 8
    assert advance_event_cursor(advanced, event.stream_key, 7) is advanced
