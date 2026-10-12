from __future__ import annotations

import pytest

from neuroagent.application.automation.models import (
    AuthorityReferences,
    EventConsumptionDecision,
    TaskBudget,
    TaskStatus,
    TaskStep,
    TaskStepStatus,
    WorkEventIdentity,
    WorkGoal,
    WorkTask,
    transition_task,
    transition_task_step,
)
from neuroagent.application.errors import ConflictError
from neuroagent.infrastructure.persistence.automation_repository import (
    SqliteWorkTaskRepository,
)
from neuroagent.infrastructure.persistence.database import Database
from neuroagent.infrastructure.persistence.unit_of_work import SqliteUnitOfWork


def make_task() -> WorkTask:
    return WorkTask(
        task_id="task-persist-1",
        goal=WorkGoal(
            description="Finish the approved fMRI analysis",
            completion_conditions=("report exists", "QC decision is recorded"),
        ),
        authority_refs=AuthorityReferences(
            conversation_id="conversation-3",
            plan_revision_id="plan-rev-2",
            workflow_run_id="run-4",
            input_revision="manifest-hash-abc",
        ),
        steps=(TaskStep(step_id="inspect", sequence=0, kind="inspect_inputs"),),
        recovery_steps=("reload authoritative run state",),
        budget=TaskBudget(
            max_steps=12,
            max_model_calls=5,
            max_tokens=6000,
            max_elapsed_seconds=1800,
        ),
    )


def open_repository(database_path) -> tuple[Database, SqliteWorkTaskRepository]:
    database = Database(f"sqlite:///{database_path.as_posix()}")
    database.initialize()
    unit_of_work = SqliteUnitOfWork(database)
    return database, SqliteWorkTaskRepository(database, unit_of_work=unit_of_work)


def test_work_task_survives_database_restart_with_steps_and_authority_references(
    tmp_path,
) -> None:
    path = tmp_path / "automation.sqlite"
    first_db, first_repo = open_repository(path)
    task = make_task()
    first_repo.create_task(task)
    first_db.dispose()

    restarted_db, restarted_repo = open_repository(path)
    restored = restarted_repo.get_task(task.task_id)
    assert restored == task
    assert restored.authority_refs.workflow_run_id == "run-4"
    assert "run_status" not in WorkTask.model_fields
    restarted_db.dispose()


def test_save_task_uses_optimistic_version_and_rejects_stale_writer(tmp_path) -> None:
    database, repository = open_repository(tmp_path / "version.sqlite")
    original = make_task()
    repository.create_task(original)

    first_update = transition_task(original, TaskStatus.RUNNING)
    repository.save_task(first_update, expected_version=original.version)

    stale_update = transition_task(original, TaskStatus.BLOCKED)
    with pytest.raises(ConflictError) as error:
        repository.save_task(stale_update, expected_version=original.version)
    assert error.value.code == "revision_conflict"
    assert repository.get_task(original.task_id).status is TaskStatus.RUNNING
    database.dispose()


def test_event_receipt_and_cursor_commit_once_with_task_progress(tmp_path) -> None:
    path = tmp_path / "events.sqlite"
    database, repository = open_repository(path)
    task = make_task()
    repository.create_task(task)
    running = transition_task(task, TaskStatus.RUNNING)
    event = WorkEventIdentity(
        stream_key="workflow:run-4",
        workflow_run_id="run-4",
        event_id=31,
        dedupe_key="workflow:run-4:state-changed:31",
    )
    decision, committed = repository.consume_event(
        task.task_id,
        event,
        expected_version=task.version,
        updated_task=running,
    )
    assert decision is EventConsumptionDecision.CONSUME
    assert committed is not None
    assert committed.event_cursors[0].event_id == 31
    assert committed.version == running.version
    database.dispose()

    database, repository = open_repository(path)
    assert repository.get_task(task.task_id) == committed

    decision, replay = repository.consume_event(
        task.task_id,
        event,
        expected_version=task.version,
        updated_task=running,
    )
    assert decision is EventConsumptionDecision.DUPLICATE
    assert replay is None
    assert repository.get_task(task.task_id) == committed

    older_event = WorkEventIdentity(
        stream_key="workflow:run-4",
        workflow_run_id="run-4",
        event_id=30,
        dedupe_key="workflow:run-4:state-changed:30",
    )
    blocked = transition_task(committed, TaskStatus.BLOCKED)
    decision, stale = repository.consume_event(
        task.task_id,
        older_event,
        expected_version=committed.version,
        updated_task=blocked,
    )
    assert decision is EventConsumptionDecision.STALE
    assert stale is None
    assert repository.get_task(task.task_id) == committed
    database.dispose()


def test_event_consume_rejects_stale_task_version_without_advancing_cursor(tmp_path) -> None:
    database, repository = open_repository(tmp_path / "stale-event.sqlite")
    task = make_task()
    repository.create_task(task)
    current = transition_task(task, TaskStatus.RUNNING)
    repository.save_task(current, expected_version=task.version)

    delayed = WorkEventIdentity(
        stream_key="workflow:run-4",
        workflow_run_id="run-4",
        event_id=8,
        dedupe_key="delayed-event",
    )
    stale_candidate = transition_task(task, TaskStatus.RUNNING)
    stale_candidate = stale_candidate.model_copy(
        update={"steps": (transition_task_step(task.steps[0], TaskStepStatus.READY),)}
    )
    with pytest.raises(ConflictError) as error:
        repository.consume_event(
            task.task_id,
            delayed,
            expected_version=task.version,
            updated_task=stale_candidate,
        )
    assert error.value.code == "revision_conflict"
    stored = repository.get_task(task.task_id)
    assert stored.version == current.version
    assert stored.event_cursors == ()
    database.dispose()
