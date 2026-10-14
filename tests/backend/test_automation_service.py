from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from neuroagent.application.automation.models import (
    AuthorityReferences,
    EventConsumptionDecision,
    RunReadiness,
    TaskBudget,
    TaskStatus,
    TaskStep,
    TaskStepStatus,
    WaitingCondition,
    WaitingKind,
    WorkEventIdentity,
    WorkGoal,
    WorkTask,
)
from neuroagent.application.automation.service import WorkTaskApplicationService
from neuroagent.application.contracts.conversations import ConversationMode
from neuroagent.application.contracts.runs import QcReviewState
from neuroagent.application.contracts.statistics import (
    StatisticalResultDetailView,
    StatisticalResultView,
)
from neuroagent.application.errors import ConflictError, InputValidationError, NotFoundError
from neuroagent.application.hashing import content_hash
from neuroagent.application.reporting import build_statistical_reproducibility_report
from neuroagent.bootstrap import build_service
from neuroagent.domain.execution import QcExecutionMode
from neuroagent.domain.fmri import (
    AnalysisImage,
    FdrCorrection,
    MissingValuePolicy,
    RegisteredArtifactMetadata,
    StatisticalArtifactRole,
    StatisticalDesignRevision,
    StatisticalMapType,
    StatisticalResultManifest,
    StatisticalResultMode,
    StatisticalTest,
    Tail,
)
from neuroagent.domain.lifecycle import PlanState, WorkflowState


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


def test_due_task_reconciliation_scans_without_event_and_rechecks_current_run(service) -> None:
    task = make_waiting_task()
    service.automation.create_task(task)
    reads = AuthoritativeReadDoubles(
        run_state=WorkflowState.RUNNING,
        qc_status="pending",
    )
    bind_authoritative_read_doubles(service, reads)

    first = service.automation.reconcile_next_due_task(
        "automation-worker",
        lease_seconds=10,
        cooldown_seconds=0,
    )
    assert first is not None
    assert first.readiness is RunReadiness.WAITING_RUN
    assert first.task.status is TaskStatus.WAITING_RUN
    assert first.event_receipts == ()

    reads.run.state = WorkflowState.FAILED_TERMINAL
    recovered = service.automation.reconcile_next_due_task(
        "automation-worker",
        lease_seconds=10,
        cooldown_seconds=0,
    )
    assert recovered is not None
    assert recovered.readiness is RunReadiness.BLOCKED
    assert recovered.authoritative is not None
    assert recovered.authoritative.run_state == WorkflowState.FAILED_TERMINAL.value
    assert recovered.task.status is TaskStatus.BLOCKED
    assert recovered.event_receipts == ()


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


def _statistical_design(revision_id: str = "design-1") -> StatisticalDesignRevision:
    subjects = ("sub-01", "sub-02", "sub-03")
    return StatisticalDesignRevision(
        revision_id=revision_id,
        test=StatisticalTest.ONE_SAMPLE_T,
        subject_order=subjects,
        images=tuple(
            AnalysisImage(
                subject_id=subject,
                artifact_id=f"metric-{subject}",
                group=None,
                condition=None,
            )
            for subject in subjects
        ),
        group_order=(),
        condition_order=(),
        covariates=(),
        contrast=(1.0,),
        one_sample_baseline=0.0,
        mask_artifact_id="mask-1",
        tail=Tail.TWO_SIDED,
        missing_value_policy=MissingValuePolicy.ERROR,
        qc_review_revision_id="qc-1",
        qc_review_hash="b" * 64,
    )


def _statistical_plan(
    *,
    design: StatisticalDesignRevision,
    plan_revision_id: str = "stat-plan-1",
    proposal_hash: str = "c" * 64,
    draft_hash: str = "d" * 64,
    state: PlanState = PlanState.APPROVED,
) -> SimpleNamespace:
    plan_payload = {
        "kind": "statistical_design",
        "design": design.model_dump(mode="json"),
        "work_context": {
            "conversation_id": "conversation-statistics",
            "proposal_hash": proposal_hash,
            "draft_hash": draft_hash,
        },
    }
    manifest_hash = "e" * 64
    environment_hash = "f" * 64
    return SimpleNamespace(
        plan_revision_id=plan_revision_id,
        project_id="project-1",
        plan_hash=content_hash(
            {
                "plan": plan_payload,
                "manifest_hash": manifest_hash,
                "environment_hash": environment_hash,
            }
        ),
        manifest_hash=manifest_hash,
        environment_hash=environment_hash,
        state=state,
        plan=plan_payload,
    )


def _statistical_task(
    *,
    status: TaskStatus = TaskStatus.WAITING_RUN,
    run_id: str = "run-statistics",
    plan_revision_id: str = "stat-plan-1",
) -> WorkTask:
    proposal_hash = "c" * 64
    draft_hash = "d" * 64
    task = WorkTask(
        task_id=WorkTaskApplicationService.analysis_proposal_task_id(
            "conversation-statistics", proposal_hash
        ),
        goal=WorkGoal(
            description="Run the approved statistical analysis",
            completion_conditions=("verify a real StatisticalResult",),
        ),
        status=status,
        waiting_condition=WaitingCondition(
            kind=(
                WaitingKind.DEPENDENCY
                if status is TaskStatus.WAITING_DEPENDENCY
                else WaitingKind.WORKFLOW_RUN
            ),
            reference=(
                f"statistical-result:{run_id}"
                if status is TaskStatus.WAITING_DEPENDENCY
                else run_id
            ),
            reason="waiting for statistical results",
        ),
        authority_refs=AuthorityReferences(
            conversation_id="conversation-statistics",
            plan_revision_id=plan_revision_id,
            workflow_run_id=run_id,
            input_revision=draft_hash,
        ),
        steps=(
            TaskStep(
                step_id="statistical-proposal",
                sequence=0,
                kind="statistical_run",
                status=TaskStepStatus.WAITING,
                input_version=proposal_hash,
                authority_refs=AuthorityReferences(
                    conversation_id="conversation-statistics",
                    plan_revision_id=plan_revision_id,
                    workflow_run_id=run_id,
                    input_revision=draft_hash,
                ),
            ),
        ),
        budget=TaskBudget(
            max_steps=10,
            max_model_calls=4,
            max_tokens=5000,
            max_elapsed_seconds=1200,
        ),
    )
    return task


def _real_zero_result(
    *,
    plan: SimpleNamespace,
    design: StatisticalDesignRevision,
    mode: StatisticalResultMode = StatisticalResultMode.REAL,
    plan_hash_in_report: str | None = None,
) -> tuple[StatisticalResultView, StatisticalResultDetailView, list[SimpleNamespace]]:
    correction = FdrCorrection(
        method="fdr",
        q_threshold=0.05,
        mask_artifact_id="mask-1",
        statistic_type=StatisticalMapType.T,
        df1=2,
        df2=None,
    )
    synthetic = mode is StatisticalResultMode.SYNTHETIC_NON_SCIENTIFIC
    artifacts = tuple(
        RegisteredArtifactMetadata(
            artifact_id=f"stat-artifact-{role.value}",
            role=role,
            artifact_type=(
                f"synthetic.non_scientific.{role.value}"
                if synthetic
                else f"statistics.{role.value}"
            ),
            relative_path=f"output/{role.value}.json",
            checksum_sha256=None if synthetic else hashlib.sha256(role.value.encode()).hexdigest(),
            size_bytes=None if synthetic else len(role.value) + 1,
            provenance_hash=None if synthetic else content_hash({"artifact_role": role.value}),
            placeholder=synthetic,
        )
        for role in (
            StatisticalArtifactRole.DESIGN_MATRIX,
            StatisticalArtifactRole.CONTRAST,
            StatisticalArtifactRole.UNCORRECTED_STATISTICAL_MAP,
            StatisticalArtifactRole.CORRECTED_STATISTICAL_MAP,
            StatisticalArtifactRole.EFFECT_MAP,
            StatisticalArtifactRole.CLUSTER_TABLE,
            StatisticalArtifactRole.EXECUTION_LOG,
            StatisticalArtifactRole.SOFTWARE_VERSION_EVIDENCE,
        )
    )
    manifest = StatisticalResultManifest(
        result_id="result-statistics",
        run_id="run-statistics",
        design_revision_id=design.revision_id,
        mode=mode,
        non_scientific=synthetic,
        non_scientific_reason="test-only synthetic result" if synthetic else None,
        correction=correction,
        cluster_connectivity_definition="26-neighbour connectivity",
        artifacts=artifacts,
        clusters=(),
    )
    report = build_statistical_reproducibility_report(
        manifest=manifest,
        design=design,
        correction=correction,
        qc_review_hash=design.qc_review_hash,
        environment_hash="f" * 64,
        plan_hash=plan_hash_in_report or plan.plan_hash,
    )
    report_json = report.json_text
    bundle_hash = report.bundle_hash
    if plan_hash_in_report is not None:
        payload = json.loads(report_json)
        payload["frozen_bindings"]["plan_hash"] = plan_hash_in_report
        report_json = json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
        bundle_hash = hashlib.sha256(f"{report_json}\n{report.markdown}".encode()).hexdigest()
    summary = StatisticalResultView(
        result_id=manifest.result_id,
        project_id="project-1",
        run_id="run-statistics",
        design_revision_id=design.revision_id,
        mode=manifest.mode.value,
        non_scientific=manifest.non_scientific,
        non_scientific_reason=manifest.non_scientific_reason,
        bundle_hash=bundle_hash,
        artifact_count=len(manifest.artifacts),
        cluster_count=len(manifest.clusters),
        version=1,
        created_at=datetime.now(UTC),
    )
    detail = StatisticalResultDetailView(
        **summary.model_dump(),
        manifest=manifest.model_dump(mode="json"),
        report_markdown=report.markdown,
        report_json=report_json,
    )
    registered = [
        SimpleNamespace(
            artifact_id=item.artifact_id,
            project_id="project-1",
            run_id="run-statistics",
            artifact_type=item.artifact_type,
            relative_path=item.relative_path,
            checksum=item.checksum_sha256,
            size_bytes=item.size_bytes,
            provenance={"artifact_role": item.role.value},
        )
        for item in manifest.artifacts
        if not item.placeholder
    ]
    return summary, detail, registered


class StatisticalReconciliationReads:
    def __init__(
        self,
        *,
        plan: SimpleNamespace,
        design: StatisticalDesignRevision,
        summaries: list[StatisticalResultView] | None = None,
        details: dict[str, StatisticalResultDetailView] | None = None,
        artifacts: list[SimpleNamespace] | None = None,
        run_state: WorkflowState = WorkflowState.SUCCEEDED,
    ) -> None:
        self.plan = plan
        self.run = SimpleNamespace(
            run_id="run-statistics",
            project_id="project-1",
            plan_revision_id=plan.plan_revision_id,
            state=run_state,
            version=3,
            attempt=1,
            qc_mode=QcExecutionMode.REQUIRE,
            qc_status="pending",
        )
        self.conversation = SimpleNamespace(
            conversation_id="conversation-statistics",
            mode=ConversationMode.WORK,
            project_id="project-1",
        )
        self.summaries = summaries or []
        self.details = details or {}
        self.artifacts = artifacts or []
        self.design = design
        self.run_reads = 0

    def get_run(self, run_id: str):
        assert run_id == self.run.run_id
        self.run_reads += 1
        return SimpleNamespace(**vars(self.run))

    def get_plan(self, plan_revision_id: str):
        assert plan_revision_id == self.plan.plan_revision_id
        return self.plan

    def get_conversation(self, conversation_id: str):
        assert conversation_id == self.conversation.conversation_id
        return self.conversation

    def list_artifacts(self, run_id: str):
        assert run_id == self.run.run_id
        return list(self.artifacts)

    def list_statistical_results(self, *, project_id: str, run_id: str | None = None):
        assert project_id == self.run.project_id
        assert run_id == self.run.run_id
        return list(self.summaries)

    def get_statistical_result(self, result_id: str):
        return self.details[result_id]


def bind_statistical_read_doubles(service, reads: StatisticalReconciliationReads) -> None:
    service.automation._run_repository = reads
    service.automation._plan_repository = reads
    service.automation._conversation_repository = reads
    service.automation._artifact_repository = reads
    service.automation._qc_repository = SimpleNamespace(
        get_latest_qc_review_for_run=lambda _run_id: pytest.fail(
            "statistical reconciliation must not inspect head-motion QC"
        ),
        get_qc_skip_for_run=lambda _run_id: pytest.fail(
            "statistical reconciliation must not inspect head-motion QC"
        ),
    )
    service.automation._event_repository = SimpleNamespace(list_events=lambda **_kwargs: [])


def test_statistical_success_ignores_pending_head_motion_qc_and_accepts_zero_significance(
    service,
) -> None:
    design = _statistical_design()
    plan = _statistical_plan(design=design)
    summary, detail, artifacts = _real_zero_result(plan=plan, design=design)
    reads = StatisticalReconciliationReads(
        plan=plan,
        design=design,
        summaries=[summary],
        details={summary.result_id: detail},
        artifacts=artifacts,
    )
    bind_statistical_read_doubles(service, reads)
    service.automation.create_task(_statistical_task())

    result = service.automation.reconcile_waiting_run(
        WorkTaskApplicationService.analysis_proposal_task_id("conversation-statistics", "c" * 64)
    )

    assert result.readiness is RunReadiness.COMPLETED
    assert result.task.status is TaskStatus.COMPLETED
    assert result.task.steps[0].status is TaskStepStatus.SUCCEEDED
    assert result.task.authority_refs.artifact_ids == tuple(
        sorted(item.artifact_id for item in artifacts)
    )
    assert result.authoritative is not None
    assert result.authoritative.qc_status == "pending"
    assert result.authoritative.verified_statistical_result_id == summary.result_id
    assert reads.run_reads == 2


def test_succeeded_statistical_run_without_result_waits_for_dependency_stably(service) -> None:
    design = _statistical_design()
    plan = _statistical_plan(design=design)
    reads = StatisticalReconciliationReads(plan=plan, design=design)
    bind_statistical_read_doubles(service, reads)
    task = _statistical_task()
    service.automation.create_task(task)

    first = service.automation.reconcile_waiting_run(task.task_id)
    second = service.automation.reconcile_waiting_run(task.task_id)

    assert first.readiness is RunReadiness.WAITING_DEPENDENCY
    assert first.task.status is TaskStatus.WAITING_DEPENDENCY
    assert first.task.waiting_condition is not None
    assert first.task.waiting_condition.reference == "statistical-result:run-statistics"
    assert second.readiness is RunReadiness.WAITING_DEPENDENCY
    assert second.task.version == first.task.version
    assert second.reason == first.reason


def test_statistical_mock_without_real_result_is_blocked_with_stable_reason(service) -> None:
    design = _statistical_design()
    plan = _statistical_plan(design=design)
    reads = StatisticalReconciliationReads(
        plan=plan,
        design=design,
        artifacts=[
            SimpleNamespace(
                artifact_id="mock-output",
                project_id="project-1",
                run_id="run-statistics",
                artifact_type="mock.result",
            )
        ],
    )
    bind_statistical_read_doubles(service, reads)
    task = _statistical_task()
    service.automation.create_task(task)

    first = service.automation.reconcile_waiting_run(task.task_id)

    assert first.readiness is RunReadiness.BLOCKED
    assert first.task.status is TaskStatus.BLOCKED
    assert "mock/synthetic" in first.reason


def test_synthetic_statistical_result_cannot_complete_analysis_task(service) -> None:
    design = _statistical_design()
    plan = _statistical_plan(design=design)
    summary, detail, artifacts = _real_zero_result(
        plan=plan,
        design=design,
        mode=StatisticalResultMode.SYNTHETIC_NON_SCIENTIFIC,
    )
    reads = StatisticalReconciliationReads(
        plan=plan,
        design=design,
        summaries=[summary],
        details={summary.result_id: detail},
        artifacts=artifacts,
    )
    bind_statistical_read_doubles(service, reads)
    task = _statistical_task()
    service.automation.create_task(task)

    result = service.automation.reconcile_waiting_run(task.task_id)

    assert result.readiness is RunReadiness.BLOCKED
    assert result.task.status is TaskStatus.BLOCKED
    assert "mock/synthetic" not in result.reason
    assert "无法通过核验" in result.reason


def test_statistical_result_with_wrong_frozen_plan_hash_fails_closed(service) -> None:
    design = _statistical_design()
    plan = _statistical_plan(design=design)
    summary, detail, artifacts = _real_zero_result(
        plan=plan,
        design=design,
        plan_hash_in_report="9" * 64,
    )
    reads = StatisticalReconciliationReads(
        plan=plan,
        design=design,
        summaries=[summary],
        details={summary.result_id: detail},
        artifacts=artifacts,
    )
    bind_statistical_read_doubles(service, reads)
    task = _statistical_task()
    service.automation.create_task(task)

    result = service.automation.reconcile_waiting_run(task.task_id)

    assert result.readiness is RunReadiness.BLOCKED
    assert result.task.status is TaskStatus.BLOCKED
    assert "cannot" not in result.reason
    assert "无法通过核验" in result.reason


@pytest.mark.parametrize("invalid_binding", ["duplicate_result", "run_plan_mismatch"])
def test_statistical_duplicate_result_or_run_plan_mismatch_fails_closed(
    service, invalid_binding
) -> None:
    design = _statistical_design()
    plan = _statistical_plan(design=design)
    summary, detail, artifacts = _real_zero_result(plan=plan, design=design)
    reads = StatisticalReconciliationReads(
        plan=plan,
        design=design,
        summaries=[summary, summary] if invalid_binding == "duplicate_result" else [summary],
        details={summary.result_id: detail},
        artifacts=artifacts,
    )
    if invalid_binding == "run_plan_mismatch":
        reads.run.plan_revision_id = "another-plan"
    bind_statistical_read_doubles(service, reads)
    task = _statistical_task()
    service.automation.create_task(task)

    result = service.automation.reconcile_waiting_run(task.task_id)

    assert result.readiness is RunReadiness.BLOCKED
    assert result.task.status is TaskStatus.BLOCKED


def test_statistical_proposal_plan_and_authorized_run_bind_idempotently(service) -> None:
    design = _statistical_design()
    plan = _statistical_plan(design=design)
    conversation = SimpleNamespace(
        conversation_id="conversation-statistics",
        mode=ConversationMode.WORK,
        project_id="project-1",
    )
    run = SimpleNamespace(
        run_id="run-statistics",
        project_id="project-1",
        plan_revision_id=plan.plan_revision_id,
        state=WorkflowState.QUEUED,
    )
    reads = StatisticalReconciliationReads(plan=plan, design=design, run_state=WorkflowState.QUEUED)
    reads.conversation = conversation
    reads.run = run
    bind_statistical_read_doubles(service, reads)
    task = service.automation.ensure_analysis_proposal_task(
        "conversation-statistics",
        "project-1",
        "c" * 64,
        "Analyze ALFF values across groups",
        "d" * 64,
    )
    reused_task = service.automation.ensure_analysis_proposal_task(
        "conversation-statistics",
        "project-1",
        "c" * 64,
        "Analyze ALFF values across groups",
        "d" * 64,
    )
    assert reused_task.version == task.version
    task = service.automation.bind_statistical_plan(
        task.task_id,
        "conversation-statistics",
        "project-1",
        "c" * 64,
        plan.plan_revision_id,
    )
    reused_plan = service.automation.bind_statistical_plan(
        task.task_id,
        "conversation-statistics",
        "project-1",
        "c" * 64,
        plan.plan_revision_id,
    )
    assert reused_plan.version == task.version
    bound = service.automation.bind_statistical_run(
        task.task_id,
        "conversation-statistics",
        "project-1",
        plan.plan_revision_id,
        run.run_id,
    )
    reused_run = service.automation.bind_statistical_run(
        task.task_id,
        "conversation-statistics",
        "project-1",
        plan.plan_revision_id,
        run.run_id,
    )

    assert task.status is TaskStatus.WAITING_USER
    assert task.steps[0].kind == "statistical_run"
    assert task.steps[0].status is TaskStepStatus.READY
    assert bound.status is TaskStatus.WAITING_RUN
    assert bound.steps[0].status is TaskStepStatus.WAITING
    assert bound.authority_refs.plan_revision_id == plan.plan_revision_id
    assert bound.authority_refs.workflow_run_id == run.run_id
    assert reused_run.version == bound.version


@pytest.mark.parametrize(
    "plan_state",
    [PlanState.DRAFT, PlanState.AWAITING_APPROVAL, PlanState.APPROVED],
)
def test_statistical_plan_binding_accepts_draft_and_keeps_run_approval_gate(
    service, plan_state
) -> None:
    design = _statistical_design()
    plan = _statistical_plan(design=design, state=plan_state)
    reads = StatisticalReconciliationReads(plan=plan, design=design, run_state=WorkflowState.QUEUED)
    bind_statistical_read_doubles(service, reads)
    task = service.automation.ensure_analysis_proposal_task(
        "conversation-statistics",
        "project-1",
        "c" * 64,
        "Analyze ALFF values across groups",
        "d" * 64,
    )

    bound_plan = service.automation.bind_statistical_plan(
        task.task_id,
        "conversation-statistics",
        "project-1",
        "c" * 64,
        plan.plan_revision_id,
    )

    assert bound_plan.status is TaskStatus.WAITING_USER
    assert bound_plan.waiting_condition is not None
    assert bound_plan.waiting_condition.reference == f"plan:{plan.plan_revision_id}"
    assert bound_plan.authority_refs.plan_revision_id == plan.plan_revision_id
    assert bound_plan.steps[0].authority_refs.plan_revision_id == plan.plan_revision_id

    if plan_state is PlanState.DRAFT:
        with pytest.raises(ConflictError) as error:
            service.automation.bind_statistical_run(
                task.task_id,
                "conversation-statistics",
                "project-1",
                plan.plan_revision_id,
                "run-statistics",
            )
        assert error.value.code == "work_task_plan_not_approved"
        assert service.automation.get_task(task.task_id).status is TaskStatus.WAITING_USER
