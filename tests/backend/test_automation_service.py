from __future__ import annotations

from types import SimpleNamespace

import pytest

from neuroagent.application.automation.models import (
    AuthorityReferences,
    EventConsumptionDecision,
    RunReadiness,
    TaskBudget,
    TaskStatus,
    WaitingCondition,
    WaitingKind,
    WorkEventIdentity,
    WorkGoal,
    WorkTask,
)
from neuroagent.application.contracts.runs import QcReviewState
from neuroagent.application.errors import InputValidationError, NotFoundError
from neuroagent.bootstrap import build_service
from neuroagent.domain.execution import QcExecutionMode
from neuroagent.domain.lifecycle import WorkflowState


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


def make_waiting_task(*, task_id: str = "reconcile-task") -> WorkTask:
    task = make_task().model_copy(update={"task_id": task_id})
    return task.model_copy(
        update={
            "status": TaskStatus.WAITING_RUN,
            "waiting_condition": WaitingCondition(
                kind=WaitingKind.WORKFLOW_RUN,
                reference="run-application",
                reason="worker is processing the run",
            ),
            "version": task.version + 1,
        }
    )


class AuthoritativeReadDoubles:
    def __init__(
        self,
        *,
        run_state: WorkflowState,
        qc_status: str,
        review_state: QcReviewState | None = None,
        artifact_ids: tuple[str, ...] = ("artifact-a",),
        referenced_artifacts: tuple[SimpleNamespace, ...] = (),
    ) -> None:
        self.run = SimpleNamespace(
            run_id="run-application",
            project_id="project-1",
            state=run_state,
            version=4,
            attempt=2,
            qc_mode=QcExecutionMode.REQUIRE,
            qc_status=qc_status,
        )
        self.review = (
            SimpleNamespace(
                run_id="run-application",
                state=review_state,
                review=SimpleNamespace(review_revision_id="qc-review-a"),
            )
            if review_state is not None
            else None
        )
        self.skip = None
        self.artifacts = tuple(
            SimpleNamespace(
                run_id="run-application",
                project_id="project-1",
                artifact_id=artifact_id,
                artifact_type="alff_map",
            )
            for artifact_id in artifact_ids
        )
        self.referenced_artifacts = {
            artifact.artifact_id: artifact for artifact in (*self.artifacts, *referenced_artifacts)
        }
        self.on_review_read = None
        self.run_reads = 0

    def get_run(self, run_id: str):
        assert run_id == self.run.run_id
        self.run_reads += 1
        return SimpleNamespace(**vars(self.run))

    def get_latest_qc_review_for_run(self, run_id: str):
        assert run_id == "run-application"
        if self.on_review_read is not None:
            self.on_review_read()
        return self.review

    def get_qc_skip_for_run(self, run_id: str):
        assert run_id == "run-application"
        return self.skip

    def list_artifacts(self, run_id: str):
        assert run_id == "run-application"
        return list(self.artifacts)

    def get_artifact(self, artifact_id: str):
        if artifact_id not in self.referenced_artifacts:
            raise NotFoundError("artifact", artifact_id)
        return self.referenced_artifacts[artifact_id]


def bind_authoritative_read_doubles(service, reads: AuthoritativeReadDoubles) -> None:
    service.automation._run_repository = reads
    service.automation._qc_repository = reads
    service.automation._artifact_repository = reads


def add_run_event(service, *, run_id: str = "run-application", payload=None) -> None:
    service.event_store.append_event(
        project_id=None,
        run_id=run_id,
        event_type="WorkflowTransitioned",
        severity="info",
        payload=payload or {"state": "succeeded", "qc_status": "approved"},
    )


def test_reconciliation_reads_authoritative_running_state_and_duplicate_delivery_is_noop(
    service,
) -> None:
    task = make_waiting_task()
    service.automation.create_task(task)
    bind_authoritative_read_doubles(
        service,
        AuthoritativeReadDoubles(
            run_state=WorkflowState.RUNNING,
            qc_status="pending",
        ),
    )
    # The event payload claims completion and approval; it must not override the
    # authoritative Run/QC reads below.
    add_run_event(service, payload={"state": "succeeded", "qc_status": "approved"})

    first = service.automation.reconcile_waiting_run(task.task_id)
    version_after_first = first.task.version
    replay = service.automation.reconcile_waiting_run(task.task_id)

    assert first.readiness is RunReadiness.WAITING_RUN
    assert first.authoritative is not None
    assert first.authoritative.run_state == WorkflowState.RUNNING.value
    assert first.authoritative.qc_status == "pending"
    assert first.event_receipts[0].decision is EventConsumptionDecision.CONSUME
    assert replay.event_receipts == ()
    assert replay.task.status is TaskStatus.WAITING_RUN
    assert replay.task.version == version_after_first


def test_reconciliation_blocks_failed_run_without_retry_or_new_run(service) -> None:
    task = make_waiting_task()
    service.automation.create_task(task)
    bind_authoritative_read_doubles(
        service,
        AuthoritativeReadDoubles(
            run_state=WorkflowState.FAILED_TERMINAL,
            qc_status="pending",
        ),
    )
    add_run_event(service, payload={"state": "succeeded"})

    result = service.automation.reconcile_waiting_run(task.task_id)

    assert result.readiness is RunReadiness.BLOCKED
    assert result.task.status is TaskStatus.BLOCKED
    assert result.authoritative is not None
    assert result.authoritative.run_state == WorkflowState.FAILED_TERMINAL.value
    assert "不会创建新 Run 或重试" in result.reason


def test_reconciliation_keeps_retryable_run_waiting_on_same_run(service) -> None:
    task = make_waiting_task()
    service.automation.create_task(task)
    bind_authoritative_read_doubles(
        service,
        AuthoritativeReadDoubles(
            run_state=WorkflowState.FAILED_RETRYABLE,
            qc_status="pending",
        ),
    )
    add_run_event(service, payload={"state": "failed_terminal"})

    result = service.automation.reconcile_waiting_run(task.task_id)

    assert result.readiness is RunReadiness.WAITING_RUN
    assert result.task.status is TaskStatus.WAITING_RUN
    assert result.authoritative is not None
    assert result.authoritative.run_id == task.authority_refs.workflow_run_id
    assert "同一 Run" in result.reason


def test_reconciliation_retries_when_qc_approval_changes_run_mid_read(service) -> None:
    task = make_waiting_task()
    service.automation.create_task(task)
    reads = AuthoritativeReadDoubles(
        run_state=WorkflowState.QC_REVIEW,
        qc_status="failed",
        review_state=QcReviewState.DRAFT,
    )

    def approve_between_reads() -> None:
        reads.on_review_read = None
        reads.run.state = WorkflowState.SUCCEEDED
        reads.run.qc_status = "approved"
        reads.run.version += 1
        reads.review.state = QcReviewState.APPROVED

    reads.on_review_read = approve_between_reads
    bind_authoritative_read_doubles(service, reads)
    add_run_event(service, payload={"qc_status": "failed"})

    result = service.automation.reconcile_waiting_run(task.task_id)

    assert reads.run_reads == 4
    assert result.readiness is RunReadiness.WAITING_DEPENDENCY
    assert result.task.status is TaskStatus.WAITING_DEPENDENCY
    assert result.authoritative is not None
    assert result.authoritative.run_version == reads.run.version
    assert result.authoritative.qc_status == "approved"


def test_continuously_changing_run_returns_indeterminate_without_blocking_task(service) -> None:
    task = make_waiting_task()
    service.automation.create_task(task)

    class AlwaysChangingRunReads(AuthoritativeReadDoubles):
        def get_run(self, run_id: str):
            result = super().get_run(run_id)
            result.version = self.run_reads
            return result

    reads = AlwaysChangingRunReads(
        run_state=WorkflowState.RUNNING,
        qc_status="pending",
    )
    bind_authoritative_read_doubles(service, reads)
    add_run_event(service)

    result = service.automation.reconcile_waiting_run(task.task_id)

    assert reads.run_reads == 6
    assert result.readiness is RunReadiness.INDETERMINATE
    assert result.authoritative is None
    assert result.task.status is TaskStatus.WAITING_RUN
    assert service.automation.get_task(task.task_id).status is TaskStatus.WAITING_RUN


def test_head_motion_qc_failed_with_draft_review_waits_for_user(service) -> None:
    task = make_waiting_task()
    service.automation.create_task(task)
    bind_authoritative_read_doubles(
        service,
        AuthoritativeReadDoubles(
            run_state=WorkflowState.QC_REVIEW,
            qc_status="failed",
            review_state=QcReviewState.DRAFT,
        ),
    )
    add_run_event(service)

    result = service.automation.reconcile_waiting_run(task.task_id)

    assert result.readiness is RunReadiness.WAITING_USER
    assert result.task.status is TaskStatus.WAITING_USER
    assert result.authoritative is not None
    assert result.authoritative.latest_qc_review_state == QcReviewState.DRAFT.value


def test_qc_awaiting_review_without_draft_still_waits_for_user_to_create_review(service) -> None:
    task = make_waiting_task()
    service.automation.create_task(task)
    bind_authoritative_read_doubles(
        service,
        AuthoritativeReadDoubles(
            run_state=WorkflowState.QC_REVIEW,
            qc_status="awaiting_review",
        ),
    )
    add_run_event(service)

    result = service.automation.reconcile_waiting_run(task.task_id)

    assert result.readiness is RunReadiness.WAITING_USER
    assert result.task.status is TaskStatus.WAITING_USER
    assert result.authoritative is not None
    assert result.authoritative.latest_qc_review_state is None


@pytest.mark.parametrize("review_state", [None, QcReviewState.REJECTED])
def test_head_motion_qc_failed_without_valid_draft_review_fails_closed(
    service, review_state
) -> None:
    task = make_waiting_task()
    service.automation.create_task(task)
    bind_authoritative_read_doubles(
        service,
        AuthoritativeReadDoubles(
            run_state=WorkflowState.QC_REVIEW,
            qc_status="failed",
            review_state=review_state,
        ),
    )
    add_run_event(service)

    result = service.automation.reconcile_waiting_run(task.task_id)

    assert result.readiness is RunReadiness.BLOCKED
    assert result.task.status is TaskStatus.BLOCKED


def test_succeeded_run_with_pending_qc_waits_for_user_and_never_approves(service) -> None:
    task = make_waiting_task()
    service.automation.create_task(task)
    reads = AuthoritativeReadDoubles(
        run_state=WorkflowState.SUCCEEDED,
        qc_status="pending",
        review_state=QcReviewState.DRAFT,
    )
    bind_authoritative_read_doubles(service, reads)
    add_run_event(service, payload={"state": "succeeded", "qc_status": "approved"})

    result = service.automation.reconcile_waiting_run(task.task_id)

    assert result.readiness is RunReadiness.WAITING_USER
    assert result.task.status is TaskStatus.WAITING_USER
    assert result.task.waiting_condition is not None
    assert result.task.waiting_condition.kind is WaitingKind.USER_DECISION
    assert result.authoritative is not None
    assert result.authoritative.latest_qc_review_state == QcReviewState.DRAFT.value
    assert reads.review.state is QcReviewState.DRAFT

    # A later wakeup re-reads the user-authored approval through the QC port.
    reads.run.qc_status = "approved"
    reads.review.state = QcReviewState.APPROVED
    add_run_event(service, payload={"qc": "approved"})
    approved = service.automation.reconcile_waiting_run(task.task_id)

    assert approved.readiness is RunReadiness.WAITING_DEPENDENCY
    assert approved.task.status is TaskStatus.WAITING_DEPENDENCY
    assert approved.authoritative is not None
    assert approved.authoritative.latest_qc_review_state == QcReviewState.APPROVED.value
    assert approved.event_receipts[0].decision is EventConsumptionDecision.CONSUME


def test_succeeded_run_with_approved_qc_waits_for_artifact_dependency(service) -> None:
    task = make_waiting_task()
    task = task.model_copy(
        update={
            "authority_refs": task.authority_refs.model_copy(
                update={"artifact_ids": ("artifact-a",)}
            )
        }
    )
    service.automation.create_task(task)
    bind_authoritative_read_doubles(
        service,
        AuthoritativeReadDoubles(
            run_state=WorkflowState.SUCCEEDED,
            qc_status="approved",
            review_state=QcReviewState.APPROVED,
        ),
    )
    add_run_event(service)

    result = service.automation.reconcile_waiting_run(task.task_id)

    assert result.readiness is RunReadiness.WAITING_DEPENDENCY
    assert result.task.status is TaskStatus.WAITING_DEPENDENCY
    assert result.task.waiting_condition is not None
    assert result.task.waiting_condition.kind is WaitingKind.DEPENDENCY
    assert result.authoritative is not None
    assert result.authoritative.registered_artifact_ids == ("artifact-a",)
    assert result.authoritative.resolved_referenced_artifact_ids == ("artifact-a",)
    assert result.authoritative.unresolved_referenced_artifact_ids == ()
    assert "没有目标级" in result.reason


def test_upstream_preprocessing_artifact_reference_is_not_compared_to_current_run_outputs(
    service,
) -> None:
    task = make_waiting_task()
    upstream_artifact = SimpleNamespace(
        artifact_id="upstream-alff",
        project_id="project-1",
        run_id="preprocessing-run",
        artifact_type="alff_map",
    )
    task = task.model_copy(
        update={
            "authority_refs": task.authority_refs.model_copy(
                update={"artifact_ids": (upstream_artifact.artifact_id,)}
            )
        }
    )
    service.automation.create_task(task)
    reads = AuthoritativeReadDoubles(
        run_state=WorkflowState.SUCCEEDED,
        qc_status="approved",
        review_state=QcReviewState.APPROVED,
        artifact_ids=(),
        referenced_artifacts=(upstream_artifact,),
    )
    bind_authoritative_read_doubles(service, reads)
    add_run_event(service)

    result = service.automation.reconcile_waiting_run(task.task_id)

    assert result.readiness is RunReadiness.WAITING_DEPENDENCY
    assert result.authoritative is not None
    assert result.authoritative.registered_artifact_ids == ()
    assert result.authoritative.resolved_referenced_artifact_ids == ("upstream-alff",)
    assert result.authoritative.unresolved_referenced_artifact_ids == ()


def test_reconciliation_rejects_event_for_another_run_before_advancing_task(service) -> None:
    task = make_waiting_task()
    service.automation.create_task(task)
    wrong_run_event = SimpleNamespace(
        event_id=501,
        run_id="unrelated-run",
        event_type="WorkflowTransitioned",
        payload={"state": "succeeded"},
    )
    service.automation._event_repository = SimpleNamespace(
        list_events=lambda **_kwargs: [wrong_run_event]
    )

    with pytest.raises(InputValidationError) as error:
        service.automation.reconcile_waiting_run(task.task_id)

    assert error.value.code == "workflow_event_run_mismatch"
    stored = service.automation.get_task(task.task_id)
    assert stored.version == task.version
    assert stored.event_cursors == ()
