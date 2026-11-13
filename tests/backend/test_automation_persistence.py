from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

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
from neuroagent.application.automation.result_interpretation import (
    ResultInterpretationStatus,
    StatisticalResultInterpretationOutbox,
)
from neuroagent.application.automation.step_lifecycle import bind_prepared_step_plan_for_review
from neuroagent.application.automation.step_preparation import (
    StepPreparationClaim,
    resolve_step_preparation_target,
)
from neuroagent.application.errors import ConflictError
from neuroagent.domain.fmri.preprocessing_outputs import DpabiPreprocessingOutput
from neuroagent.infrastructure.persistence.automation_repository import (
    SqliteWorkTaskRepository,
)
from neuroagent.infrastructure.persistence.database import Database
from neuroagent.infrastructure.persistence.models import ConversationMessageRow, ConversationRow
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


def make_step_preparation_task(
    *, task_id: str = "task-step-prepare", target_kind: str = "statistical_run"
) -> WorkTask:
    return WorkTask(
        task_id=task_id,
        goal=WorkGoal(
            description="Prepare and review a follow-up fMRI analysis",
            completion_conditions=("all planned analysis steps are verified",),
        ),
        status=TaskStatus.RUNNING,
        authority_refs=AuthorityReferences(
            conversation_id="conversation-3",
        ),
        steps=(
            TaskStep(
                step_id="preprocessing",
                sequence=0,
                kind="preprocessing_run",
                status=TaskStepStatus.SUCCEEDED,
                input_version="preprocessing-input-v1",
                authority_refs=AuthorityReferences(
                    plan_revision_id="preprocessing-plan",
                    workflow_run_id="preprocessing-run",
                    input_revision="manifest-v1",
                    artifact_ids=("metric-alff", "mask-common"),
                ),
                version=2,
            ),
            TaskStep(
                step_id="analysis",
                sequence=1,
                kind=target_kind,
                status=TaskStepStatus.READY,
                input_version="analysis-request-v1",
                authority_refs=AuthorityReferences(
                    conversation_id="conversation-3",
                    analysis_intent_task_id="intent-1",
                    analysis_request_hash="a" * 64,
                    input_revision="analysis-input-v1",
                ),
                version=3,
            ),
            TaskStep(
                step_id="report",
                sequence=2,
                kind="statistical_run",
                status=TaskStepStatus.PENDING,
            ),
        ),
        budget=TaskBudget(
            max_steps=12,
            max_model_calls=5,
            max_tokens=6000,
            max_elapsed_seconds=1800,
        ),
        version=7,
    )


def open_repository(database_path) -> tuple[Database, SqliteWorkTaskRepository]:
    database = Database(f"sqlite:///{database_path.as_posix()}")
    database.initialize()
    unit_of_work = SqliteUnitOfWork(database)
    return database, SqliteWorkTaskRepository(database, unit_of_work=unit_of_work)


def ensure_conversation(database: Database, conversation_id: str) -> None:
    with database.session_factory.begin() as session:
        if session.get(ConversationRow, conversation_id) is None:
            session.add(
                ConversationRow(
                    conversation_id=conversation_id,
                    mode="work",
                    title="Synthetic test conversation",
                    project_id=None,
                    version=1,
                )
            )


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


def test_step_preparation_target_fingerprint_binds_goal_step_and_successful_inputs() -> None:
    task = make_step_preparation_task()
    target = resolve_step_preparation_target(task)
    assert target is not None

    changed_goal = task.model_copy(
        update={"goal": task.goal.model_copy(update={"description": "Changed goal"})}
    )
    assert (
        resolve_step_preparation_target(changed_goal).input_fingerprint != target.input_fingerprint
    )

    changed_step = task.model_copy(
        update={
            "steps": (
                task.steps[0],
                task.steps[1].model_copy(update={"version": task.steps[1].version + 1}),
                task.steps[2],
            )
        }
    )
    assert (
        resolve_step_preparation_target(changed_step).input_fingerprint != target.input_fingerprint
    )

    changed_predecessor = task.model_copy(
        update={
            "steps": (
                task.steps[0].model_copy(
                    update={
                        "authority_refs": task.steps[0].authority_refs.model_copy(
                            update={"artifact_ids": ("different-metric",)}
                        )
                    }
                ),
                task.steps[1],
                task.steps[2],
            )
        }
    )
    assert (
        resolve_step_preparation_target(changed_predecessor).input_fingerprint
        != target.input_fingerprint
    )


def test_step_preparation_claim_returns_exact_ready_target_and_persists_across_restart(
    tmp_path,
) -> None:
    path = tmp_path / "step-preparation-restart.sqlite"
    database, repository = open_repository(path)
    task = make_step_preparation_task()
    repository.create_task(task)
    expected = resolve_step_preparation_target(task)
    assert expected is not None
    database.dispose()

    restarted_db, restarted_repo = open_repository(path)
    claim = restarted_repo.claim_next_step_preparation(
        "prepare-worker", lease_seconds=30, cooldown_seconds=0
    )
    assert claim is not None
    assert claim.task_id == expected.task_id
    assert claim.task_version == expected.task_version
    assert claim.step_id == expected.step_id
    assert claim.step_kind == expected.step_kind
    assert claim.step_version == expected.step_version
    assert claim.input_fingerprint == expected.input_fingerprint
    assert claim.claim_token
    assert claim.lease_expires_at > 0
    assert (
        restarted_repo.claim_next_step_preparation(
            "other-worker", lease_seconds=30, cooldown_seconds=0
        )
        is None
    )
    assert restarted_repo.release_step_preparation_claim(claim)
    restarted_db.dispose()


def test_step_preparation_claim_fences_plan_review_transition_atomically(tmp_path) -> None:
    database, repository = open_repository(tmp_path / "step-preparation-review.sqlite")
    task = make_step_preparation_task()
    repository.create_task(task)
    claim = repository.claim_next_step_preparation(
        "prepare-worker", lease_seconds=30, cooldown_seconds=0
    )
    assert claim is not None
    successor = task.steps[1]
    reviewed = bind_prepared_step_plan_for_review(
        task,
        successor,
        plan_revision_id="statistical-plan-1",
        input_revision="analysis-input-v1",
        waiting_reason="Review the statistical analysis design.",
    )

    repository.save_task(
        reviewed,
        expected_version=task.version,
        step_preparation_claim=claim,
    )

    stored = repository.get_task(task.task_id)
    assert stored.status is TaskStatus.WAITING_USER
    assert stored.steps[1].authority_refs.plan_revision_id == "statistical-plan-1"
    assert (
        repository.claim_next_step_preparation("other-worker", lease_seconds=30, cooldown_seconds=0)
        is None
    )
    stale_followup = stored.model_copy(
        update={
            "status": TaskStatus.RUNNING,
            "waiting_condition": None,
            "version": stored.version + 1,
        }
    )
    with pytest.raises(ConflictError) as error:
        repository.save_task(
            stale_followup,
            expected_version=stored.version,
            step_preparation_claim=claim,
        )
    assert error.value.code == "step_preparation_claim_lost"
    database.dispose()


@pytest.mark.parametrize(
    "case",
    ("predecessor_pending", "multiple_active", "unknown_kind", "single_step", "legacy"),
)
def test_invalid_or_unsupported_step_preparation_targets_are_not_claimed(tmp_path, case) -> None:
    task = make_step_preparation_task(task_id=f"task-step-prepare-{case}")
    if case == "predecessor_pending":
        task = task.model_copy(
            update={
                "steps": (
                    task.steps[0].model_copy(update={"status": TaskStepStatus.PENDING}),
                    task.steps[1],
                    task.steps[2],
                )
            }
        )
    elif case == "multiple_active":
        task = task.model_copy(
            update={
                "steps": (
                    task.steps[0],
                    task.steps[1],
                    task.steps[2].model_copy(update={"status": TaskStepStatus.READY}),
                )
            }
        )
    elif case == "unknown_kind":
        task = make_step_preparation_task(
            task_id=f"task-step-prepare-{case}", target_kind="unknown_step_kind"
        )
    elif case == "single_step":
        task = task.model_copy(update={"steps": (task.steps[1],)})
    elif case == "legacy":
        task = task.model_copy(update={"steps": ()})

    database, repository = open_repository(tmp_path / f"step-preparation-{case}.sqlite")
    repository.create_task(task)
    assert (
        repository.claim_next_step_preparation(
            "prepare-worker", lease_seconds=30, cooldown_seconds=0
        )
        is None
    )
    database.dispose()


@pytest.mark.parametrize("change", ("step_version", "predecessor_artifacts"))
def test_step_preparation_claim_fails_after_step_or_input_fingerprint_changes(
    tmp_path, change
) -> None:
    database, repository = open_repository(tmp_path / f"step-preparation-stale-{change}.sqlite")
    task = make_step_preparation_task()
    repository.create_task(task)
    claim = repository.claim_next_step_preparation(
        "prepare-worker", lease_seconds=30, cooldown_seconds=0
    )
    assert claim is not None

    if change == "step_version":
        first = task.steps[0]
        target_step = task.steps[1].model_copy(update={"version": task.steps[1].version + 1})
    else:
        first = task.steps[0].model_copy(
            update={
                "authority_refs": task.steps[0].authority_refs.model_copy(
                    update={"artifact_ids": ("replacement-artifact",)}
                )
            }
        )
        target_step = task.steps[1]
    changed = task.model_copy(
        update={
            "steps": (first, target_step, task.steps[2]),
            "version": task.version + 1,
        }
    )
    repository.save_task(changed, expected_version=task.version)
    assert resolve_step_preparation_target(changed).input_fingerprint != claim.input_fingerprint

    current_step = changed.steps[1]
    preparing = changed.model_copy(
        update={
            "steps": (
                changed.steps[0],
                current_step.model_copy(
                    update={"status": TaskStepStatus.RUNNING, "version": current_step.version + 1}
                ),
                changed.steps[2],
            ),
            "version": changed.version + 1,
        }
    )
    with pytest.raises(ConflictError) as error:
        repository.save_task(
            preparing,
            expected_version=changed.version,
            step_preparation_claim=claim.model_copy(update={"task_version": changed.version}),
        )
    assert error.value.code == "step_preparation_claim_lost"
    database.dispose()


def test_step_preparation_claim_expiry_replaces_token_and_fences_old_worker(
    tmp_path, monkeypatch
) -> None:
    import neuroagent.infrastructure.persistence.automation_repository as automation_repository

    now = [1000.0]
    monkeypatch.setattr(automation_repository.time, "time", lambda: now[0])
    path = tmp_path / "step-preparation-expiry.sqlite"
    database, repository = open_repository(path)
    task = make_step_preparation_task()
    repository.create_task(task)
    barrier = Barrier(2)

    def claim(worker_id: str) -> StepPreparationClaim | None:
        barrier.wait()
        return repository.claim_next_step_preparation(
            worker_id, lease_seconds=5, cooldown_seconds=0
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        simultaneous = tuple(executor.map(claim, ("worker-a", "worker-b")))
    first = next(item for item in simultaneous if item is not None)
    assert first is not None
    assert sum(item is not None for item in simultaneous) == 1
    database.dispose()

    database, repository = open_repository(path)
    assert (
        repository.claim_next_step_preparation(
            "worker-after-restart", lease_seconds=5, cooldown_seconds=0
        )
        is None
    )

    now[0] += 5
    second = repository.claim_next_step_preparation("worker-b", lease_seconds=5, cooldown_seconds=0)
    assert second is not None
    assert second.claim_token != first.claim_token
    next_step = task.steps[1]
    preparing = task.model_copy(
        update={
            "steps": (
                task.steps[0],
                next_step.model_copy(
                    update={"status": TaskStepStatus.RUNNING, "version": next_step.version + 1}
                ),
                task.steps[2],
            ),
            "version": task.version + 1,
        }
    )
    with pytest.raises(ConflictError) as error:
        repository.save_task(
            preparing,
            expected_version=task.version,
            step_preparation_claim=first,
        )
    assert error.value.code == "step_preparation_claim_lost"
    repository.save_task(
        preparing,
        expected_version=task.version,
        step_preparation_claim=second,
    )
    assert repository.get_task(task.task_id).steps[1].status is TaskStepStatus.RUNNING
    assert not repository.release_step_preparation_claim(first)
    database.dispose()


def test_step_preparation_release_preserves_cooldown(tmp_path, monkeypatch) -> None:
    import neuroagent.infrastructure.persistence.automation_repository as automation_repository

    now = [1000.0]
    monkeypatch.setattr(automation_repository.time, "time", lambda: now[0])
    database, repository = open_repository(tmp_path / "step-preparation-cooldown.sqlite")
    repository.create_task(make_step_preparation_task())
    claim = repository.claim_next_step_preparation(
        "prepare-worker", lease_seconds=30, cooldown_seconds=10
    )
    assert claim is not None
    assert repository.release_step_preparation_claim(claim)
    assert (
        repository.claim_next_step_preparation("other-worker", lease_seconds=30, cooldown_seconds=0)
        is None
    )
    now[0] += 10
    assert (
        repository.claim_next_step_preparation("other-worker", lease_seconds=30, cooldown_seconds=0)
        is not None
    )
    database.dispose()


def test_analysis_outbox_is_idempotent_and_ready_binds_exact_verified_ids(tmp_path) -> None:
    path = tmp_path / "analysis-outbox.sqlite"
    database, repository = open_repository(path)
    ensure_conversation(database, "conversation-3")
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
    ensure_conversation(database, "conversation-3")
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


def test_result_interpretation_outbox_binds_unique_task_and_result(tmp_path, monkeypatch) -> None:
    import neuroagent.infrastructure.persistence.automation_repository as automation_repository

    now = [1000.0]
    monkeypatch.setattr(automation_repository.time, "time", lambda: now[0])
    database, repository = open_repository(tmp_path / "result-interpretation-outbox.sqlite")
    ensure_conversation(database, "conversation-3")
    record = StatisticalResultInterpretationOutbox(
        task_id="stat-task-1",
        conversation_id="conversation-3",
        project_id="project-1",
        run_id="run-1",
        plan_revision_id="plan-1",
        result_id="result-1",
        result_bundle_hash="a" * 64,
        result_version=1,
        artifact_ids=("artifact-1",),
    )
    assert repository.ensure_result_interpretation_outbox(record) == record
    assert repository.ensure_result_interpretation_outbox(record) == record
    conflicting_task = record.model_copy(update={"task_id": "stat-task-2"})
    with pytest.raises(ConflictError, match="不同的 WorkTask"):
        repository.ensure_result_interpretation_outbox(conflicting_task)
    conflicting_result = record.model_copy(update={"result_id": "result-2"})
    with pytest.raises(ConflictError, match="不同的 WorkTask"):
        repository.ensure_result_interpretation_outbox(conflicting_result)

    claim = repository.claim_result_interpretation_outbox("worker-a", lease_seconds=5)
    assert claim is not None and claim.task_id == "stat-task-1"
    assert repository.claim_result_interpretation_outbox("worker-b", lease_seconds=5) is None
    now[0] += 6
    second = repository.claim_result_interpretation_outbox("worker-b", lease_seconds=5)
    assert second is not None and second.fencing_token > claim.fencing_token
    with pytest.raises(ConflictError, match="claim 已过期"):
        repository.complete_result_interpretation_outbox(
            claim,
            status=ResultInterpretationStatus.COMPLETED,
            continuation_message_id="stale-message",
        )
    with database.session_factory.begin() as session:
        session.add(
            ConversationMessageRow(
                message_id="assistant-result-1",
                conversation_id="conversation-3",
                sequence=1,
                role="assistant",
                content="result interpretation draft",
                payload_json="{}",
                continuation_key="stat_result_interpretation:stat-task-1:g0",
            )
        )
    completed = repository.complete_result_interpretation_outbox(
        second,
        status=ResultInterpretationStatus.COMPLETED,
        continuation_message_id="assistant-result-1",
    )
    assert completed.status is ResultInterpretationStatus.COMPLETED
    assert repository.get_result_interpretation_outbox("stat-task-1") == completed
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


def test_multi_step_reconciliation_target_uses_unique_waiting_step_run(tmp_path) -> None:
    database, repository = open_repository(tmp_path / "multi-step-target.sqlite")
    task = make_task().model_copy(
        update={
            "authority_refs": AuthorityReferences(),
            "status": TaskStatus.WAITING_RUN,
            "waiting_condition": WaitingCondition(
                kind=WaitingKind.WORKFLOW_RUN,
                reference="step-run",
                reason="active step is waiting for its run",
            ),
            "steps": (
                TaskStep(
                    step_id="active-step",
                    sequence=0,
                    kind="statistical_run",
                    status=TaskStepStatus.WAITING,
                    authority_refs=AuthorityReferences(workflow_run_id="step-run"),
                ),
                TaskStep(
                    step_id="next-step",
                    sequence=1,
                    kind="preprocessing_run",
                    status=TaskStepStatus.PENDING,
                ),
            ),
            "version": 2,
        }
    )

    repository.create_task(task)
    claim = repository.claim_next_reconciliation_task(
        "multi-step-worker", lease_seconds=10, cooldown_seconds=0
    )

    assert claim is not None
    assert claim.task_id == task.task_id
    repository.release_reconciliation_claim(claim.task_id, claim.worker_id, claim.claim_token)
    database.dispose()


def test_ambiguous_multi_step_task_is_not_a_reconciliation_target(tmp_path) -> None:
    database, repository = open_repository(tmp_path / "ambiguous-multi-step-target.sqlite")
    task = make_task().model_copy(
        update={
            "status": TaskStatus.WAITING_RUN,
            "waiting_condition": WaitingCondition(
                kind=WaitingKind.WORKFLOW_RUN,
                reference="run-4",
                reason="ambiguous active steps",
            ),
            "steps": (
                TaskStep(
                    step_id="active-step-a",
                    sequence=0,
                    kind="statistical_run",
                    status=TaskStepStatus.WAITING,
                    authority_refs=AuthorityReferences(workflow_run_id="run-4"),
                ),
                TaskStep(
                    step_id="active-step-b",
                    sequence=1,
                    kind="preprocessing_run",
                    status=TaskStepStatus.WAITING,
                    authority_refs=AuthorityReferences(workflow_run_id="run-5"),
                ),
            ),
            "version": 2,
        }
    )

    repository.create_task(task)
    claim = repository.claim_next_reconciliation_task(
        "ambiguous-worker", lease_seconds=10, cooldown_seconds=0
    )

    assert claim is None
    database.dispose()


def test_multi_step_preprocessing_dependency_target_uses_active_step_plan(tmp_path) -> None:
    database, repository = open_repository(tmp_path / "multi-step-preprocessing-target.sqlite")
    task = make_task().model_copy(
        update={
            "authority_refs": AuthorityReferences(),
            "status": TaskStatus.WAITING_DEPENDENCY,
            "waiting_condition": WaitingCondition(
                kind=WaitingKind.DEPENDENCY,
                reference="downstream:step-run",
                reason="verify active preprocessing outputs",
            ),
            "steps": (
                TaskStep(
                    step_id="preprocessing-step",
                    sequence=0,
                    kind="preprocessing_run",
                    status=TaskStepStatus.WAITING,
                    authority_refs=AuthorityReferences(
                        plan_revision_id="step-plan",
                        workflow_run_id="step-run",
                    ),
                ),
                TaskStep(
                    step_id="analysis-step",
                    sequence=1,
                    kind="statistical_run",
                    status=TaskStepStatus.PENDING,
                ),
            ),
            "version": 2,
        }
    )

    repository.create_task(task)
    claim = repository.claim_next_reconciliation_task(
        "multi-preprocessing-worker", lease_seconds=10, cooldown_seconds=0
    )

    assert claim is not None
    assert claim.task_id == task.task_id
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
