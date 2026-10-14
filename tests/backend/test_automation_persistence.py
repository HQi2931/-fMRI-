from __future__ import annotations

import pytest

from neuroagent.application.automation.analysis_outbox import (
    AnalysisOutboxStatus,
    AnalysisProposalOutbox,
)
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
    transition_task,
    transition_task_step,
)
from neuroagent.application.errors import ConflictError
from neuroagent.domain.fmri.preprocessing_outputs import DpabiPreprocessingOutput
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
    expected = DpabiPreprocessingOutput(
        subject_id="sub-01",
        metric="timeseries",
        scaling=None,
        relative_path="output/sub-01/timeseries.nii",
        artifact_type="preprocessing.functional_timeseries",
    )
    task = make_task().model_copy(
        update={
            "steps": (
                TaskStep(
                    step_id="preprocessing",
                    sequence=0,
                    kind="preprocessing_run",
                    expected_artifacts=(expected,),
                ),
            )
        }
    )
    first_repo.create_task(task)
    first_db.dispose()

    restarted_db, restarted_repo = open_repository(path)
    restored = restarted_repo.get_task(task.task_id)
    assert restored == task
    assert restored.authority_refs.workflow_run_id == "run-4"
    assert restored.steps[0].expected_artifacts == (expected,)
    assert "run_status" not in WorkTask.model_fields
    restarted_db.dispose()


def test_reconciliation_claim_is_exclusive_and_respects_cooldown(tmp_path, monkeypatch) -> None:
    import neuroagent.infrastructure.persistence.automation_repository as automation_repository

    now = [1000.0]
    monkeypatch.setattr(automation_repository.time, "time", lambda: now[0])
    database, repository = open_repository(tmp_path / "claim-exclusive.sqlite")
    task = make_task().model_copy(
        update={
            "status": TaskStatus.WAITING_RUN,
            "waiting_condition": WaitingCondition(
                kind=WaitingKind.WORKFLOW_RUN,
                reference="run-4",
                reason="wait for the bound run",
            ),
            "version": 2,
        }
    )
    repository.create_task(task)

    first = repository.claim_next_reconciliation_task(
        "worker-a", lease_seconds=10, cooldown_seconds=3
    )
    assert first is not None
    assert first.task_id == task.task_id
    assert (
        repository.claim_next_reconciliation_task("worker-b", lease_seconds=10, cooldown_seconds=3)
        is None
    )
    assert repository.release_reconciliation_claim(
        first.task_id, first.worker_id, first.claim_token
    )
    assert (
        repository.claim_next_reconciliation_task("worker-b", lease_seconds=10, cooldown_seconds=3)
        is None
    )

    now[0] += 3
    second = repository.claim_next_reconciliation_task(
        "worker-b", lease_seconds=10, cooldown_seconds=0
    )
    assert second is not None
    assert second.worker_id == "worker-b"
    database.dispose()


def test_analysis_outbox_is_idempotent_and_ready_binds_exact_verified_ids(tmp_path) -> None:
    path = tmp_path / "analysis-outbox.sqlite"
    database, repository = open_repository(path)
    record = AnalysisProposalOutbox(
        intent_task_id="intent-01",
        conversation_id="conversation-3",
        project_id="project-1",
        preprocessing_task_id="prep-task-1",
        plan_revision_id="plan-1",
        manifest_hash="a" * 64,
        run_id="run-1",
        analysis_request_hash="b" * 64,
    )
    assert repository.ensure_analysis_outbox(record) == record
    assert repository.ensure_analysis_outbox(record) == record
    with pytest.raises(ConflictError):
        repository.ensure_analysis_outbox(record.model_copy(update={"run_id": "wrong-run"}))
    with pytest.raises(ConflictError):
        repository.mark_analysis_outbox_ready(
            "intent-01",
            preprocessing_task_id="other-task",
            plan_revision_id="plan-1",
            manifest_hash="a" * 64,
            run_id="run-1",
            artifact_ids=("art-1",),
        )
    with pytest.raises(ConflictError):
        repository.mark_analysis_outbox_ready(
            "intent-01",
            preprocessing_task_id="prep-task-1",
            plan_revision_id="plan-1",
            manifest_hash="a" * 64,
            run_id="wrong-run",
            artifact_ids=("art-1",),
        )

    ready = repository.mark_analysis_outbox_ready(
        "intent-01",
        preprocessing_task_id="prep-task-1",
        plan_revision_id="plan-1",
        manifest_hash="a" * 64,
        run_id="run-1",
        artifact_ids=("art-2", "art-1"),
    )
    assert ready.status is AnalysisOutboxStatus.READY
    assert ready.artifact_ids == ("art-2", "art-1")
    assert (
        repository.mark_analysis_outbox_ready(
            "intent-01",
            preprocessing_task_id="prep-task-1",
            plan_revision_id="plan-1",
            manifest_hash="a" * 64,
            run_id="run-1",
            artifact_ids=("art-2", "art-1"),
        )
        == ready
    )
    with pytest.raises(ConflictError):
        repository.mark_analysis_outbox_ready(
            "intent-01",
            preprocessing_task_id="prep-task-1",
            plan_revision_id="plan-1",
            manifest_hash="a" * 64,
            run_id="run-1",
            artifact_ids=("different",),
        )
    database.dispose()

    restarted, restarted_repository = open_repository(path)
    restored = restarted_repository.get_analysis_outbox("intent-01")
    assert restored == ready
    assert "analysis_request" not in AnalysisProposalOutbox.model_fields
    restarted.dispose()


def test_analysis_outbox_claim_uses_expiring_lease_and_fencing_token(tmp_path, monkeypatch) -> None:
    import neuroagent.infrastructure.persistence.automation_repository as automation_repository

    now = [1000.0]
    monkeypatch.setattr(automation_repository.time, "time", lambda: now[0])
    database, repository = open_repository(tmp_path / "analysis-claim.sqlite")
    record = AnalysisProposalOutbox(
        intent_task_id="intent-claim",
        conversation_id="conversation-3",
        project_id="project-1",
        preprocessing_task_id="prep-task-claim",
        plan_revision_id="plan-claim",
        manifest_hash="c" * 64,
        run_id="run-claim",
        analysis_request_hash="d" * 64,
    )
    repository.ensure_analysis_outbox(record)
    repository.mark_analysis_outbox_ready(
        record.intent_task_id,
        preprocessing_task_id=record.preprocessing_task_id,
        plan_revision_id=record.plan_revision_id,
        manifest_hash=record.manifest_hash,
        run_id=record.run_id,
        artifact_ids=("artifact-1",),
    )
    first = repository.claim_analysis_outbox("worker-a", lease_seconds=5)
    assert first is not None
    assert first.fencing_token == 1
    assert repository.validate_analysis_outbox_claim(first)
    assert repository.claim_analysis_outbox("worker-b", lease_seconds=5) is None

    now[0] += 6
    second = repository.claim_analysis_outbox("worker-b", lease_seconds=5)
    assert second is not None
    assert second.fencing_token == 2
    assert not repository.validate_analysis_outbox_claim(first)
    assert repository.validate_analysis_outbox_claim(second)
    database.dispose()


def test_expired_reconciliation_claim_is_fenced_after_reclaim(tmp_path, monkeypatch) -> None:
    import neuroagent.infrastructure.persistence.automation_repository as automation_repository

    now = [2000.0]
    monkeypatch.setattr(automation_repository.time, "time", lambda: now[0])
    database, repository = open_repository(tmp_path / "claim-fencing.sqlite")
    task = make_task().model_copy(
        update={
            "status": TaskStatus.WAITING_RUN,
            "waiting_condition": WaitingCondition(
                kind=WaitingKind.WORKFLOW_RUN,
                reference="run-4",
                reason="wait for the bound run",
            ),
            "version": 2,
        }
    )
    repository.create_task(task)
    old_claim = repository.claim_next_reconciliation_task(
        "old-worker", lease_seconds=5, cooldown_seconds=0
    )
    assert old_claim is not None

    now[0] += 6
    new_claim = repository.claim_next_reconciliation_task(
        "new-worker", lease_seconds=10, cooldown_seconds=0
    )
    assert new_claim is not None
    assert new_claim.claim_token != old_claim.claim_token

    blocked = transition_task(task, TaskStatus.BLOCKED)
    with pytest.raises(ConflictError) as error:
        repository.save_task(
            blocked,
            expected_version=task.version,
            claim_token=old_claim.claim_token,
        )
    assert error.value.code == "work_task_claim_lost"
    assert repository.get_task(task.task_id) == task

    repository.save_task(
        blocked,
        expected_version=task.version,
        claim_token=new_claim.claim_token,
    )
    assert repository.get_task(task.task_id).status is TaskStatus.BLOCKED
    assert not repository.release_reconciliation_claim(
        old_claim.task_id, old_claim.worker_id, old_claim.claim_token
    )
    assert repository.release_reconciliation_claim(
        new_claim.task_id, new_claim.worker_id, new_claim.claim_token
    )
    database.dispose()


@pytest.mark.parametrize(
    ("status", "waiting_kind", "reference", "expected"),
    [
        (TaskStatus.WAITING_USER, WaitingKind.USER_DECISION, "proposal:hash", False),
        (TaskStatus.WAITING_DEPENDENCY, WaitingKind.DEPENDENCY, "downstream:run-4", False),
        (
            TaskStatus.WAITING_USER,
            WaitingKind.USER_DECISION,
            "qc:run-4",
            True,
        ),
        (
            TaskStatus.WAITING_DEPENDENCY,
            WaitingKind.DEPENDENCY,
            "statistical-result:run-4",
            True,
        ),
    ],
)
def test_reconciliation_target_only_indexes_supported_waits(
    tmp_path, status, waiting_kind, reference, expected
) -> None:
    database, repository = open_repository(tmp_path / f"target-{status.value}-{expected}.sqlite")
    task = make_task().model_copy(
        update={
            "status": status,
            "waiting_condition": WaitingCondition(
                kind=waiting_kind,
                reference=reference,
                reason="test wait",
            ),
            "version": 2,
        }
    )
    repository.create_task(task)
    claim = repository.claim_next_reconciliation_task(
        "target-worker", lease_seconds=10, cooldown_seconds=0
    )
    assert (claim is not None) is expected
    if claim is not None:
        repository.release_reconciliation_claim(claim.task_id, claim.worker_id, claim.claim_token)
    database.dispose()


def test_preprocessing_downstream_wait_is_claimable_but_generic_dependency_is_not(
    tmp_path,
) -> None:
    database, repository = open_repository(tmp_path / "preprocessing-dependency-target.sqlite")
    task = make_task().model_copy(
        update={
            "status": TaskStatus.WAITING_DEPENDENCY,
            "waiting_condition": WaitingCondition(
                kind=WaitingKind.DEPENDENCY,
                reference="downstream:run-4",
                reason="verify frozen preprocessing outputs",
            ),
            "steps": (
                TaskStep(
                    step_id="preprocessing:plan-rev-2",
                    sequence=0,
                    kind="preprocessing_run",
                    status=TaskStepStatus.WAITING,
                    authority_refs=AuthorityReferences(
                        plan_revision_id="plan-rev-2",
                        workflow_run_id="run-4",
                    ),
                ),
            ),
            "version": 2,
        }
    )
    repository.create_task(task)
    claim = repository.claim_next_reconciliation_task(
        "preprocessing-worker", lease_seconds=10, cooldown_seconds=0
    )
    assert claim is not None
    assert claim.task_id == task.task_id
    repository.release_reconciliation_claim(claim.task_id, claim.worker_id, claim.claim_token)
    repository.save_task(
        transition_task(task, TaskStatus.BLOCKED),
        expected_version=task.version,
    )

    generic = make_task().model_copy(
        update={
            "task_id": "generic-downstream-task",
            "status": TaskStatus.WAITING_DEPENDENCY,
            "waiting_condition": WaitingCondition(
                kind=WaitingKind.DEPENDENCY,
                reference="downstream:run-4",
                reason="not a preprocessing target",
            ),
            "version": 2,
        }
    )
    repository.create_task(generic)
    assert (
        repository.claim_next_reconciliation_task(
            "preprocessing-worker", lease_seconds=10, cooldown_seconds=0
        )
        is None
    )
    database.dispose()


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
