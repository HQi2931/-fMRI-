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
from neuroagent.application.automation.step_lifecycle import (
    bind_step_to_run,
    complete_step,
    resolve_single_bound_run_step,
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

    completed = transition_task(waiting, TaskStatus.COMPLETED)
    assert completed.status is TaskStatus.COMPLETED
    with pytest.raises(ValueError, match="invalid task transition"):
        transition_task(completed, TaskStatus.WAITING_RUN)
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


def test_shared_step_binding_transition_is_versioned_and_idempotent() -> None:
    step = TaskStep(
        step_id="preprocess",
        sequence=0,
        kind="preprocessing_run",
        status=TaskStepStatus.READY,
    )
    task = make_task(
        status=TaskStatus.WAITING_USER,
        waiting_condition=WaitingCondition(
            kind=WaitingKind.USER_DECISION,
            reference="plan:plan-1",
            reason="review the frozen plan",
        ),
        steps=(step,),
    )

    waiting = bind_step_to_run(
        task, step, run_id="run-1", waiting_reason="waiting for the same Run"
    )

    assert waiting.status is TaskStatus.WAITING_RUN
    assert waiting.version == task.version + 1
    assert waiting.steps[0].status is TaskStepStatus.WAITING
    assert waiting.steps[0].version == step.version + 2
    assert waiting.steps[0].authority_refs.workflow_run_id == "run-1"
    assert waiting.authority_refs.workflow_run_id == "run-1"
    assert bind_step_to_run(
        waiting,
        waiting.steps[0],
        run_id="run-1",
        waiting_reason="ignored on an idempotent replay",
    ) is waiting


def test_single_bound_run_step_resolver_keeps_legacy_tasks_and_rejects_mismatch() -> None:
    legacy = make_task(authority_refs=AuthorityReferences(workflow_run_id="run-1"))
    assert resolve_single_bound_run_step(legacy, "run-1") is None

    step = TaskStep(
        step_id="preprocess",
        sequence=0,
        kind="preprocessing_run",
        status=TaskStepStatus.WAITING,
        authority_refs=AuthorityReferences(workflow_run_id="run-1"),
    )
    task = make_task(
        status=TaskStatus.WAITING_RUN,
        waiting_condition=WaitingCondition(
            kind=WaitingKind.WORKFLOW_RUN,
            reference="run-1",
            reason="waiting for the bound run",
        ),
        authority_refs=AuthorityReferences(workflow_run_id="run-1"),
        steps=(step,),
    )
    assert resolve_single_bound_run_step(task, "run-1") == step

    mismatched_step = step.model_copy(
        update={
            "authority_refs": step.authority_refs.model_copy(
                update={"workflow_run_id": "run-2"}
            )
        }
    )
    mismatched = WorkTask.model_validate(
        {**task.model_dump(), "steps": (mismatched_step.model_dump(),)}
    )
    with pytest.raises(ValueError, match="same bound Run"):
        resolve_single_bound_run_step(mismatched, "run-1")


@pytest.mark.parametrize("sibling_kind", ["preprocessing_run", "unregistered_run"])
def test_shared_run_step_lifecycle_rejects_sibling_steps(sibling_kind: str) -> None:
    ready = TaskStep(
        step_id="analysis",
        sequence=0,
        kind="statistical_run",
        status=TaskStepStatus.READY,
    )
    sibling = TaskStep(step_id="sibling", sequence=1, kind=sibling_kind)
    task = make_task(
        status=TaskStatus.WAITING_USER,
        waiting_condition=WaitingCondition(
            kind=WaitingKind.USER_DECISION,
            reference="proposal:proposal-1",
            reason="review the proposal",
        ),
        steps=(ready, sibling),
    )

    with pytest.raises(ValueError, match="exactly one TaskStep"):
        bind_step_to_run(task, ready, run_id="run-1", waiting_reason="wait")

    bound = bind_step_to_run(
        make_task(
            status=TaskStatus.WAITING_USER,
            waiting_condition=WaitingCondition(
                kind=WaitingKind.USER_DECISION,
                reference="proposal:proposal-1",
                reason="review the proposal",
            ),
            steps=(ready,),
        ),
        ready,
        run_id="run-1",
        waiting_reason="wait",
    )
    task_with_sibling = WorkTask.model_validate(
        {
            **bound.model_dump(),
            "steps": (*bound.model_dump()["steps"], sibling.model_dump()),
        }
    )
    with pytest.raises(ValueError, match="exactly one TaskStep"):
        complete_step(
            task_with_sibling,
            task_with_sibling.steps[0],
            artifact_ids=("artifact-1",),
        )


@pytest.mark.parametrize("wrong_run_id", ["run-2", ""])
def test_shared_step_binding_rejects_wrong_or_missing_run(wrong_run_id: str) -> None:
    step = TaskStep(
        step_id="analysis",
        sequence=0,
        kind="statistical_run",
        status=TaskStepStatus.READY,
    )
    task = make_task(
        status=TaskStatus.WAITING_USER,
        waiting_condition=WaitingCondition(
            kind=WaitingKind.USER_DECISION,
            reference="proposal:proposal-1",
            reason="review the proposal",
        ),
        steps=(step,),
    )
    bound = bind_step_to_run(task, step, run_id="run-1", waiting_reason="wait")

    with pytest.raises(ValueError):
        bind_step_to_run(bound, bound.steps[0], run_id=wrong_run_id, waiting_reason="wait")

    partially_bound = bound.model_copy(
        update={
            "authority_refs": bound.authority_refs.model_copy(
                update={"workflow_run_id": None}
            )
        }
    )
    with pytest.raises(ValueError, match="already bound to a different or invalid Run state"):
        bind_step_to_run(
            partially_bound,
            partially_bound.steps[0],
            run_id="run-1",
            waiting_reason="wait",
        )


def test_shared_step_binding_rejects_wrong_task_state_and_stale_step() -> None:
    ready = TaskStep(
        step_id="analysis",
        sequence=0,
        kind="statistical_run",
        status=TaskStepStatus.READY,
    )
    queued_task = make_task(steps=(ready,))
    with pytest.raises(ValueError, match="waiting-user task"):
        bind_step_to_run(queued_task, ready, run_id="run-1", waiting_reason="wait")

    waiting_user_task = make_task(
        status=TaskStatus.WAITING_USER,
        waiting_condition=WaitingCondition(
            kind=WaitingKind.USER_DECISION,
            reference="proposal:proposal-1",
            reason="review the proposal",
        ),
        steps=(ready,),
    )
    stale_step = ready.model_copy(update={"version": ready.version + 1})
    with pytest.raises(ValueError, match="current persisted task step"):
        bind_step_to_run(waiting_user_task, stale_step, run_id="run-1", waiting_reason="wait")


def test_shared_step_completion_transition_is_idempotent_and_keeps_task_graph() -> None:
    step = TaskStep(
        step_id="analysis",
        sequence=0,
        kind="statistical_run",
        status=TaskStepStatus.READY,
    )
    task = make_task(
        status=TaskStatus.WAITING_USER,
        waiting_condition=WaitingCondition(
            kind=WaitingKind.USER_DECISION,
            reference="proposal:proposal-1",
            reason="review the proposal",
        ),
        steps=(step,),
    )
    waiting = bind_step_to_run(task, step, run_id="run-1", waiting_reason="wait")

    completed = complete_step(
        waiting, waiting.steps[0], artifact_ids=("artifact-b", "artifact-a")
    )

    assert completed.status is TaskStatus.COMPLETED
    assert completed.version == waiting.version + 1
    assert completed.steps[0].status is TaskStepStatus.SUCCEEDED
    assert completed.steps[0].authority_refs.artifact_ids == ("artifact-a", "artifact-b")
    assert completed.authority_refs.artifact_ids == ("artifact-a", "artifact-b")
    assert (
        complete_step(
            completed, completed.steps[0], artifact_ids=("artifact-a", "artifact-b")
        )
        is completed
    )
    with pytest.raises(ValueError, match="Artifact references differ"):
        complete_step(completed, completed.steps[0], artifact_ids=("different",))


def test_shared_step_completion_does_not_allow_waiting_user_to_skip_decision() -> None:
    step = TaskStep(
        step_id="preprocess",
        sequence=0,
        kind="preprocessing_run",
        status=TaskStepStatus.WAITING,
        authority_refs=AuthorityReferences(workflow_run_id="run-1"),
    )
    task = make_task(
        status=TaskStatus.WAITING_USER,
        waiting_condition=WaitingCondition(
            kind=WaitingKind.USER_DECISION,
            reference="plan:plan-1",
            reason="The statistical plan still requires review",
        ),
        authority_refs=AuthorityReferences(workflow_run_id="run-1"),
        steps=(step,),
    )

    with pytest.raises(ValueError, match="not waiting for this verified Run"):
        complete_step(task, step, artifact_ids=("artifact-1",))


def test_shared_step_completion_can_close_only_the_matching_resolved_qc_wait() -> None:
    ready_step = TaskStep(
        step_id="preprocess",
        sequence=0,
        kind="preprocessing_run",
        status=TaskStepStatus.READY,
    )
    task = make_task(
        status=TaskStatus.WAITING_USER,
        waiting_condition=WaitingCondition(
            kind=WaitingKind.USER_DECISION,
            reference="plan:plan-1",
            reason="review the frozen plan",
        ),
        steps=(ready_step,),
    )
    waiting = bind_step_to_run(task, ready_step, run_id="run-1", waiting_reason="wait")
    qc_waiting = WorkTask.model_validate(
        {
            **waiting.model_dump(),
            "status": TaskStatus.WAITING_USER,
            "waiting_condition": WaitingCondition(
                kind=WaitingKind.USER_DECISION,
                reference="qc:run-1",
                reason="the user must review QC",
            ).model_dump(),
            "version": waiting.version + 1,
        }
    )

    completed = complete_step(
        qc_waiting,
        qc_waiting.steps[0],
        artifact_ids=("artifact-1",),
    )

    assert completed.status is TaskStatus.COMPLETED
    assert completed.waiting_condition is None
    assert completed.version == qc_waiting.version + 1
    assert completed.steps[0].status is TaskStepStatus.SUCCEEDED

    statistical_step = qc_waiting.steps[0].model_copy(update={"kind": "statistical_run"})
    wrong_kind_wait = WorkTask.model_validate(
        {**qc_waiting.model_dump(), "steps": (statistical_step.model_dump(),)}
    )
    with pytest.raises(ValueError, match="not waiting for this verified Run"):
        complete_step(wrong_kind_wait, wrong_kind_wait.steps[0], artifact_ids=("artifact-1",))


def test_shared_run_step_lifecycle_rejects_unregistered_step_kind() -> None:
    step = TaskStep(
        step_id="unknown",
        sequence=0,
        kind="unregistered_run",
        status=TaskStepStatus.READY,
    )
    task = make_task(
        status=TaskStatus.WAITING_USER,
        waiting_condition=WaitingCondition(
            kind=WaitingKind.USER_DECISION,
            reference="approval:plan-1",
            reason="waiting for approval",
        ),
        steps=(step,),
    )

    with pytest.raises(ValueError, match="unsupported Run step kind"):
        bind_step_to_run(task, step, run_id="run-1", waiting_reason="wait")


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
