"""Narrow application use cases for durable user-goal progress."""

# ruff: noqa: RUF001

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable, Mapping
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from neuroagent.application.automation.analysis_outbox import (
    AnalysisOutboxClaim,
    AnalysisOutboxRegistrationSource,
    AnalysisOutboxStatus,
    AnalysisProposalOutbox,
    AnalysisSourceModelFacts,
    VerifiedAnalysisOutboxSource,
)
from neuroagent.application.automation.composite_goal import (
    CompositeGoalIdentity,
    CompositeGoalProgress,
    CompositeGoalSnapshot,
    project_composite_goal,
)
from neuroagent.application.automation.model_budget import (
    CompositeGoalModelBudget,
    CompositeGoalModelCallReserveResult,
    CompositeGoalModelCallSettlement,
)
from neuroagent.application.automation.models import (
    AuthoritativeRunSnapshot,
    EventConsumptionDecision,
    EventReconciliationReceipt,
    RunReadiness,
    TaskBudget,
    TaskStatus,
    TaskStep,
    TaskStepStatus,
    WaitingCondition,
    WaitingKind,
    WorkEventIdentity,
    WorkflowRunReconciliation,
    WorkGoal,
    WorkTask,
    transition_task,
    transition_task_step,
)
from neuroagent.application.automation.ports import WorkTaskRepositoryPort
from neuroagent.application.automation.result_interpretation import (
    ResultInterpretationClaim,
    ResultInterpretationStatus,
    StatisticalResultInterpretationOutbox,
    VerifiedResultInterpretationSource,
    project_result_model_facts,
)
from neuroagent.application.automation.step_lifecycle import (
    bind_prepared_step_plan_for_review,
    bind_step_to_run,
    complete_step,
    resolve_linear_step_sequence,
    resolve_routed_workflow_run,
    step_plan_review_reference,
)
from neuroagent.application.automation.step_preparation import (
    StepPreparationClaim,
    resolve_step_preparation_target,
)
from neuroagent.application.automation.step_readiness import (
    StepReadinessAssessment,
    StepReadinessEvidenceSummary,
    StepReadinessOutcome,
    StepReadinessRegistry,
    StepReadinessResultKind,
    StepReadinessStepKind,
)
from neuroagent.application.contracts.automation import StepPlanReviewView
from neuroagent.application.contracts.conversations import ConversationMode
from neuroagent.application.contracts.runs import ArtifactView
from neuroagent.application.contracts.statistics import (
    StatisticalResultDetailView,
    StatisticalResultView,
)
from neuroagent.application.errors import (
    ConflictError,
    InputValidationError,
    NotFoundError,
)
from neuroagent.application.hashing import content_hash
from neuroagent.application.repository_ports import (
    ArtifactCatalogRepositoryPort,
    ConversationRepositoryPort,
    EventRepositoryPort,
    PlanRepositoryPort,
    ProjectRepositoryPort,
    QcRepositoryPort,
    RunRepositoryPort,
)
from neuroagent.application.statistical_result_facts import (
    render_result_readout,
    result_model_facts,
    verified_result_design,
)
from neuroagent.domain.fmri.artifacts import ArtifactKind, ArtifactLineage
from neuroagent.domain.fmri.metrics import (
    AlffFalffParameters,
    MetricMaskSource,
    RehoParameters,
)
from neuroagent.domain.fmri.preprocessing import PreprocessingParameters
from neuroagent.domain.fmri.preprocessing_outputs import (
    DpabiPreprocessingOutput,
    enumerate_dpabi_preprocessing_outputs,
)
from neuroagent.domain.fmri.results import StatisticalResultMode
from neuroagent.domain.fmri.statistics import StatisticalDesignRevision
from neuroagent.domain.lifecycle import PlanState, WorkflowState
from neuroagent.skills.models import SkillPlan, stable_hash


class _InvalidStatisticalAuthority(ValueError):
    """Raised when a stored statistical task/result chain cannot be trusted."""


class _InvalidPreprocessingAuthority(ValueError):
    """Raised when a frozen preprocessing task or its artifacts cannot be trusted."""


class _StableRunAuthority:
    """Run-bracketed workflow, QC and artifact records for one reconciliation pass."""

    def __init__(
        self,
        snapshot: AuthoritativeRunSnapshot,
        *,
        project_id: str,
        plan_revision_id: str | None,
        artifacts: tuple[ArtifactView, ...],
    ) -> None:
        self.snapshot = snapshot
        self.project_id = project_id
        self.plan_revision_id = plan_revision_id
        self.artifacts = artifacts


class _StatisticalReadinessSnapshot(BaseModel):
    """Minimal immutable identities needed to bind assessment to finalization.

    The existing public authority snapshot stays unchanged. These extra fields
    carry only the frozen Plan/input and verified result fingerprints required
    by the readiness contract; they intentionally omit result payloads/paths.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    authoritative: AuthoritativeRunSnapshot
    plan_revision_id: str | None = Field(default=None, min_length=1, max_length=100)
    frozen_input_fingerprint: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    result_bundle_fingerprint: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    result_version: int | None = Field(default=None, ge=1)
    result_lineage_fingerprint: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")


class _StatisticalReadinessAssessment(StepReadinessAssessment):
    """Readiness contract plus its internal read-only snapshot for coordination."""

    stable_snapshot: _StatisticalReadinessSnapshot | None = None


class _StatisticalFinalizationEvidence(BaseModel):
    """Freshly re-read statistical outbox identity plus verified lineage hash."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    outbox: StatisticalResultInterpretationOutbox
    lineage_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")


class _PreprocessingCompletionEvidence(BaseModel):
    """Read-only, verified identities needed to complete one preprocessing step."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    plan_revision_id: str = Field(min_length=1, max_length=100)
    manifest_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    expected_artifacts: tuple[DpabiPreprocessingOutput, ...] = Field(min_length=1)
    verified_artifact_ids: tuple[str, ...] = Field(min_length=1)
    target_artifact_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    lineage_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    qc_decision_kind: str = Field(pattern=r"^(approved|skipped)$")
    qc_decision_identity: str = Field(min_length=1, max_length=100)


class _PreprocessingReadinessAssessment(StepReadinessAssessment):
    """Readiness contract plus authority used only by the coordinator."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    stable_authority: _StableRunAuthority | None = None
    completion_evidence: _PreprocessingCompletionEvidence | None = None


class _StatisticalRunReadinessHandler:
    """Classify one stable statistical authority read without mutating state."""

    kind = "statistical_run"

    def __init__(
        self,
        reader: Callable[[WorkTask, str], tuple[_StatisticalReadinessSnapshot | None, str | None]],
    ) -> None:
        self._reader = reader

    def inspect(
        self, task: WorkTask, step: TaskStep, run_id: str
    ) -> _StatisticalReadinessAssessment:
        if step.kind != self.kind:
            return self._assessment(
                task,
                step,
                run_id,
                StepReadinessOutcome.INDETERMINATE,
                "statistical_step_kind_mismatch",
                "The requested step is not a statistical Run step.",
            )
        try:
            snapshot, evidence_error = self._reader(task, run_id)
        except NotFoundError:
            return self._assessment(
                task,
                step,
                run_id,
                StepReadinessOutcome.INDETERMINATE,
                "statistical_authority_unavailable",
                "绑定的统计 Run 暂不可读取；任务状态未推进。",
            )
        except _InvalidStatisticalAuthority:
            return self._assessment(
                task,
                step,
                run_id,
                StepReadinessOutcome.BLOCKED,
                "statistical_authority_invalid",
                "统计 Run 或其冻结任务绑定未通过权威核验；任务已阻断。",
            )

        if snapshot is None:
            return self._assessment(
                task,
                step,
                run_id,
                StepReadinessOutcome.INDETERMINATE,
                "statistical_run_unstable",
                "统计 Run 在三次权威状态读取期间都发生版本变化；保留原状态后重试。",
            )
        authoritative = snapshot.authoritative
        if evidence_error is not None:
            return self._assessment(
                task,
                step,
                run_id,
                StepReadinessOutcome.BLOCKED,
                "statistical_evidence_invalid",
                "统计结果或冻结方案证据无法通过核验；任务已阻断。",
                stable_snapshot=snapshot,
            )

        try:
            state = WorkflowState(authoritative.run_state)
        except ValueError:
            return self._assessment(
                task,
                step,
                run_id,
                StepReadinessOutcome.INDETERMINATE,
                "statistical_run_state_unknown",
                "统计 Workflow 返回了不受支持的状态；任务状态未推进。",
                stable_snapshot=snapshot,
            )
        if state in {WorkflowState.QUEUED, WorkflowState.RUNNING, WorkflowState.CANCELLING}:
            return self._assessment(
                task,
                step,
                run_id,
                StepReadinessOutcome.WAITING_RUN,
                "statistical_run_active",
                "权威统计 Workflow 状态仍在排队、运行或取消中。",
                stable_snapshot=snapshot,
                waiting_condition=WaitingCondition(
                    kind=WaitingKind.WORKFLOW_RUN,
                    reference=run_id,
                    reason="等待该统计 Workflow Run 进入可核验的终态。",
                ),
            )
        if state is WorkflowState.SUCCEEDED:
            if authoritative.statistical_result_ids:
                if authoritative.verified_statistical_result_id is None:
                    return self._assessment(
                        task,
                        step,
                        run_id,
                        StepReadinessOutcome.BLOCKED,
                        "statistical_result_unverified",
                        "统计 Run 的结果记录未通过真实科学结果核验；任务已阻断。",
                        stable_snapshot=snapshot,
                    )
                try:
                    evidence = self._evidence_summary(snapshot, authoritative)
                except (ValidationError, ValueError):
                    return self._assessment(
                        task,
                        step,
                        run_id,
                        StepReadinessOutcome.INDETERMINATE,
                        "statistical_readiness_evidence_unavailable",
                        "真实统计结果的 readiness 证据摘要不完整；任务状态未推进。",
                        stable_snapshot=snapshot,
                    )
                return self._assessment(
                    task,
                    step,
                    run_id,
                    StepReadinessOutcome.READY_TO_FINALIZE,
                    "statistical_result_ready_to_finalize",
                    "唯一真实统计结果及其 Artifact 已通过核验，可以进入完成收口。",
                    stable_snapshot=snapshot,
                    evidence=evidence,
                )
            if "mock.result" in authoritative.registered_artifact_types or any(
                artifact_type.startswith("synthetic.")
                for artifact_type in authoritative.registered_artifact_types
            ):
                return self._assessment(
                    task,
                    step,
                    run_id,
                    StepReadinessOutcome.BLOCKED,
                    "statistical_mock_result_missing",
                    "统计 Run 是 mock/synthetic 执行且没有真实 StatisticalResult；"
                    "不能作为科学分析完成。",
                    stable_snapshot=snapshot,
                )
            return self._assessment(
                task,
                step,
                run_id,
                StepReadinessOutcome.WAITING_DEPENDENCY,
                "statistical_result_pending",
                "统计 Run 已成功但尚无 StatisticalResult；保留 waiting_dependency 并等待结果登记。",
                stable_snapshot=snapshot,
                waiting_condition=WaitingCondition(
                    kind=WaitingKind.DEPENDENCY,
                    reference=f"statistical-result:{run_id}",
                    reason="统计 Run 已成功，等待唯一真实 StatisticalResult 完成登记并通过核验。",
                ),
            )
        if state in {WorkflowState.FAILED_RETRYABLE, WorkflowState.TIMED_OUT}:
            if task.status is TaskStatus.WAITING_RUN:
                return self._assessment(
                    task,
                    step,
                    run_id,
                    StepReadinessOutcome.WAITING_RUN,
                    "statistical_run_retrying",
                    f"统计 Workflow 暂处于 {state.value}；继续等待同一 Run 的处理。",
                    stable_snapshot=snapshot,
                    waiting_condition=WaitingCondition(
                        kind=WaitingKind.WORKFLOW_RUN,
                        reference=run_id,
                        reason="Worker 正在按原有预算处理同一统计 Run。",
                    ),
                )
            return self._assessment(
                task,
                step,
                run_id,
                StepReadinessOutcome.BLOCKED,
                "statistical_run_state_inconsistent",
                "等待统计结果时 Run 又回到非终态，任务与权威 Workflow 状态不一致。",
                stable_snapshot=snapshot,
            )
        return self._assessment(
            task,
            step,
            run_id,
            StepReadinessOutcome.BLOCKED,
            "statistical_run_terminal",
            f"统计 Workflow 已处于 {state.value}；本任务不会自动重试或创建新 Run。",
            stable_snapshot=snapshot,
        )

    @staticmethod
    def _assessment(
        task: WorkTask,
        step: TaskStep,
        run_id: str,
        outcome: StepReadinessOutcome,
        reason_code: str,
        reason: str,
        *,
        stable_snapshot: _StatisticalReadinessSnapshot | None = None,
        waiting_condition: WaitingCondition | None = None,
        evidence: StepReadinessEvidenceSummary | None = None,
    ) -> _StatisticalReadinessAssessment:
        return _StatisticalReadinessAssessment(
            task_id=task.task_id,
            task_version=task.version,
            step_id=step.step_id,
            step_version=step.version,
            step_kind=step.kind,
            run_id=run_id,
            outcome=outcome,
            reason_code=reason_code,
            reason=reason,
            waiting_condition=waiting_condition,
            evidence=evidence,
            stable_snapshot=stable_snapshot,
        )

    @staticmethod
    def _evidence_summary(
        snapshot: _StatisticalReadinessSnapshot,
        authoritative: AuthoritativeRunSnapshot,
    ) -> StepReadinessEvidenceSummary:
        if (
            snapshot.plan_revision_id is None
            or snapshot.frozen_input_fingerprint is None
            or snapshot.result_bundle_fingerprint is None
            or snapshot.result_version is None
            or snapshot.result_lineage_fingerprint is None
            or authoritative.verified_statistical_result_id is None
        ):
            raise ValueError("verified statistical readiness identities are incomplete")
        return StepReadinessEvidenceSummary(
            step_kind=StepReadinessStepKind.STATISTICAL_RUN,
            run_version=authoritative.run_version,
            plan_revision_id=snapshot.plan_revision_id,
            frozen_input_fingerprint=snapshot.frozen_input_fingerprint,
            result_kind=StepReadinessResultKind.STATISTICAL_RESULT,
            result_identity=authoritative.verified_statistical_result_id,
            lineage_fingerprint=snapshot.result_lineage_fingerprint,
            result_version=snapshot.result_version,
            result_bundle_fingerprint=snapshot.result_bundle_fingerprint,
        )


class _PreprocessingRunReadinessHandler:
    """Classify one stable preprocessing Run and its frozen output lineage."""

    kind = "preprocessing_run"

    def __init__(
        self,
        reader: Callable[[WorkTask, str], _StableRunAuthority | None],
        completion_reader: Callable[
            [WorkTask, str, _StableRunAuthority], _PreprocessingCompletionEvidence
        ],
    ) -> None:
        self._reader = reader
        self._completion_reader = completion_reader

    def inspect(
        self, task: WorkTask, step: TaskStep, run_id: str
    ) -> _PreprocessingReadinessAssessment:
        if step.kind != self.kind:
            return self._assessment(
                task,
                step,
                run_id,
                StepReadinessOutcome.INDETERMINATE,
                "preprocessing_step_kind_mismatch",
                "The requested step is not a preprocessing Run step.",
            )
        try:
            authority = self._reader(task, run_id)
        except NotFoundError:
            return self._assessment(
                task,
                step,
                run_id,
                StepReadinessOutcome.INDETERMINATE,
                "preprocessing_authority_unavailable",
                "绑定的预处理 Run 暂不可读取；任务状态未推进。",
            )
        except InputValidationError:
            return self._assessment(
                task,
                step,
                run_id,
                StepReadinessOutcome.INDETERMINATE,
                "preprocessing_authority_unverifiable",
                "预处理权威记录的身份绑定无法核实；任务状态未推进。",
            )

        if authority is None:
            return self._assessment(
                task,
                step,
                run_id,
                StepReadinessOutcome.INDETERMINATE,
                "preprocessing_run_unstable",
                "预处理 Run 在稳定读取期间持续变化；保留原状态后重试。",
            )
        snapshot = authority.snapshot
        if snapshot.unresolved_referenced_artifact_ids:
            return self._assessment(
                task,
                step,
                run_id,
                StepReadinessOutcome.BLOCKED,
                "preprocessing_referenced_artifact_missing",
                "任务引用的 Artifact 不存在或不属于该项目；需先核验引用。",
                stable_authority=authority,
            )

        try:
            state = WorkflowState(snapshot.run_state)
        except ValueError:
            return self._assessment(
                task,
                step,
                run_id,
                StepReadinessOutcome.INDETERMINATE,
                "preprocessing_run_state_unknown",
                "预处理 Workflow 返回了不受支持的状态；任务状态未推进。",
                stable_authority=authority,
            )
        if state in {WorkflowState.QUEUED, WorkflowState.RUNNING, WorkflowState.CANCELLING}:
            return self._waiting_run(task, step, run_id, authority)
        if state is WorkflowState.QC_REVIEW:
            actionable = (
                snapshot.qc_status in {"pending", "awaiting_review"}
                and snapshot.latest_qc_review_state in {None, "draft"}
            ) or (snapshot.qc_status == "failed" and snapshot.latest_qc_review_state == "draft")
            if actionable:
                return self._waiting_user(
                    task,
                    step,
                    run_id,
                    authority,
                    "Workflow 已到达 QC 决策边界；需要用户查看并明确处理 QC。",
                    "QC 尚未明确批准或记录显式跳过；本次未创建或批准 QC。",
                )
            return self._assessment(
                task,
                step,
                run_id,
                StepReadinessOutcome.BLOCKED,
                "preprocessing_qc_state_inconsistent",
                "Workflow 处于 QC_REVIEW，但 QC 状态或审查记录不一致。",
                stable_authority=authority,
            )
        if state is WorkflowState.SUCCEEDED:
            qc_approved = (
                snapshot.qc_status == "approved" and snapshot.latest_qc_review_state == "approved"
            )
            qc_skipped = snapshot.qc_status == "skipped" and snapshot.qc_skip_id is not None
            if not (qc_approved or qc_skipped) and (
                snapshot.qc_status == "rejected" or snapshot.latest_qc_review_state == "rejected"
            ):
                return self._assessment(
                    task,
                    step,
                    run_id,
                    StepReadinessOutcome.BLOCKED,
                    "preprocessing_qc_rejected",
                    "权威 QC 记录为 rejected；任务已阻断，需用户决定后续处理。",
                    stable_authority=authority,
                )
            if (
                not (qc_approved or qc_skipped)
                and snapshot.qc_status == "pending"
                and snapshot.latest_qc_review_state
                in {
                    None,
                    "draft",
                }
            ):
                return self._waiting_user(
                    task,
                    step,
                    run_id,
                    authority,
                    "运行已成功但 QC 仍 pending；等待用户检查或明确处理。",
                    "运行成功但 QC 仍 pending；本次不会隐含批准或跳过。",
                )
            if not (qc_approved or qc_skipped):
                return self._assessment(
                    task,
                    step,
                    run_id,
                    StepReadinessOutcome.BLOCKED,
                    "preprocessing_qc_state_inconsistent",
                    "运行成功，但 QC 状态与审查或跳过记录不一致；不能安全继续。",
                    stable_authority=authority,
                )

            try:
                completion_evidence = self._completion_reader(task, run_id, authority)
                evidence = StepReadinessEvidenceSummary(
                    step_kind=StepReadinessStepKind.PREPROCESSING_RUN,
                    run_version=snapshot.run_version,
                    plan_revision_id=completion_evidence.plan_revision_id,
                    frozen_input_fingerprint=completion_evidence.manifest_hash,
                    result_kind=StepReadinessResultKind.PREPROCESSING_ARTIFACTS,
                    result_identity=completion_evidence.target_artifact_fingerprint,
                    lineage_fingerprint=completion_evidence.lineage_fingerprint,
                )
            except NotFoundError:
                return self._assessment(
                    task,
                    step,
                    run_id,
                    StepReadinessOutcome.BLOCKED,
                    "preprocessing_evidence_invalid",
                    "预处理任务引用的冻结对象或目标 Artifact 不存在；任务已阻断。",
                    stable_authority=authority,
                )
            except (
                ConflictError,
                InputValidationError,
                ValueError,
                TypeError,
                KeyError,
                AttributeError,
                ValidationError,
            ):
                return self._assessment(
                    task,
                    step,
                    run_id,
                    StepReadinessOutcome.BLOCKED,
                    "preprocessing_evidence_invalid",
                    "预处理目标产物或冻结权威链无法通过核验；任务已阻断。",
                    stable_authority=authority,
                )
            return self._assessment(
                task,
                step,
                run_id,
                StepReadinessOutcome.READY_TO_FINALIZE,
                "preprocessing_artifacts_ready_to_finalize",
                "冻结预处理 Plan、QC 决策及所有精确目标 Artifact 均已核验，可以进入完成收口。",
                stable_authority=authority,
                completion_evidence=completion_evidence,
                evidence=evidence,
            )
        if state in {WorkflowState.FAILED_RETRYABLE, WorkflowState.TIMED_OUT}:
            return self._waiting_run(
                task,
                step,
                run_id,
                authority,
                reason=(
                    f"Workflow 暂处于 {state.value}；任务继续等待 Worker 对同一 Run 的重试，"
                    "不创建新 Run。"
                ),
                condition_reason="Worker 正在按现有重试预算处理同一 Workflow Run。",
            )
        if state in {WorkflowState.FAILED_TERMINAL, WorkflowState.CANCELLED}:
            reason = f"权威 Workflow 状态为 {state.value}；本协调器不会创建新 Run 或重试。"
        else:
            reason = "当前 Workflow/QC 组合无法安全判定后续动作；保留任务状态供人工核验。"
        return self._assessment(
            task,
            step,
            run_id,
            StepReadinessOutcome.BLOCKED,
            "preprocessing_run_terminal",
            reason,
            stable_authority=authority,
        )

    def _waiting_run(
        self,
        task: WorkTask,
        step: TaskStep,
        run_id: str,
        authority: _StableRunAuthority,
        *,
        reason: str = "权威 Workflow 状态仍在排队、运行或取消中。",
        condition_reason: str = "等待该 Workflow Run 进入可核验的终态。",
    ) -> _PreprocessingReadinessAssessment:
        return self._assessment(
            task,
            step,
            run_id,
            StepReadinessOutcome.WAITING_RUN,
            "preprocessing_run_active",
            reason,
            stable_authority=authority,
            waiting_condition=WaitingCondition(
                kind=WaitingKind.WORKFLOW_RUN,
                reference=run_id,
                reason=condition_reason,
            ),
        )

    def _waiting_user(
        self,
        task: WorkTask,
        step: TaskStep,
        run_id: str,
        authority: _StableRunAuthority,
        condition_reason: str,
        reason: str,
    ) -> _PreprocessingReadinessAssessment:
        return self._assessment(
            task,
            step,
            run_id,
            StepReadinessOutcome.WAITING_USER,
            "preprocessing_qc_waiting_user",
            reason,
            stable_authority=authority,
            waiting_condition=WaitingCondition(
                kind=WaitingKind.USER_DECISION,
                reference=f"qc:{run_id}",
                reason=condition_reason,
            ),
        )

    @staticmethod
    def _assessment(
        task: WorkTask,
        step: TaskStep,
        run_id: str,
        outcome: StepReadinessOutcome,
        reason_code: str,
        reason: str,
        *,
        stable_authority: _StableRunAuthority | None = None,
        completion_evidence: _PreprocessingCompletionEvidence | None = None,
        waiting_condition: WaitingCondition | None = None,
        evidence: StepReadinessEvidenceSummary | None = None,
    ) -> _PreprocessingReadinessAssessment:
        return _PreprocessingReadinessAssessment(
            task_id=task.task_id,
            task_version=task.version,
            step_id=step.step_id,
            step_version=step.version,
            step_kind=step.kind,
            run_id=run_id,
            outcome=outcome,
            reason_code=reason_code,
            reason=reason,
            waiting_condition=waiting_condition,
            evidence=evidence,
            stable_authority=stable_authority,
            completion_evidence=completion_evidence,
        )


class WorkTaskApplicationService:
    """Persist task intent and event receipts without owning scientific run state."""

    def __init__(
        self,
        repository: WorkTaskRepositoryPort,
        *,
        event_repository: EventRepositoryPort,
        run_repository: RunRepositoryPort,
        qc_repository: QcRepositoryPort,
        artifact_repository: ArtifactCatalogRepositoryPort,
        conversation_repository: ConversationRepositoryPort,
        plan_repository: PlanRepositoryPort,
        project_repository: ProjectRepositoryPort,
    ) -> None:
        self._repository = repository
        self._event_repository = event_repository
        self._run_repository = run_repository
        self._qc_repository = qc_repository
        self._artifact_repository = artifact_repository
        self._conversation_repository = conversation_repository
        self._plan_repository = plan_repository
        self._project_repository = project_repository
        self._step_readiness_registry = StepReadinessRegistry(
            (
                _PreprocessingRunReadinessHandler(
                    self._read_stable_authoritative_snapshot,
                    self._read_preprocessing_completion_evidence,
                ),
                _StatisticalRunReadinessHandler(self._read_stable_statistical_snapshot),
            )
        )

    def create_task(self, task: WorkTask) -> WorkTask:
        return self._repository.create_task(task)

    def ensure_analysis_proposal_outbox(
        self,
        *,
        intent: Mapping[str, Any] | None,
        conversation_id: str,
        project_id: str,
        preprocessing_task_id: str,
    ) -> AnalysisProposalOutbox | None:
        """Persist a proposal-only outbox only for the explicit composite intent."""

        if not isinstance(intent, Mapping) or intent.get("task_kind") != "preprocess_then_analyze":
            return None
        intent_id = intent.get("task_id")
        request = intent.get("analysis_request")
        if not isinstance(intent_id, str) or not intent_id or not isinstance(request, str):
            raise InputValidationError(
                "analysis_intent_invalid", "复合分析意图缺少稳定身份或原始请求。"
            )
        if intent.get("project_id") != project_id:
            raise ConflictError(
                "analysis_intent_project_mismatch", "复合分析意图与当前项目不匹配。"
            )
        if intent.get("conversation_id") != conversation_id:
            raise ConflictError(
                "analysis_intent_conversation_mismatch", "复合分析意图与当前会话不匹配。"
            )
        conversation = self._conversation_repository.get_conversation(conversation_id)
        self._require_work_conversation(conversation, conversation_id, project_id)
        task = self._repository.get_task(preprocessing_task_id)
        plan_id = task.authority_refs.plan_revision_id
        run_id = task.authority_refs.workflow_run_id
        prep_intent_id = task.authority_refs.analysis_intent_task_id
        prep_request_hash = task.authority_refs.analysis_request_hash
        if (
            task.authority_refs.conversation_id != conversation_id
            or (prep_intent_id is None) != (prep_request_hash is None)
            or plan_id is None
            or run_id is None
        ):
            raise ConflictError(
                "analysis_outbox_preprocessing_binding_missing",
                "预处理任务尚未绑定冻结方案和运行。",
            )
        plan = self._plan_repository.get_plan(plan_id)
        run = self._run_repository.get_run(run_id)
        if (
            plan.plan_revision_id != plan_id
            or plan.project_id != project_id
            or task.authority_refs.input_revision != plan.manifest_hash
            or run.run_id != run_id
            or run.project_id != project_id
            or run.plan_revision_id != plan_id
        ):
            raise ConflictError(
                "analysis_outbox_authority_mismatch",
                "续接记录的项目、预处理任务、Plan、manifest 或 Run 引用不一致。",
            )
        record = AnalysisProposalOutbox(
            intent_task_id=intent_id,
            conversation_id=conversation_id,
            project_id=project_id,
            preprocessing_task_id=preprocessing_task_id,
            plan_revision_id=plan_id,
            manifest_hash=plan.manifest_hash,
            run_id=run_id,
            analysis_request_hash=content_hash(request.strip()),
        )
        if not self._outbox_intent_matches(record):
            raise ConflictError(
                "analysis_outbox_intent_mismatch",
                "分析 outbox 必须绑定会话中具有精确身份的单条分析意图。",
            )
        return self._repository.ensure_analysis_outbox(record)

    def inspect_analysis_outbox_registration_source(
        self,
        *,
        conversation_id: str,
        project_id: str,
        preprocessing_task_id: str,
        run_id: str,
    ) -> AnalysisOutboxRegistrationSource:
        """Capture an exact, read-only source for an explicitly selected analysis intent."""

        conversation = self._conversation_repository.get_conversation(conversation_id)
        self._require_work_conversation(conversation, conversation_id, project_id)
        task = self._repository.get_task(preprocessing_task_id)
        refs = task.authority_refs
        step = self._preprocessing_step(task)
        plan_id = refs.plan_revision_id
        if (
            step is None
            or plan_id is None
            or refs.conversation_id != conversation_id
            or refs.workflow_run_id != run_id
            or (refs.analysis_intent_task_id is None) != (refs.analysis_request_hash is None)
            or task.task_id != self.preprocessing_task_id(conversation_id, plan_id)
            or step.authority_refs.conversation_id != conversation_id
            or step.authority_refs.plan_revision_id != plan_id
            or step.authority_refs.workflow_run_id != run_id
            or step.authority_refs.input_revision != refs.input_revision
        ):
            raise ConflictError(
                "analysis_outbox_preprocessing_binding_mismatch",
                "所选预处理任务没有绑定当前会话中的精确 Run、Plan 与 manifest。",
            )
        plan = self._plan_repository.get_plan(plan_id)
        run = self._run_repository.get_run(run_id)
        if (
            plan.plan_revision_id != plan_id
            or plan.project_id != project_id
            or plan.state is not PlanState.APPROVED
            or plan.manifest_hash != refs.input_revision
            or plan.plan_hash != step.input_version
            or run.run_id != run_id
            or run.project_id != project_id
            or run.plan_revision_id != plan_id
        ):
            raise ConflictError(
                "analysis_outbox_authority_mismatch",
                "所选 Run 与预处理任务绑定的项目、冻结 Plan 或 manifest 不一致。",
            )

        completion_evidence_hash: str | None = None
        if run.state in {
            WorkflowState.QUEUED,
            WorkflowState.RUNNING,
            WorkflowState.QC_REVIEW,
        }:
            if task.status in {TaskStatus.COMPLETED, TaskStatus.BLOCKED, TaskStatus.CANCELLED}:
                raise ConflictError(
                    "analysis_outbox_preprocessing_state_mismatch",
                    "预处理任务已终止，但其绑定 Run 仍处于活动状态。",
                )
            ready_artifact_ids: tuple[str, ...] = ()
        elif run.state is WorkflowState.SUCCEEDED:
            if task.status is TaskStatus.COMPLETED:
                if step.status is not TaskStepStatus.SUCCEEDED:
                    raise ConflictError(
                        "analysis_outbox_preprocessing_state_mismatch",
                        "已完成的预处理任务没有成功的唯一执行步骤。",
                    )
                authority = self._read_stable_authoritative_snapshot(task, run_id)
                if (
                    authority is None
                    or authority.snapshot.run_version != run.version
                    or authority.snapshot.unresolved_referenced_artifact_ids
                    or authority.project_id != project_id
                    or authority.plan_revision_id != plan_id
                ):
                    raise ConflictError(
                        "analysis_outbox_authority_indeterminate",
                        "预处理 Run、QC 或 Artifact 在读取期间变化或无法解析，请稍后重试。",
                    )
                try:
                    evidence = self._read_preprocessing_completion_evidence(task, run_id, authority)
                except (
                    ConflictError,
                    InputValidationError,
                    NotFoundError,
                    ValueError,
                    TypeError,
                    KeyError,
                    AttributeError,
                    ValidationError,
                ) as exc:
                    raise ConflictError(
                        "analysis_outbox_preprocessing_evidence_invalid",
                        "已完成的预处理来源未通过 QC、目标 Artifact 或 lineage 核验。",
                    ) from exc
                ready_artifact_ids = evidence.verified_artifact_ids
                completion_evidence_hash = evidence.lineage_fingerprint
                if tuple(sorted(refs.artifact_ids)) != tuple(sorted(ready_artifact_ids)) or tuple(
                    sorted(step.authority_refs.artifact_ids)
                ) != tuple(sorted(ready_artifact_ids)):
                    raise ConflictError(
                        "analysis_outbox_preprocessing_artifacts_mismatch",
                        "已完成任务引用的 Artifact IDs 与当前完整核验结果不一致。",
                    )
            elif task.status not in {TaskStatus.WAITING_RUN, TaskStatus.WAITING_USER}:
                raise ConflictError(
                    "analysis_outbox_preprocessing_state_mismatch",
                    "Run 已成功，但预处理任务不处于可等待完成收口的状态。",
                )
            else:
                ready_artifact_ids = ()
        else:
            raise ConflictError(
                "analysis_outbox_preprocessing_run_not_usable",
                "失败或已取消的预处理 Run 不能作为新分析目标来源。",
            )

        return AnalysisOutboxRegistrationSource(
            preprocessing_task_id=task.task_id,
            task_version=task.version,
            preprocessing_step_id=step.step_id,
            step_version=step.version,
            task_status=task.status.value,
            step_status=step.status.value,
            run_id=run.run_id,
            run_version=run.version,
            run_state=run.state.value,
            plan_revision_id=plan.plan_revision_id,
            plan_version=plan.version,
            plan_hash=plan.plan_hash,
            manifest_hash=plan.manifest_hash,
            ready_artifact_ids=ready_artifact_ids,
            completion_evidence_hash=completion_evidence_hash,
        )

    def register_analysis_proposal_outbox(
        self,
        *,
        intent: Mapping[str, Any],
        conversation_id: str,
        project_id: str,
        source: AnalysisOutboxRegistrationSource,
    ) -> AnalysisProposalOutbox:
        """Commit one explicit intent against the source captured in prepare."""

        task = self._repository.get_task(source.preprocessing_task_id)
        step = self._preprocessing_step(task)
        plan = self._plan_repository.get_plan(source.plan_revision_id)
        run = self._run_repository.get_run(source.run_id)
        if (
            step is None
            or task.task_id != source.preprocessing_task_id
            or task.version != source.task_version
            or step.step_id != source.preprocessing_step_id
            or step.version != source.step_version
            or task.status.value != source.task_status
            or step.status.value != source.step_status
            or task.authority_refs.conversation_id != conversation_id
            or task.authority_refs.workflow_run_id != source.run_id
            or task.authority_refs.plan_revision_id != source.plan_revision_id
            or task.authority_refs.input_revision != source.manifest_hash
            or step.authority_refs.conversation_id != conversation_id
            or step.authority_refs.workflow_run_id != source.run_id
            or step.authority_refs.plan_revision_id != source.plan_revision_id
            or step.authority_refs.input_revision != source.manifest_hash
            or plan.version != source.plan_version
            or plan.plan_revision_id != source.plan_revision_id
            or plan.project_id != project_id
            or plan.plan_hash != source.plan_hash
            or plan.manifest_hash != source.manifest_hash
            or run.version != source.run_version
            or run.run_id != source.run_id
            or run.project_id != project_id
            or run.plan_revision_id != source.plan_revision_id
            or run.state.value != source.run_state
        ):
            raise ConflictError(
                "analysis_outbox_source_changed",
                "显式分析来源在请求准备期间发生变化；没有登记续接任务，请刷新后重试。",
            )
        if source.ready_artifact_ids:
            if (
                task.status is not TaskStatus.COMPLETED
                or step.status is not TaskStepStatus.SUCCEEDED
                or tuple(sorted(task.authority_refs.artifact_ids))
                != tuple(sorted(source.ready_artifact_ids))
                or tuple(sorted(step.authority_refs.artifact_ids))
                != tuple(sorted(source.ready_artifact_ids))
                or source.completion_evidence_hash is None
            ):
                raise ConflictError(
                    "analysis_outbox_preprocessing_artifacts_mismatch",
                    "预处理任务的目标 Artifact 引用已变化；没有登记续接任务。",
                )
            authority = self._read_stable_authoritative_snapshot(task, source.run_id)
            if (
                authority is None
                or authority.snapshot.run_version != source.run_version
                or authority.snapshot.unresolved_referenced_artifact_ids
                or authority.project_id != project_id
                or authority.plan_revision_id != source.plan_revision_id
            ):
                raise ConflictError(
                    "analysis_outbox_authority_indeterminate",
                    "预处理 Run、QC 或 Artifact 在最终登记前变化或无法解析。",
                )
            try:
                evidence = self._read_preprocessing_completion_evidence(
                    task, source.run_id, authority
                )
            except (
                ConflictError,
                InputValidationError,
                NotFoundError,
                ValueError,
                TypeError,
                KeyError,
                AttributeError,
                ValidationError,
            ) as exc:
                raise ConflictError(
                    "analysis_outbox_preprocessing_evidence_invalid",
                    "已完成的预处理来源未通过最终 QC、目标 Artifact 或 lineage 复核。",
                ) from exc
            if (
                tuple(sorted(evidence.verified_artifact_ids))
                != tuple(sorted(source.ready_artifact_ids))
                or evidence.lineage_fingerprint != source.completion_evidence_hash
            ):
                raise ConflictError(
                    "analysis_outbox_preprocessing_evidence_changed",
                    "预处理完成证据在请求准备期间发生变化；没有登记续接任务。",
                )
        outbox = self.ensure_analysis_proposal_outbox(
            intent=intent,
            conversation_id=conversation_id,
            project_id=project_id,
            preprocessing_task_id=source.preprocessing_task_id,
        )
        if outbox is None:
            raise ConflictError(
                "analysis_outbox_intent_invalid", "显式分析意图没有创建 proposal outbox。"
            )
        if source.ready_artifact_ids:
            return self._repository.mark_analysis_outbox_ready(
                outbox.intent_task_id,
                preprocessing_task_id=source.preprocessing_task_id,
                plan_revision_id=source.plan_revision_id,
                manifest_hash=source.manifest_hash,
                run_id=source.run_id,
                artifact_ids=source.ready_artifact_ids,
            )
        return outbox

    def get_task(self, task_id: str) -> WorkTask:
        return self._repository.get_task(task_id)

    def get_step_plan_review(
        self, conversation_id: str, task_id: str, step_id: str
    ) -> StepPlanReviewView:
        """Read one step review using a consistent repository snapshot."""

        with self.atomic():
            return self._read_step_plan_review(conversation_id, task_id, step_id)

    def _read_step_plan_review(
        self, conversation_id: str, task_id: str, step_id: str
    ) -> StepPlanReviewView:
        """Read the exact frozen Plan bound to one READY step awaiting user review.

        This projection is intentionally read-only. It neither records Plan
        approval nor creates/authorizes a workflow Run.
        """

        task = self._repository.get_task(task_id)
        if task.task_id != task_id:
            raise NotFoundError("WorkTask", task_id)
        if task.authority_refs.conversation_id != conversation_id:
            # Keep this route conversation-scoped; do not disclose whether a
            # task ID is valid in another Work conversation.
            raise NotFoundError("WorkTask", task_id)

        conversation = self._conversation_repository.get_conversation(conversation_id)
        if (
            conversation.conversation_id != conversation_id
            or conversation.mode is not ConversationMode.WORK
            or not conversation.project_id
        ):
            raise NotFoundError("Work 会话", conversation_id)
        project = self._project_repository.get_project(conversation.project_id)
        if project.project_id != conversation.project_id:
            raise ConflictError(
                "step_plan_review_project_mismatch", "Work 会话项目与项目读取结果不一致。"
            )

        if task.status is not TaskStatus.WAITING_USER:
            raise ConflictError(
                "step_plan_review_task_not_waiting",
                "只有等待用户审查的 WorkTask 才能读取步骤方案。",
            )
        waiting = task.waiting_condition
        if waiting is None or waiting.kind is not WaitingKind.USER_DECISION:
            raise ConflictError(
                "step_plan_review_wait_mismatch", "WorkTask 当前等待状态不是用户决策。"
            )
        if task.authority_refs.workflow_run_id is not None:
            raise ConflictError(
                "step_plan_review_run_already_bound", "待审查步骤不能已有 WorkTask Run 绑定。"
            )

        try:
            ordered = resolve_linear_step_sequence(task)
        except ValueError as exc:
            raise ConflictError(
                "step_plan_review_sequence_invalid", "WorkTask 步骤序列不是合法线性流程。"
            ) from exc
        if len(ordered) < 2:
            raise ConflictError(
                "step_plan_review_sequence_required", "步骤级方案审查仅适用于显式多步骤任务。"
            )
        active = tuple(
            item
            for item in ordered
            if item.status in {TaskStepStatus.READY, TaskStepStatus.RUNNING, TaskStepStatus.WAITING}
        )
        ready = tuple(item for item in active if item.status is TaskStepStatus.READY)
        if len(active) != 1 or len(ready) != 1 or ready[0].step_id != step_id:
            raise ConflictError(
                "step_plan_review_step_not_current",
                "指定步骤不是当前唯一 READY 的线性步骤。",
                step_id=step_id,
            )
        step = ready[0]
        step_index = ordered.index(step)
        if step_index == 0 or any(
            item.status is not TaskStepStatus.SUCCEEDED for item in ordered[:step_index]
        ):
            raise ConflictError(
                "step_plan_review_predecessor_not_succeeded",
                "待审查步骤必须位于已成功的线性前缀之后。",
            )
        if step.kind not in {"preprocessing_run", "statistical_run"}:
            raise ConflictError(
                "step_plan_review_kind_unsupported", "当前步骤类型没有已注册的 Plan 审查合同。"
            )
        if step.authority_refs.conversation_id != conversation_id:
            raise ConflictError(
                "step_plan_review_step_conversation_mismatch", "步骤没有绑定到请求的 Work 会话。"
            )

        plan_revision_id = task.authority_refs.plan_revision_id
        input_revision = task.authority_refs.input_revision
        if (
            not plan_revision_id
            or not input_revision
            or step.authority_refs.plan_revision_id != plan_revision_id
            or step.authority_refs.input_revision != input_revision
            or step.authority_refs.workflow_run_id is not None
            or any(
                item.authority_refs.plan_revision_id == plan_revision_id
                for item in ordered
                if item.step_id != step.step_id
            )
        ):
            raise ConflictError(
                "step_plan_review_binding_mismatch",
                "Task 根引用与当前步骤的 Plan、输入或 Run 身份不一致。",
            )
        try:
            expected_wait = step_plan_review_reference(task_id, step_id, plan_revision_id)
        except ValueError as exc:
            raise ConflictError(
                "step_plan_review_identity_invalid", "步骤方案审查身份超出合法范围。"
            ) from exc
        if waiting.reference != expected_wait:
            raise ConflictError(
                "step_plan_review_wait_reference_mismatch",
                "WorkTask 的等待引用没有指向该步骤的精确 Plan 修订。",
            )

        plan = self._plan_repository.get_plan(plan_revision_id)
        if plan.plan_revision_id != plan_revision_id or plan.project_id != project.project_id:
            raise ConflictError(
                "step_plan_review_plan_scope_mismatch",
                "重读的 Plan 修订不属于当前 Work 会话项目。",
            )

        expected_artifacts: tuple[DpabiPreprocessingOutput, ...] = ()
        if step.kind == "statistical_run":
            proposal_hash = step.input_version
            if proposal_hash is None:
                raise ConflictError(
                    "step_plan_review_statistical_input_missing",
                    "统计步骤缺少提案版本身份。",
                )
            try:
                self._validate_hash(proposal_hash, "proposal_hash")
                context, _design = self._statistical_plan_context(
                    plan,
                    conversation_id=conversation_id,
                    project_id=project.project_id,
                    proposal_hash=proposal_hash,
                )
            except (ConflictError, InputValidationError) as exc:
                raise ConflictError(
                    "step_plan_review_statistical_plan_invalid",
                    "冻结统计 Plan 与当前会话、提案或哈希不匹配。",
                ) from exc
            if (
                plan.state not in {PlanState.DRAFT, PlanState.AWAITING_APPROVAL, PlanState.APPROVED}
                or context["draft_hash"] != input_revision
                or step.expected_artifacts
            ):
                raise ConflictError(
                    "step_plan_review_statistical_binding_mismatch",
                    "统计 Plan 状态、输入草稿或产物投影与当前步骤不匹配。",
                )
        else:
            if (
                plan.state not in {PlanState.AWAITING_APPROVAL, PlanState.APPROVED}
                or step.input_version != plan.plan_hash
                or input_revision != plan.manifest_hash
            ):
                raise ConflictError(
                    "step_plan_review_preprocessing_binding_mismatch",
                    "预处理 Plan 状态、输入 manifest 或方案哈希与当前步骤不匹配。",
                )
            try:
                expected_artifacts = self._derive_preprocessing_expectations(
                    plan, project.project_id
                )
            except (
                NotFoundError,
                _InvalidPreprocessingAuthority,
                ValueError,
                TypeError,
                KeyError,
                AttributeError,
                ValidationError,
            ) as exc:
                raise ConflictError(
                    "step_plan_review_preprocessing_plan_invalid",
                    "预处理 Plan 的冻结 manifest、SkillPlan 或输出合同无法核验。",
                ) from exc
            if not expected_artifacts or step.expected_artifacts != expected_artifacts:
                raise ConflictError(
                    "step_plan_review_preprocessing_outputs_mismatch",
                    "步骤输出投影与冻结预处理 Plan 推导结果不一致。",
                )

        return StepPlanReviewView(
            conversation_id=conversation_id,
            project_id=project.project_id,
            task_id=task.task_id,
            task_version=task.version,
            task_status=task.status,
            step_id=step.step_id,
            step_sequence=step.sequence,
            step_kind=step.kind,
            step_status=step.status,
            step_version=step.version,
            step_input_version=step.input_version,
            input_revision=input_revision,
            waiting_reference=waiting.reference,
            plan_revision_id=plan.plan_revision_id,
            plan_hash=plan.plan_hash,
            plan_state=plan.state,
            plan=plan,
            expected_artifacts=expected_artifacts,
            execution_authorized=False,
        )

    def get_composite_goal_progress(
        self, conversation_id: str, intent_task_id: str
    ) -> CompositeGoalProgress:
        """Read and project one explicitly identified composite goal."""

        snapshot = self._repository.get_composite_goal_snapshot(intent_task_id)
        self._require_composite_goal_scope(snapshot, conversation_id, intent_task_id)
        return project_composite_goal(snapshot)

    def list_composite_goal_progress(self, conversation_id: str) -> list[CompositeGoalProgress]:
        """List freshly projected composite goals visible to one Work conversation."""

        conversation = self._conversation_repository.get_conversation(conversation_id)
        if conversation.mode is not ConversationMode.WORK:
            raise NotFoundError("Work 会话复合目标", conversation_id)
        intent_task_ids = self._repository.list_composite_goal_intent_ids(conversation_id)
        return [
            self.get_composite_goal_progress(conversation_id, intent_task_id)
            for intent_task_id in intent_task_ids
        ]

    def find_composite_goal(self, intent_task_id: str) -> CompositeGoalIdentity | None:
        """Distinguish a legacy standalone outbox from a broken goal ledger."""

        return self._repository.find_composite_goal(intent_task_id)

    def get_composite_goal(
        self, conversation_id: str, intent_task_id: str
    ) -> CompositeGoalIdentity:
        snapshot = self._repository.get_composite_goal_snapshot(intent_task_id)
        self._require_composite_goal_scope(snapshot, conversation_id, intent_task_id)
        return snapshot.goal

    def get_composite_goal_model_budget(self, intent_task_id: str) -> CompositeGoalModelBudget:
        """Read cumulative limits and usage for one explicit composite goal."""

        return self._repository.get_composite_goal_model_budget(intent_task_id)

    def reserve_composite_goal_model_call(
        self,
        intent_task_id: str,
        *,
        reservation_id: str,
        call_id: str,
        estimated_tokens: int,
        estimated_elapsed_seconds: int = 1,
    ) -> CompositeGoalModelCallReserveResult:
        """Atomically reserve cumulative goal budget before provider dispatch."""

        return self._repository.reserve_composite_goal_model_call(
            intent_task_id,
            reservation_id=reservation_id,
            call_id=call_id,
            estimated_tokens=estimated_tokens,
            estimated_elapsed_seconds=estimated_elapsed_seconds,
        )

    def settle_composite_goal_model_call(
        self,
        intent_task_id: str,
        *,
        reservation_id: str,
        actual_tokens: int | None,
        elapsed_seconds: int,
    ) -> CompositeGoalModelCallSettlement:
        """Persist one provider outcome and return the cumulative goal budget."""

        return self._repository.settle_composite_goal_model_call(
            intent_task_id,
            reservation_id=reservation_id,
            actual_tokens=actual_tokens,
            elapsed_seconds=elapsed_seconds,
        )

    def find_composite_goal_for_analysis_task(
        self, analysis_work_task_id: str
    ) -> CompositeGoalIdentity | None:
        """Resolve an internal task link without exposing an unscoped progress API."""

        return self._repository.find_composite_goal_for_analysis_task(analysis_work_task_id)

    @staticmethod
    def _require_composite_goal_scope(
        snapshot: CompositeGoalSnapshot, conversation_id: str, intent_task_id: str
    ) -> None:
        goal = snapshot.goal
        task = snapshot.preprocessing_task
        refs = task.authority_refs if task is not None else None
        preprocessing_root_matches = bool(
            refs is not None
            and refs.analysis_intent_task_id == goal.intent_task_id
            and refs.analysis_request_hash == goal.analysis_request_hash
        )
        outbox = snapshot.analysis_proposal_outbox
        outbox_root_matches = bool(
            outbox is not None
            and refs is not None
            and outbox.intent_task_id == goal.intent_task_id
            and outbox.conversation_id == goal.conversation_id
            and outbox.project_id == goal.project_id
            and outbox.preprocessing_task_id == goal.preprocessing_task_id
            and outbox.analysis_request_hash == goal.analysis_request_hash
            and outbox.plan_revision_id == refs.plan_revision_id
            and outbox.manifest_hash == refs.input_revision
            and outbox.run_id == refs.workflow_run_id
        )
        if (
            snapshot.conversation_mode != ConversationMode.WORK.value
            or snapshot.conversation_project_id not in {None, goal.project_id}
            or snapshot.preprocessing_plan_project_id != goal.project_id
            or task is None
            or task.authority_refs.plan_revision_id != snapshot.preprocessing_plan_revision_id
            or task.authority_refs.input_revision != snapshot.preprocessing_plan_manifest_hash
            or goal.conversation_id != conversation_id
            or task.task_id != goal.preprocessing_task_id
            or task.authority_refs.conversation_id != goal.conversation_id
            or task.authority_refs.plan_revision_id is None
            or (task.authority_refs.analysis_intent_task_id is None)
            != (task.authority_refs.analysis_request_hash is None)
            or (not preprocessing_root_matches and not outbox_root_matches)
        ):
            raise NotFoundError("复合自动化目标", f"{conversation_id}:{intent_task_id}")

    def get_analysis_outbox(self, intent_task_id: str) -> AnalysisProposalOutbox:
        return self._repository.get_analysis_outbox(intent_task_id)

    def claim_next_analysis_outbox(
        self, worker_id: str, *, lease_seconds: int
    ) -> AnalysisOutboxClaim | None:
        return self._repository.claim_analysis_outbox(worker_id, lease_seconds=lease_seconds)

    def renew_analysis_outbox_claim(
        self, claim: AnalysisOutboxClaim, *, lease_seconds: int
    ) -> AnalysisOutboxClaim | None:
        return self._repository.renew_analysis_outbox_claim(claim, lease_seconds=lease_seconds)

    def defer_analysis_outbox_claim(
        self,
        claim: AnalysisOutboxClaim,
        *,
        retry_after_seconds: int,
        error_code: str,
    ) -> bool:
        return self._repository.defer_analysis_outbox_claim(
            claim,
            retry_after_seconds=retry_after_seconds,
            error_code=error_code,
        )

    def complete_analysis_outbox(
        self,
        claim: AnalysisOutboxClaim,
        *,
        status: AnalysisOutboxStatus,
        continuation_message_id: str,
        analysis_work_task_id: str | None,
        error_code: str | None = None,
        record_attempt: bool = False,
    ) -> AnalysisProposalOutbox:
        return self._repository.complete_analysis_outbox(
            claim,
            status=status,
            continuation_message_id=continuation_message_id,
            analysis_work_task_id=analysis_work_task_id,
            error_code=error_code,
            record_attempt=record_attempt,
        )

    def requeue_analysis_outbox(
        self, intent_task_id: str, *, recoverable_error_codes: tuple[str, ...]
    ) -> AnalysisProposalOutbox | None:
        return self._repository.requeue_analysis_outbox(
            intent_task_id, recoverable_error_codes=recoverable_error_codes
        )

    def ensure_result_interpretation_outbox(
        self, record: StatisticalResultInterpretationOutbox
    ) -> StatisticalResultInterpretationOutbox:
        return self._repository.ensure_result_interpretation_outbox(record)

    def get_result_interpretation_outbox(
        self, task_id: str
    ) -> StatisticalResultInterpretationOutbox:
        return self._repository.get_result_interpretation_outbox(task_id)

    def claim_next_result_interpretation_outbox(
        self, worker_id: str, *, lease_seconds: int
    ) -> ResultInterpretationClaim | None:
        return self._repository.claim_result_interpretation_outbox(
            worker_id, lease_seconds=lease_seconds
        )

    def renew_result_interpretation_claim(
        self, claim: ResultInterpretationClaim, *, lease_seconds: int
    ) -> ResultInterpretationClaim | None:
        return self._repository.renew_result_interpretation_claim(
            claim, lease_seconds=lease_seconds
        )

    def defer_result_interpretation_claim(
        self,
        claim: ResultInterpretationClaim,
        *,
        retry_after_seconds: int,
        error_code: str,
    ) -> bool:
        return self._repository.defer_result_interpretation_claim(
            claim, retry_after_seconds=retry_after_seconds, error_code=error_code
        )

    def complete_result_interpretation_outbox(
        self,
        claim: ResultInterpretationClaim,
        *,
        status: ResultInterpretationStatus,
        continuation_message_id: str,
        error_code: str | None = None,
        record_attempt: bool = False,
    ) -> StatisticalResultInterpretationOutbox:
        return self._repository.complete_result_interpretation_outbox(
            claim,
            status=status,
            continuation_message_id=continuation_message_id,
            error_code=error_code,
            record_attempt=record_attempt,
        )

    def requeue_result_interpretation_outbox(
        self, task_id: str, *, recoverable_error_codes: tuple[str, ...]
    ) -> StatisticalResultInterpretationOutbox | None:
        return self._repository.requeue_result_interpretation_outbox(
            task_id, recoverable_error_codes=recoverable_error_codes
        )

    def verify_result_interpretation_source(
        self, outbox: StatisticalResultInterpretationOutbox
    ) -> VerifiedResultInterpretationSource:
        """Re-read the full completed WorkTask → Run/Plan → result → Artifact chain."""
        for _attempt in range(3):
            task = self._repository.get_task(outbox.task_id)
            refs = task.authority_refs
            step = self._statistical_step(task)
            if (
                task.status is not TaskStatus.COMPLETED
                or step is None
                or step.status is not TaskStepStatus.SUCCEEDED
                or refs.conversation_id != outbox.conversation_id
                or refs.workflow_run_id != outbox.run_id
                or refs.plan_revision_id != outbox.plan_revision_id
                or tuple(sorted(refs.artifact_ids)) != tuple(sorted(outbox.artifact_ids))
                or tuple(sorted(step.authority_refs.artifact_ids))
                != tuple(sorted(outbox.artifact_ids))
                or step.authority_refs.workflow_run_id != outbox.run_id
                or step.authority_refs.plan_revision_id != outbox.plan_revision_id
                or not step.input_version
            ):
                raise ConflictError(
                    "result_interpretation_task_mismatch",
                    "统计 WorkTask 已变化或不再绑定同一真实结果。",
                )
            conversation = self._conversation_repository.get_conversation(outbox.conversation_id)
            self._require_work_conversation(conversation, outbox.conversation_id, outbox.project_id)
            if conversation.project_id != outbox.project_id:
                raise ConflictError(
                    "result_interpretation_conversation_mismatch",
                    "统计结果所属工作区与当前会话不一致。",
                )
            plan = self._plan_repository.get_plan(outbox.plan_revision_id)
            run_before = self._run_repository.get_run(outbox.run_id)
            if (
                run_before.run_id != outbox.run_id
                or run_before.project_id != outbox.project_id
                or run_before.plan_revision_id != outbox.plan_revision_id
                or run_before.state is not WorkflowState.SUCCEEDED
                or plan.plan_revision_id != outbox.plan_revision_id
                or plan.project_id != outbox.project_id
                or plan.state is not PlanState.APPROVED
            ):
                raise ConflictError(
                    "result_interpretation_run_plan_mismatch",
                    "统计 Run 或冻结 Plan 与结果解释续接绑定不一致。",
                )
            context, design = self._statistical_plan_context(
                plan,
                conversation_id=outbox.conversation_id,
                project_id=outbox.project_id,
                proposal_hash=step.input_version,
            )
            if task.authority_refs.input_revision != context["draft_hash"]:
                raise ConflictError(
                    "result_interpretation_draft_binding_mismatch",
                    "统计任务草稿修订与冻结 Plan 不一致。",
                )
            if not self._analysis_proposal_task_identity_matches(
                task, outbox.conversation_id, context["proposal_hash"]
            ):
                raise ConflictError(
                    "result_interpretation_task_identity_mismatch",
                    "统计 WorkTask ID 与会话/提案绑定不一致。",
                )
            summaries = self._artifact_repository.list_statistical_results(
                project_id=outbox.project_id, run_id=outbox.run_id
            )
            if len(summaries) != 1 or summaries[0].result_id != outbox.result_id:
                raise ConflictError(
                    "result_interpretation_result_not_unique",
                    "统计 Run 当前没有唯一且匹配的已登记结果。",
                )
            summary = summaries[0]
            detail = self._artifact_repository.get_statistical_result(outbox.result_id)
            artifacts = self._artifact_repository.list_artifacts(outbox.run_id)
            artifact_ids = self._verify_statistical_result(
                summary=summary,
                detail=detail,
                plan=plan,
                design=design,
                run_id=outbox.run_id,
                project_id=outbox.project_id,
                registered_artifacts=artifacts,
            )
            run_after = self._run_repository.get_run(outbox.run_id)
            if run_before.version != run_after.version:
                continue
            if (
                detail.result_id != outbox.result_id
                or detail.bundle_hash != outbox.result_bundle_hash
                or detail.version != outbox.result_version
                or detail.project_id != outbox.project_id
                or detail.run_id != outbox.run_id
                or detail.design_revision_id != design.revision_id
                or artifact_ids != tuple(sorted(outbox.artifact_ids))
            ):
                raise ConflictError(
                    "result_interpretation_evidence_changed",
                    "统计结果或其冻结 Artifact 证据与续接记录不一致。",
                )
            facts = result_model_facts(detail)
            # Only aggregate design and result evidence enters model context. The
            # registered IDs, hashes, paths, report body and subject rows remain
            # server-side. Cluster indices are display positions, not record IDs.
            model_facts = project_result_model_facts(facts)
            # Build the deterministic text from the same bounded whitelist as the
            # model facts; free-form design labels remain explicitly untrusted data.
            readout = render_result_readout(
                {**model_facts.model_dump(mode="json"), "status": "succeeded"}
            )
            evidence = {
                "task_id": task.task_id,
                "task_version": task.version,
                "task_status": task.status.value,
                "step_id": step.step_id,
                "step_version": step.version,
                "step_input_version": step.input_version,
                "conversation_id": conversation.conversation_id,
                "conversation_project_id": conversation.project_id,
                "plan_revision_id": plan.plan_revision_id,
                "plan_hash": plan.plan_hash,
                "run_id": run_after.run_id,
                "run_version": run_after.version,
                "result_id": detail.result_id,
                "result_bundle_hash": detail.bundle_hash,
                "result_version": detail.version,
                "artifact_ids": artifact_ids,
                "artifact_metadata": [
                    {
                        "artifact_id": item.artifact_id,
                        "artifact_type": item.artifact_type,
                        "checksum": item.checksum,
                        "size_bytes": item.size_bytes,
                        "relative_path": item.relative_path,
                        "provenance_hash": content_hash(item.provenance),
                    }
                    for item in sorted(artifacts, key=lambda item: item.artifact_id)
                    if item.artifact_id in set(artifact_ids)
                ],
            }
            return VerifiedResultInterpretationSource(
                task_id=task.task_id,
                conversation_id=conversation.conversation_id,
                project_id=outbox.project_id,
                run_id=outbox.run_id,
                plan_revision_id=outbox.plan_revision_id,
                result_id=detail.result_id,
                result_bundle_hash=detail.bundle_hash,
                result_version=detail.version,
                artifact_ids=artifact_ids,
                source_evidence_hash=content_hash(evidence),
                model_facts=model_facts,
                deterministic_readout=readout,
            )
        raise ConflictError(
            "result_interpretation_authority_indeterminate",
            "统计 Run 在多次权威读取期间持续变化，暂不能解释结果。",
        )

    def verify_analysis_outbox_source(
        self, outbox: AnalysisProposalOutbox
    ) -> VerifiedAnalysisOutboxSource:
        """Recheck the exact intent and preprocessing authority before model use."""

        try:
            conversation = self._conversation_repository.get_conversation(outbox.conversation_id)
        except NotFoundError as exc:
            raise ConflictError(
                "analysis_outbox_conversation_missing", "续接来源会话已不存在。"
            ) from exc
        self._require_work_conversation(conversation, outbox.conversation_id, outbox.project_id)
        candidates: list[dict[str, Any]] = []
        for message in conversation.messages:
            candidate = message.payload.get("analysis_dependency")
            if isinstance(candidate, dict) and candidate.get("task_id") == outbox.intent_task_id:
                candidates.append(candidate)
        intent = next(
            (
                item
                for item in reversed(candidates)
                if item.get("task_kind") == "preprocess_then_analyze"
                and item.get("conversation_id") == outbox.conversation_id
                and item.get("project_id") == outbox.project_id
                and isinstance(item.get("analysis_request"), str)
                and content_hash(str(item["analysis_request"]).strip())
                == outbox.analysis_request_hash
            ),
            None,
        )
        if intent is None:
            raise ConflictError(
                "analysis_outbox_intent_mismatch", "续接记录无法绑定到精确的原始分析请求。"
            )

        try:
            composite = self._repository.get_composite_goal_snapshot(outbox.intent_task_id)
        except NotFoundError as exc:
            raise ConflictError(
                "analysis_outbox_goal_missing", "分析续接没有精确绑定的复合目标 root。"
            ) from exc
        self._require_composite_goal_scope(composite, outbox.conversation_id, outbox.intent_task_id)
        goal = composite.goal
        if (
            goal.intent_task_id != outbox.intent_task_id
            or goal.conversation_id != outbox.conversation_id
            or goal.project_id != outbox.project_id
            or goal.preprocessing_task_id != outbox.preprocessing_task_id
            or goal.analysis_request_hash != outbox.analysis_request_hash
            or goal.analysis_proposal_outbox_intent_task_id != outbox.intent_task_id
            or composite.analysis_proposal_outbox != outbox
        ):
            raise ConflictError(
                "analysis_outbox_goal_mismatch",
                "分析续接的 root 与其精确来源或 outbox 不一致。",
            )

        task = self._repository.get_task(outbox.preprocessing_task_id)
        refs = task.authority_refs
        step = self._preprocessing_step(task)
        if (
            task.status is not TaskStatus.COMPLETED
            or step is None
            or step.status is not TaskStepStatus.SUCCEEDED
            or refs.conversation_id != outbox.conversation_id
            or (refs.analysis_intent_task_id is None) != (refs.analysis_request_hash is None)
            or step.authority_refs.conversation_id != refs.conversation_id
            or step.authority_refs.analysis_intent_task_id != refs.analysis_intent_task_id
            or step.authority_refs.analysis_request_hash != refs.analysis_request_hash
            or refs.plan_revision_id != outbox.plan_revision_id
            or refs.workflow_run_id != outbox.run_id
            or refs.input_revision != outbox.manifest_hash
            or tuple(sorted(refs.artifact_ids)) != tuple(sorted(outbox.artifact_ids))
            or tuple(sorted(step.authority_refs.artifact_ids)) != tuple(sorted(outbox.artifact_ids))
        ):
            raise ConflictError(
                "analysis_outbox_preprocessing_mismatch",
                "预处理 WorkTask 已变化或不再绑定同一意图、Plan、Run 与产物。",
            )

        plan = self._plan_repository.get_plan(outbox.plan_revision_id)
        run = self._run_repository.get_run(outbox.run_id)
        if (
            plan.plan_revision_id != outbox.plan_revision_id
            or plan.project_id != outbox.project_id
            or plan.state is not PlanState.APPROVED
            or plan.plan.get("kind") != "skill_plan"
            or plan.manifest_hash != outbox.manifest_hash
            or run.run_id != outbox.run_id
            or run.project_id != outbox.project_id
            or run.plan_revision_id != outbox.plan_revision_id
            or run.state is not WorkflowState.SUCCEEDED
        ):
            raise ConflictError(
                "analysis_outbox_run_authority_mismatch",
                "来源 Run、冻结预处理 Plan 或 manifest 与续接记录不一致。",
            )
        authority = self._read_stable_authoritative_snapshot(task, outbox.run_id)
        if authority is None:
            raise ConflictError(
                "analysis_outbox_authority_indeterminate",
                "来源 Run、QC 或 Artifact 正在变化，暂不能准备分析草稿。",
            )
        snapshot = authority.snapshot
        qc_approved = (
            snapshot.qc_status == "approved" and snapshot.latest_qc_review_state == "approved"
        )
        qc_skipped = snapshot.qc_status in {"skipped", "qc_skipped"} and bool(snapshot.qc_skip_id)
        if (
            authority.project_id != outbox.project_id
            or authority.plan_revision_id != outbox.plan_revision_id
            or snapshot.run_state != WorkflowState.SUCCEEDED.value
            or not (qc_approved or qc_skipped)
            or snapshot.unresolved_referenced_artifact_ids
        ):
            raise ConflictError(
                "analysis_outbox_qc_or_artifact_mismatch",
                "来源 QC 尚未通过或续接 Artifact 引用无法核验。",
            )

        expected = self._derive_preprocessing_expectations(plan, outbox.project_id)
        if step.expected_artifacts != expected:
            raise ConflictError(
                "analysis_outbox_output_contract_changed",
                "来源 Plan 的预处理目标与任务记录不一致。",
            )
        raw_skill = plan.plan.get("skill_plan")
        if not isinstance(raw_skill, Mapping):
            raise ConflictError("analysis_outbox_skill_plan_missing", "冻结 SkillPlan 不可读取。")
        skill = SkillPlan.model_validate(raw_skill)
        resolved = dict(skill.resolved_parameters)
        alff = (
            AlffFalffParameters.model_validate(resolved["alff_falff"])
            if "alff_falff" in resolved
            else None
        )
        reho = RehoParameters.model_validate(resolved["reho"]) if "reho" in resolved else None
        verified_ids = self._verify_preprocessing_artifacts(
            expected=expected,
            artifacts=authority.artifacts,
            project_id=outbox.project_id,
            run_id=outbox.run_id,
            manifest_hash=outbox.manifest_hash,
            alff=alff,
            reho=reho,
        )
        if tuple(sorted(outbox.artifact_ids)) != verified_ids:
            raise ConflictError(
                "analysis_outbox_artifact_ids_mismatch",
                "outbox Artifact IDs 与当前冻结目标的完整核验结果不一致。",
            )
        target_artifacts = {
            item.artifact_id: item
            for item in authority.artifacts
            if item.artifact_id in set(outbox.artifact_ids)
        }
        dependency_artifact_ids: set[str] = set()
        for target in target_artifacts.values():
            raw_lineage = target.provenance.get("lineage")
            if not isinstance(raw_lineage, Mapping):
                raise ConflictError(
                    "analysis_outbox_artifact_ids_mismatch",
                    "目标产物缺少已核验的指标谱系。",
                )
            lineage = ArtifactLineage.model_validate(raw_lineage)
            mask_id = lineage.mask_artifact_id
            if mask_id is None or mask_id in target_artifacts:
                continue
            mask = self._artifact_repository.get_artifact(mask_id)
            raw_mask_lineage = mask.provenance.get("lineage")
            if not isinstance(raw_mask_lineage, Mapping):
                raise ConflictError(
                    "analysis_outbox_mask_lineage_mismatch",
                    "目标产物引用的 mask 缺少已登记的类型化谱系。",
                )
            mask_lineage = ArtifactLineage.model_validate(raw_mask_lineage)
            if (
                mask.artifact_id != mask_id
                or mask.project_id != outbox.project_id
                or mask_lineage.artifact_id != mask_id
                or mask_lineage.kind is not ArtifactKind.BRAIN_MASK
                or mask_lineage.subject_id is not None
                or not mask_lineage.metadata_verified
                or mask_lineage.grid_signature != lineage.mask_grid_signature
            ):
                raise ConflictError(
                    "analysis_outbox_mask_lineage_mismatch",
                    "目标产物引用的 mask 不属于本项目或其已核验网格谱系不匹配。",
                )
            dependency_artifact_ids.add(mask_id)
        artifact_facts = [
            {
                "artifact_id": item.artifact_id,
                "artifact_type": item.artifact_type,
                "checksum": item.checksum,
                "size_bytes": item.size_bytes,
                "relative_path": item.relative_path,
                "provenance_hash": content_hash(item.provenance),
                "lineage": ArtifactLineage.model_validate(
                    item.provenance.get("lineage", {})
                ).model_dump(mode="json"),
            }
            for item in sorted(
                (
                    *target_artifacts.values(),
                    *(
                        self._artifact_repository.get_artifact(artifact_id)
                        for artifact_id in dependency_artifact_ids
                    ),
                ),
                key=lambda artifact: artifact.artifact_id,
            )
        ]
        evidence = {
            "conversation_id": outbox.conversation_id,
            "project_id": outbox.project_id,
            "intent_task_id": outbox.intent_task_id,
            "request_hash": outbox.analysis_request_hash,
            "preprocessing_task_id": task.task_id,
            "plan_revision_id": plan.plan_revision_id,
            "plan_hash": plan.plan_hash,
            "manifest_hash": plan.manifest_hash,
            "run_id": run.run_id,
            "run_version": snapshot.run_version,
            "qc_status": snapshot.qc_status,
            "qc_review_id": snapshot.latest_qc_review_id,
            "qc_review_state": snapshot.latest_qc_review_state,
            "qc_skip_id": snapshot.qc_skip_id,
            "artifact_facts": artifact_facts,
        }
        metric_targets = [
            (target, ArtifactLineage.model_validate(target.provenance.get("lineage", {})))
            for target in target_artifacts.values()
        ]
        metric_targets = [
            (target, lineage)
            for target, lineage in metric_targets
            if lineage.kind
            in {ArtifactKind.ALFF_MAP, ArtifactKind.FALFF_MAP, ArtifactKind.REHO_MAP}
        ]
        metric_lineages = [lineage for _target, lineage in metric_targets]
        model_facts = AnalysisSourceModelFacts.summarize(
            source_run_state=snapshot.run_state,
            qc_status=snapshot.qc_status or "unknown",
            qc_review_state=snapshot.latest_qc_review_state,
            input_manifest_verified=True,
            metric_count=len(metric_lineages),
            metric_artifact_types=(target.artifact_type for target, _lineage in metric_targets),
            subject_count=len(
                {lineage.subject_id for lineage in metric_lineages if lineage.subject_id}
            ),
            mask_count=len(dependency_artifact_ids),
            verified_artifact_count=len(target_artifacts) + len(dependency_artifact_ids),
        )
        return VerifiedAnalysisOutboxSource(
            conversation_id=outbox.conversation_id,
            project_id=outbox.project_id,
            intent_task_id=outbox.intent_task_id,
            preprocessing_task_id=outbox.preprocessing_task_id,
            retry_generation=outbox.retry_generation,
            analysis_request=str(intent["analysis_request"]).strip(),
            run_id=outbox.run_id,
            plan_revision_id=outbox.plan_revision_id,
            manifest_hash=outbox.manifest_hash,
            artifact_ids=outbox.artifact_ids,
            dependency_artifact_ids=tuple(sorted(dependency_artifact_ids)),
            source_evidence_hash=content_hash(evidence),
            model_facts=model_facts,
        )

    def atomic(self) -> Any:
        """Expose the shared repository Unit of Work to composite app use cases."""

        return self._repository.atomic()

    def reconcile_next_due_task(
        self,
        worker_id: str,
        *,
        lease_seconds: int,
        cooldown_seconds: float,
    ) -> WorkflowRunReconciliation | None:
        """Claim and reconcile one due Run wait; the lease only fences task writes."""

        claim = self._repository.claim_next_reconciliation_task(
            worker_id,
            lease_seconds=lease_seconds,
            cooldown_seconds=cooldown_seconds,
        )
        if claim is None:
            return None
        try:
            return self.reconcile_waiting_run(claim.task_id, claim_token=claim.claim_token)
        finally:
            self._repository.release_reconciliation_claim(
                claim.task_id,
                claim.worker_id,
                claim.claim_token,
            )

    @staticmethod
    def analysis_proposal_task_id(conversation_id: str, proposal_hash: str) -> str:
        """Return the historical standalone/single-target task identity."""

        binding = f"{conversation_id}\0analysis\0{proposal_hash}".encode()
        return f"work-analysis-{hashlib.sha256(binding).hexdigest()[:32]}"

    @staticmethod
    def composite_analysis_proposal_task_id(
        conversation_id: str, proposal_hash: str, intent_task_id: str
    ) -> str:
        """Return a stable task identity scoped to one exact composite intent."""

        binding = (
            f"{conversation_id}\0analysis-composite\0{intent_task_id}\0{proposal_hash}"
        ).encode()
        return f"work-analysis-{hashlib.sha256(binding).hexdigest()[:32]}"

    @classmethod
    def _analysis_proposal_task_identity_matches(
        cls, task: WorkTask, conversation_id: str, proposal_hash: str
    ) -> bool:
        if task.task_id == cls.analysis_proposal_task_id(conversation_id, proposal_hash):
            # The legacy identity remains valid for standalone work and for
            # composite tasks persisted before intent-scoped IDs were added.
            return True
        intent_task_id = task.authority_refs.analysis_intent_task_id
        return bool(
            intent_task_id is not None
            and task.task_id
            == cls.composite_analysis_proposal_task_id(
                conversation_id, proposal_hash, intent_task_id
            )
        )

    def _conversation_bound_analysis_task(
        self, conversation: Any, proposal_hash: str
    ) -> WorkTask | None:
        """Resolve only the durable analysis role binding, never a guessed intent."""

        candidates = tuple(
            item
            for item in getattr(conversation, "work_tasks", ())
            if getattr(getattr(item, "agent_role", None), "value", None) == "analysis"
            and getattr(item, "proposal_hash", None) == proposal_hash
            and isinstance(getattr(item, "work_task_id", None), str)
        )
        if len(candidates) > 1:
            raise ConflictError(
                "work_task_analysis_binding_ambiguous",
                "会话中存在多个匹配该提案哈希的统计任务绑定。",
            )
        if not candidates:
            return None
        task_id = candidates[0].work_task_id
        try:
            task = self._repository.get_task(task_id)
        except NotFoundError as exc:
            raise ConflictError(
                "work_task_analysis_binding_missing",
                "会话引用的统计 WorkTask 不存在，不能另建重复任务。",
            ) from exc
        step = self._statistical_step(task)
        if (
            task.authority_refs.conversation_id != conversation.conversation_id
            or step is None
            or step.input_version != proposal_hash
            or not self._analysis_proposal_task_identity_matches(
                task, conversation.conversation_id, proposal_hash
            )
        ):
            raise ConflictError(
                "work_task_analysis_binding_mismatch",
                "会话统计任务引用与持久 WorkTask 的会话或提案身份不一致。",
            )
        return task

    def bind_ready_step_plan_for_review(
        self,
        claim: StepPreparationClaim,
        *,
        project_id: str,
        plan_revision_id: str,
    ) -> WorkTask:
        """Persist an authoritative Plan on the claimed READY step for review.

        The Work conversation, exact Plan record, project and input lineage are
        checked in the same Unit of Work as the task transition. Statistical
        Plans also carry a verifiable conversation identity; preprocessing
        PlanRevision has no conversation field, so its identity is established
        by the exact selected revision plus project and frozen manifest checks.
        The claim fences the exact Task/Step/input snapshot; this method never
        approves the Plan or authorizes a Run.
        """

        if not project_id or not plan_revision_id:
            raise InputValidationError(
                "step_preparation_plan_identity_required",
                "步骤方案绑定需要明确的项目和 Plan 修订 ID。",
            )
        with self.atomic():
            task = self._repository.get_task(claim.task_id)
            try:
                target = resolve_step_preparation_target(task)
            except ValueError as exc:
                raise ConflictError(
                    "step_preparation_sequence_invalid",
                    "步骤顺序在准备期间变为无效；方案没有绑定。",
                    task_id=claim.task_id,
                ) from exc
            if (
                target is None
                or target.task_id != claim.task_id
                or target.task_version != claim.task_version
                or target.step_id != claim.step_id
                or target.step_kind != claim.step_kind
                or target.step_version != claim.step_version
                or target.input_fingerprint != claim.input_fingerprint
            ):
                raise ConflictError(
                    "step_preparation_claim_lost",
                    "步骤准备期间任务、步骤或输入已变化；方案没有绑定。",
                    task_id=claim.task_id,
                    step_id=claim.step_id,
                )
            step = next((item for item in task.steps if item.step_id == claim.step_id), None)
            conversation_id = task.authority_refs.conversation_id
            if step is None or step.kind != claim.step_kind or conversation_id is None:
                raise ConflictError(
                    "step_preparation_identity_mismatch",
                    "步骤准备目标缺少可核验的会话或步骤身份。",
                    task_id=claim.task_id,
                    step_id=claim.step_id,
                )
            if step.authority_refs.conversation_id != conversation_id:
                raise ConflictError(
                    "step_preparation_conversation_mismatch",
                    "步骤没有绑定到任务所属的 Work 会话。",
                    task_id=claim.task_id,
                    step_id=claim.step_id,
                )

            conversation = self._conversation_repository.get_conversation(conversation_id)
            self._require_work_conversation(conversation, conversation_id, project_id)
            plan = self._plan_repository.get_plan(plan_revision_id)
            if plan.project_id != project_id:
                raise ConflictError(
                    "step_preparation_plan_project_mismatch",
                    "步骤 Plan 不属于当前 Work 项目。",
                    plan_revision_id=plan_revision_id,
                )

            expected_artifacts: tuple[DpabiPreprocessingOutput, ...] = ()
            if step.kind == "preprocessing_run":
                skill_plan = plan.plan.get("skill_plan")
                if (
                    plan.plan.get("kind") != "skill_plan"
                    or not isinstance(skill_plan, dict)
                    or not isinstance(skill_plan.get("preprocessing_parameters_hash"), str)
                    or plan.state not in {PlanState.AWAITING_APPROVAL, PlanState.APPROVED}
                ):
                    raise ConflictError(
                        "step_preparation_preprocessing_plan_invalid",
                        "预处理步骤只能绑定可审查的冻结 SkillPlan。",
                        plan_revision_id=plan_revision_id,
                    )
                try:
                    expected_artifacts = self._derive_preprocessing_expectations(plan, project_id)
                except (
                    NotFoundError,
                    ValueError,
                    TypeError,
                    KeyError,
                    AttributeError,
                    ValidationError,
                ) as exc:
                    raise ConflictError(
                        "step_preparation_preprocessing_plan_invalid",
                        "预处理 Plan 的冻结输入或目标产物无法核验。",
                        plan_revision_id=plan_revision_id,
                    ) from exc
                input_revision = plan.manifest_hash
                waiting_reason = (
                    f"预处理步骤 {step.step_id} 的 SkillPlan 已准备；等待用户审查并明确批准与启动。"
                )
            elif step.kind == "statistical_run":
                proposal_hash = step.input_version
                if proposal_hash is None:
                    raise ConflictError(
                        "step_preparation_statistical_identity_missing",
                        "统计步骤缺少与提案绑定的输入版本。",
                        step_id=step.step_id,
                    )
                self._validate_hash(proposal_hash, "proposal_hash")
                context, _design = self._statistical_plan_context(
                    plan,
                    conversation_id=conversation_id,
                    project_id=project_id,
                    proposal_hash=proposal_hash,
                )
                if plan.state not in {
                    PlanState.DRAFT,
                    PlanState.AWAITING_APPROVAL,
                    PlanState.APPROVED,
                }:
                    raise ConflictError(
                        "step_preparation_plan_not_reviewable",
                        "统计 Plan 当前不能进入用户审查。",
                        plan_revision_id=plan_revision_id,
                    )
                input_revision = context["draft_hash"]
                waiting_reason = (
                    f"统计步骤 {step.step_id} 的设计 Plan 已准备；等待用户审查并明确批准与启动。"
                )
            else:
                raise ConflictError(
                    "step_preparation_kind_unsupported",
                    "该 TaskStep 类型没有已注册的 Plan 准备合同。",
                    step_kind=step.kind,
                )

            try:
                updated = bind_prepared_step_plan_for_review(
                    task,
                    step,
                    plan_revision_id=plan_revision_id,
                    input_revision=input_revision,
                    waiting_reason=waiting_reason,
                    expected_artifacts=expected_artifacts,
                )
            except ValueError as exc:
                raise ConflictError(
                    "step_preparation_plan_binding_invalid",
                    "当前 READY 步骤不能绑定该 Plan；任务仍保持原状态。",
                    task_id=task.task_id,
                    step_id=step.step_id,
                    reason=str(exc),
                ) from exc
            return self._repository.save_task(
                updated,
                expected_version=task.version,
                step_preparation_claim=claim,
            )

    def ensure_analysis_proposal_task(
        self,
        conversation_id: str,
        project_id: str,
        proposal_hash: str,
        goal_description: str,
        draft_hash: str,
        *,
        analysis_intent_task_id: str | None = None,
        analysis_request_hash: str | None = None,
        source_run_id: str | None = None,
        input_manifest_hash: str | None = None,
    ) -> WorkTask:
        """Create/reuse the durable task for one reviewed statistical proposal."""

        self._validate_hash(proposal_hash, "proposal_hash")
        self._validate_hash(draft_hash, "draft_hash")
        conversation = self._conversation_repository.get_conversation(conversation_id)
        self._require_work_conversation(conversation, conversation_id, project_id)
        description = goal_description.strip()[:4000]
        if not description:
            raise InputValidationError("work_task_goal_required", "持久任务缺少用户目标描述。")

        legacy_task_id = self.analysis_proposal_task_id(conversation_id, proposal_hash)
        try:
            legacy_current = self._repository.get_task(legacy_task_id)
        except NotFoundError:
            legacy_current = None
        bound_current = self._conversation_bound_analysis_task(conversation, proposal_hash)
        current = bound_current or legacy_current
        if current is not None and analysis_intent_task_id is None:
            # Reuse the exact persisted association; the role slot above is a
            # durable task pointer, not an inference from recent conversation text.
            analysis_intent_task_id = current.authority_refs.analysis_intent_task_id
            analysis_request_hash = current.authority_refs.analysis_request_hash
        if (analysis_intent_task_id is None) != (analysis_request_hash is None):
            raise ConflictError(
                "composite_goal_intent_incomplete",
                "统计任务的复合 intent ID 与请求摘要必须同时存在。",
            )
        snapshot = None
        if analysis_intent_task_id is not None:
            self._validate_hash(analysis_request_hash or "", "analysis_request_hash")
            snapshot = self._repository.get_composite_goal_snapshot(analysis_intent_task_id)
            goal = snapshot.goal
            if (
                goal.conversation_id != conversation_id
                or goal.project_id != project_id
                or goal.analysis_request_hash != analysis_request_hash
            ):
                raise ConflictError(
                    "composite_goal_analysis_scope_mismatch",
                    "统计提案的会话、项目或请求摘要不属于该复合目标。",
                )
            if snapshot.analysis_proposal_outbox is None:
                raise ConflictError(
                    "composite_goal_proposal_outbox_missing",
                    "统计提案不能绑定到尚未建立精确预处理 outbox 的目标。",
                )
            outbox = snapshot.analysis_proposal_outbox
            if source_run_id is not None and source_run_id != outbox.run_id:
                raise ConflictError(
                    "composite_goal_proposal_source_mismatch",
                    "统计提案使用的预处理 Run 与复合目标来源不一致。",
                )
            if input_manifest_hash is not None and input_manifest_hash != outbox.manifest_hash:
                raise ConflictError(
                    "composite_goal_proposal_manifest_mismatch",
                    "统计提案使用的 manifest 与复合目标冻结输入不一致。",
                )
            legacy_matches_intent = bool(
                legacy_current is not None
                and legacy_current.authority_refs.conversation_id == conversation_id
                and legacy_current.authority_refs.analysis_intent_task_id == analysis_intent_task_id
                and legacy_current.authority_refs.analysis_request_hash == analysis_request_hash
            )
            bound_matches_intent = bool(
                bound_current is not None
                and bound_current.authority_refs.analysis_intent_task_id == analysis_intent_task_id
                and bound_current.authority_refs.analysis_request_hash == analysis_request_hash
            )
            if bound_current is not None and bound_matches_intent:
                current = bound_current
                task_id = bound_current.task_id
            elif legacy_current is not None and legacy_matches_intent:
                current = legacy_current
                task_id = legacy_task_id
            else:
                task_id = self.composite_analysis_proposal_task_id(
                    conversation_id, proposal_hash, analysis_intent_task_id
                )
                try:
                    current = self._repository.get_task(task_id)
                except NotFoundError:
                    current = None
                if current is not None and (
                    current.authority_refs.conversation_id != conversation_id
                    or current.authority_refs.analysis_intent_task_id != analysis_intent_task_id
                    or current.authority_refs.analysis_request_hash != analysis_request_hash
                ):
                    raise ConflictError(
                        "work_task_composite_goal_conflict",
                        "intent-scoped 统计任务 ID 已绑定到不同复合目标。",
                    )
            if current is None and (source_run_id is None or input_manifest_hash is None):
                raise ConflictError(
                    "composite_goal_proposal_source_missing",
                    "新的统计尝试必须显式绑定预处理 Run 和 manifest。",
                )
        else:
            task_id = (
                current.task_id
                if bound_current is not None and current is bound_current
                else legacy_task_id
            )
        task = WorkTask(
            task_id=task_id,
            goal=WorkGoal(
                description=description,
                completion_conditions=(
                    "统计 Run 已成功，并且存在唯一、可复核、与冻结统计设计和 Plan 哈希一致的"
                    "真实 StatisticalResult；零显著体素也是有效结果。",
                ),
            ),
            status=TaskStatus.WAITING_USER,
            waiting_condition=WaitingCondition(
                kind=WaitingKind.USER_DECISION,
                reference=f"proposal:{proposal_hash}",
                reason="统计提案已生成，等待用户审查并确认冻结分析方案。",
            ),
            authority_refs={
                "conversation_id": conversation_id,
                "analysis_intent_task_id": analysis_intent_task_id,
                "analysis_request_hash": analysis_request_hash,
                "input_revision": draft_hash,
            },
            steps=(
                TaskStep(
                    step_id=f"statistical:{proposal_hash}",
                    sequence=0,
                    kind="statistical_run",
                    status=TaskStepStatus.READY,
                    input_version=proposal_hash,
                    authority_refs={
                        "conversation_id": conversation_id,
                        "analysis_intent_task_id": analysis_intent_task_id,
                        "analysis_request_hash": analysis_request_hash,
                        "input_revision": draft_hash,
                    },
                ),
            ),
            budget=TaskBudget(
                max_steps=100,
                max_model_calls=40,
                max_tokens=200_000,
                max_elapsed_seconds=86_400,
            ),
        )
        with self.atomic():
            if current is None:
                current = self._repository.create_task(task)
            elif (
                current.authority_refs.analysis_intent_task_id != analysis_intent_task_id
                or current.authority_refs.analysis_request_hash != analysis_request_hash
            ):
                raise ConflictError(
                    "work_task_composite_goal_conflict",
                    "同一统计提案不能切换到不同的复合目标身份。",
                )
            current_step = self._statistical_step(current)
            if (
                current.authority_refs.conversation_id != conversation_id
                or current.authority_refs.input_revision != draft_hash
                or current_step is None
                or current_step.input_version != proposal_hash
                or current_step.authority_refs.conversation_id != conversation_id
                or current_step.authority_refs.input_revision != draft_hash
            ):
                raise ConflictError(
                    "work_task_binding_conflict", "同一统计提案已绑定到不同草稿或会话。"
                )
            if snapshot is not None:
                self._repository.bind_composite_goal_analysis_task(
                    snapshot.goal, task_id, proposal_hash
                )
        return current

    def bind_statistical_plan(
        self,
        task_id: str,
        conversation_id: str,
        project_id: str,
        proposal_hash: str,
        plan_revision_id: str,
    ) -> WorkTask:
        """Bind the exact frozen statistical Plan while keeping the user gate."""

        conversation = self._conversation_repository.get_conversation(conversation_id)
        self._require_work_conversation(conversation, conversation_id, project_id)
        plan = self._plan_repository.get_plan(plan_revision_id)
        if plan.project_id != project_id:
            raise ConflictError("work_task_plan_project_mismatch", "统计方案不属于当前项目。")
        context, design = self._statistical_plan_context(
            plan,
            conversation_id=conversation_id,
            project_id=project_id,
            proposal_hash=proposal_hash,
        )
        if plan.state not in {
            PlanState.DRAFT,
            PlanState.AWAITING_APPROVAL,
            PlanState.APPROVED,
        }:
            raise ConflictError("work_task_plan_not_reviewable", "统计方案尚未冻结供用户审查。")

        current = self._repository.get_task(task_id)
        step = self._statistical_step(current)
        if (
            not self._analysis_proposal_task_identity_matches(
                current, conversation_id, proposal_hash
            )
            or current.authority_refs.conversation_id != conversation_id
            or current.authority_refs.input_revision != context["draft_hash"]
            or step is None
            or step.input_version != proposal_hash
            or step.authority_refs.conversation_id != conversation_id
            or step.authority_refs.input_revision != context["draft_hash"]
        ):
            raise ConflictError("work_task_binding_mismatch", "统计任务与当前提案草稿不匹配。")
        self._require_active_composite_analysis_task(current, proposal_hash)
        if current.authority_refs.plan_revision_id not in {None, plan_revision_id}:
            raise ConflictError("work_task_plan_already_bound", "统计任务已绑定另一冻结方案。")
        if step.authority_refs.plan_revision_id not in {None, plan_revision_id}:
            raise ConflictError("work_task_plan_already_bound", "统计步骤已绑定另一冻结方案。")
        if current.authority_refs.plan_revision_id == plan_revision_id:
            return current
        if current.status is not TaskStatus.WAITING_USER or step.status is not TaskStepStatus.READY:
            raise ConflictError("work_task_not_waiting_for_plan", "统计任务当前不能绑定新方案。")

        updated_step = step.model_copy(
            update={
                "authority_refs": step.authority_refs.model_copy(
                    update={"plan_revision_id": plan_revision_id}
                ),
            }
        )
        updated = WorkTask.model_validate(
            {
                **current.model_dump(),
                "version": current.version + 1,
                "authority_refs": current.authority_refs.model_copy(
                    update={"plan_revision_id": plan_revision_id}
                ).model_dump(),
                "waiting_condition": WaitingCondition(
                    kind=WaitingKind.USER_DECISION,
                    reference=f"plan:{plan_revision_id}",
                    reason=(f"统计方案已冻结，设计 {design.revision_id}；等待用户审查并确认启动。"),
                ).model_dump(),
                "steps": tuple(
                    updated_step if item.step_id == step.step_id else item for item in current.steps
                ),
            }
        )
        return self._repository.save_task(updated, expected_version=current.version)

    def bind_statistical_run(
        self,
        task_id: str,
        conversation_id: str,
        project_id: str,
        plan_revision_id: str,
        run_id: str,
    ) -> WorkTask:
        """Bind the Run created by the user's authorized statistics action."""

        conversation = self._conversation_repository.get_conversation(conversation_id)
        self._require_work_conversation(conversation, conversation_id, project_id)
        plan = self._plan_repository.get_plan(plan_revision_id)
        if plan.project_id != project_id:
            raise ConflictError("work_task_plan_project_mismatch", "统计方案不属于当前项目。")
        task = self._repository.get_task(task_id)
        step = self._statistical_step(task)
        if step is None or task.authority_refs.conversation_id != conversation_id:
            raise ConflictError("work_task_binding_mismatch", "统计任务与当前会话不匹配。")
        proposal_hash = step.input_version
        if not proposal_hash:
            raise ConflictError("work_task_binding_mismatch", "统计任务缺少已审查提案哈希。")
        self._require_active_composite_analysis_task(task, proposal_hash)
        context, _design = self._statistical_plan_context(
            plan,
            conversation_id=conversation_id,
            project_id=project_id,
            proposal_hash=proposal_hash,
        )
        if plan.state is not PlanState.APPROVED:
            raise ConflictError(
                "work_task_plan_not_approved", "只能将已批准并授权执行的统计方案绑定到 Run。"
            )
        if (
            not self._analysis_proposal_task_identity_matches(task, conversation_id, proposal_hash)
            or task.authority_refs.input_revision != context["draft_hash"]
            or task.authority_refs.plan_revision_id != plan_revision_id
            or step.authority_refs.conversation_id != conversation_id
            or step.authority_refs.input_revision != context["draft_hash"]
            or step.authority_refs.plan_revision_id != plan_revision_id
        ):
            raise ConflictError("work_task_binding_mismatch", "统计任务与冻结方案不匹配。")
        run = self._run_repository.get_run(run_id)
        if run.project_id != project_id or run.plan_revision_id != plan_revision_id:
            raise ConflictError("work_task_run_plan_mismatch", "统计 Run 与冻结方案或项目不匹配。")
        if task.authority_refs.workflow_run_id is not None:
            try:
                return bind_step_to_run(
                    task,
                    step,
                    run_id=run_id,
                    waiting_reason="用户已确认启动统计分析；等待该统计 Workflow Run 和结果登记。",
                )
            except ValueError as exc:
                raise ConflictError(
                    "work_task_run_already_bound", "统计任务已绑定另一 Run 或绑定状态不一致。"
                ) from exc
        if task.status is not TaskStatus.WAITING_USER or step.status is not TaskStepStatus.READY:
            raise ConflictError("work_task_not_waiting_for_start", "统计任务当前不在等待启动状态。")
        updated = bind_step_to_run(
            task,
            step,
            run_id=run_id,
            waiting_reason="用户已确认启动统计分析；等待该统计 Workflow Run 和结果登记。",
        )
        return self._repository.save_task(updated, expected_version=task.version)

    @staticmethod
    def _validate_hash(value: str, field: str) -> None:
        if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
            raise InputValidationError("work_task_hash_invalid", f"{field} 必须是 SHA-256 哈希。")

    def _require_active_composite_analysis_task(self, task: WorkTask, proposal_hash: str) -> None:
        intent_id = task.authority_refs.analysis_intent_task_id
        request_hash = task.authority_refs.analysis_request_hash
        if intent_id is None and request_hash is None:
            return
        if intent_id is None or request_hash is None:
            raise ConflictError(
                "composite_goal_analysis_identity_incomplete",
                "统计任务的复合目标身份不完整。",
            )
        snapshot = self._repository.get_composite_goal_snapshot(intent_id)
        attempt = next(
            (item for item in snapshot.analysis_attempts if item.work_task_id == task.task_id),
            None,
        )
        if (
            snapshot.goal.conversation_id != task.authority_refs.conversation_id
            or snapshot.goal.analysis_request_hash != request_hash
            or snapshot.goal.analysis_work_task_id != task.task_id
            or attempt is None
            or attempt.proposal_hash != proposal_hash
        ):
            raise ConflictError(
                "composite_goal_analysis_attempt_superseded",
                "该统计提案已经不是复合目标的当前尝试，不能继续绑定方案或运行。",
            )

    @staticmethod
    def _require_work_conversation(
        conversation: Any, conversation_id: str, project_id: str
    ) -> None:
        if conversation.mode is not ConversationMode.WORK:
            raise InputValidationError(
                "work_task_conversation_mode_required", "持久统计任务需要 Work 会话。"
            )
        if conversation.conversation_id != conversation_id:
            raise ConflictError("work_task_conversation_mismatch", "会话读取结果与请求 ID 不一致。")
        if conversation.project_id not in {None, project_id}:
            raise ConflictError("work_task_project_mismatch", "Work 会话已绑定其他项目。")

    @staticmethod
    def _statistical_step(task: WorkTask) -> TaskStep | None:
        steps = tuple(step for step in task.steps if step.kind == "statistical_run")
        return steps[0] if len(steps) == 1 else None

    @staticmethod
    def _statistical_plan_context(
        plan: Any,
        *,
        conversation_id: str,
        project_id: str,
        proposal_hash: str,
    ) -> tuple[dict[str, Any], StatisticalDesignRevision]:
        context = plan.plan.get("work_context")
        raw_design = plan.plan.get("design")
        if (
            plan.project_id != project_id
            or plan.plan.get("kind") != "statistical_design"
            or not isinstance(context, dict)
            or not isinstance(raw_design, dict)
            or context.get("conversation_id") != conversation_id
            or context.get("proposal_hash") != proposal_hash
            or not isinstance(context.get("draft_hash"), str)
        ):
            raise ConflictError(
                "work_task_statistical_plan_mismatch", "统计方案 work_context 与提案任务不匹配。"
            )
        WorkTaskApplicationService._validate_hash(context["draft_hash"], "draft_hash")
        try:
            design = StatisticalDesignRevision.model_validate(raw_design)
        except ValidationError as exc:
            raise ConflictError(
                "work_task_statistical_plan_invalid", "冻结统计方案中的 design 无法核验。"
            ) from exc
        expected_plan_hash = content_hash(
            {
                "plan": plan.plan,
                "manifest_hash": plan.manifest_hash,
                "environment_hash": plan.environment_hash,
            }
        )
        if expected_plan_hash != plan.plan_hash:
            raise ConflictError(
                "work_task_statistical_plan_hash_mismatch",
                "统计方案内容与冻结 Plan 哈希不一致。",
            )
        return context, design

    @staticmethod
    def preprocessing_task_id(conversation_id: str, plan_revision_id: str) -> str:
        """Return a stable key for one preprocessing Plan in one Work conversation."""

        binding = f"{conversation_id}\0preprocessing\0{plan_revision_id}".encode()
        return f"work-preprocessing-{hashlib.sha256(binding).hexdigest()[:32]}"

    def ensure_preprocessing_plan_task(
        self,
        *,
        conversation_id: str,
        project_id: str,
        plan_revision_id: str,
        goal_description: str,
        analysis_intent_task_id: str | None = None,
        analysis_request_hash: str | None = None,
    ) -> WorkTask:
        """Persist a user-reviewed preprocessing goal after its Plan commits.

        The caller runs this from the Work turn's final SQLite unit of work.
        Repeated calls for the same conversation/Plan are idempotent; a stable
        key that resolves to different authority references is treated as a
        binding conflict.
        """

        if (analysis_intent_task_id is None) != (analysis_request_hash is None):
            raise ConflictError(
                "composite_goal_intent_incomplete",
                "预处理任务的复合 intent ID 与请求摘要必须同时存在。",
            )
        if analysis_request_hash is not None:
            self._validate_hash(analysis_request_hash, "analysis_request_hash")

        conversation = self._conversation_repository.get_conversation(conversation_id)
        if conversation.mode is not ConversationMode.WORK:
            raise InputValidationError(
                "work_task_conversation_mode_required", "持久预处理任务需要 Work 会话。"
            )
        if conversation.project_id not in {None, project_id}:
            raise ConflictError(
                "work_task_project_mismatch", "Work 会话已绑定其他项目，不能关联此预处理方案。"
            )
        plan = self._plan_repository.get_plan(plan_revision_id)
        if plan.project_id != project_id:
            raise ConflictError(
                "work_task_plan_project_mismatch", "预处理方案不属于当前 Work 项目。"
            )
        if plan.state not in {PlanState.AWAITING_APPROVAL, PlanState.APPROVED}:
            raise ConflictError(
                "work_task_plan_not_reviewable", "只有成功冻结的预处理方案可以创建持久任务。"
            )
        skill_plan = plan.plan.get("skill_plan")
        if plan.plan.get("kind") != "skill_plan" or not isinstance(skill_plan, dict):
            raise ConflictError(
                "work_task_preprocessing_plan_required", "当前方案不是可追溯的预处理 SkillPlan。"
            )
        if not isinstance(skill_plan.get("preprocessing_parameters_hash"), str):
            raise ConflictError(
                "work_task_preprocessing_plan_required", "当前 SkillPlan 未请求预处理。"
            )
        try:
            expected_artifacts = self._derive_preprocessing_expectations(plan, project_id)
        except (NotFoundError, ValueError, TypeError, KeyError, AttributeError) as exc:
            raise ConflictError(
                "work_task_preprocessing_plan_invalid",
                "冻结预处理方案或数据清单无法通过目标产物核验。",
            ) from exc
        description = goal_description.strip()[:4000]
        if not description:
            raise InputValidationError("work_task_goal_required", "持久任务缺少用户目标描述。")

        task_id = self.preprocessing_task_id(conversation_id, plan_revision_id)
        outputs = tuple(
            str(value)
            for value in skill_plan.get("primary_outputs", ())
            if isinstance(value, str) and value.strip()
        )
        metrics = tuple(
            str(value)
            for value in skill_plan.get("requested_metrics", ())
            if isinstance(value, str) and value.strip()
        )
        expectations = tuple(
            str(value)
            for value in skill_plan.get("artifact_expectations", ())
            if isinstance(value, str) and value.strip()
        )
        targets = outputs or metrics or expectations
        completion_conditions = [
            "该 Plan 对应的 Workflow Run 达到 succeeded。",
            "用户启动确认选择的 QC 要求由权威记录满足：要求 QC 时存在 approved 审查；"
            "明确跳过时存在 QC skip 记录。",
        ]
        if targets:
            completion_conditions.append(
                "该 Run 已登记此 Plan 要求的目标产物：" + "、".join(targets) + "。"
            )
        else:
            completion_conditions.append("该 Run 已登记该预处理 Plan 声明的目标产物。")

        task = WorkTask(
            task_id=task_id,
            goal=WorkGoal(
                description=description,
                completion_conditions=tuple(completion_conditions),
            ),
            status=TaskStatus.WAITING_USER,
            waiting_condition=WaitingCondition(
                kind=WaitingKind.USER_DECISION,
                reference=f"plan:{plan_revision_id}",
                reason="预处理方案已提交，等待用户审查并明确启动。",
            ),
            authority_refs={
                "conversation_id": conversation_id,
                "analysis_intent_task_id": analysis_intent_task_id,
                "analysis_request_hash": analysis_request_hash,
                "plan_revision_id": plan_revision_id,
                "input_revision": plan.manifest_hash,
            },
            steps=(
                TaskStep(
                    step_id=f"preprocessing:{plan_revision_id}",
                    sequence=0,
                    kind="preprocessing_run",
                    status=TaskStepStatus.READY,
                    input_version=plan.plan_hash,
                    authority_refs={
                        "conversation_id": conversation_id,
                        "analysis_intent_task_id": analysis_intent_task_id,
                        "analysis_request_hash": analysis_request_hash,
                        "plan_revision_id": plan_revision_id,
                        "input_revision": plan.manifest_hash,
                    },
                    expected_artifacts=expected_artifacts,
                ),
            ),
            budget=TaskBudget(
                max_steps=100,
                max_model_calls=40,
                max_tokens=200_000,
                max_elapsed_seconds=86_400,
            ),
        )
        try:
            current = self._repository.get_task(task_id)
        except NotFoundError:
            created = self._repository.create_task(task)
            if analysis_intent_task_id is not None or analysis_request_hash is not None:
                self._repository.ensure_preprocessing_composite_goal(created)
            return created
        if (
            current.authority_refs.conversation_id != conversation_id
            or current.authority_refs.plan_revision_id != plan_revision_id
            or current.authority_refs.input_revision not in {None, plan.manifest_hash}
        ):
            raise ConflictError(
                "work_task_binding_conflict", "同一 Plan 已绑定到其他会话的持久任务。"
            )
        if (
            current.authority_refs.analysis_intent_task_id is not None
            and analysis_intent_task_id is not None
            and current.authority_refs.analysis_intent_task_id != analysis_intent_task_id
        ):
            raise ConflictError(
                "work_task_analysis_intent_conflict", "预处理任务已绑定另一条复合分析意图。"
            )
        if (
            current.authority_refs.analysis_request_hash is not None
            and analysis_request_hash is not None
            and current.authority_refs.analysis_request_hash != analysis_request_hash
        ):
            raise ConflictError(
                "work_task_analysis_intent_conflict", "预处理任务绑定的分析请求摘要不一致。"
            )
        # An existing explicit composite intent is durable and remains attached
        # when the same frozen Plan is revisited through an ordinary start flow.
        if current.authority_refs.analysis_intent_task_id is None and (
            analysis_intent_task_id is not None or analysis_request_hash is not None
        ):
            current = self._repository.save_task(
                current.model_copy(
                    update={
                        "version": current.version + 1,
                        "authority_refs": current.authority_refs.model_copy(
                            update={"analysis_intent_task_id": analysis_intent_task_id}
                            | {"analysis_request_hash": analysis_request_hash}
                        ),
                        "steps": tuple(
                            step.model_copy(
                                update={
                                    "version": step.version + 1,
                                    "authority_refs": step.authority_refs.model_copy(
                                        update={
                                            "analysis_intent_task_id": analysis_intent_task_id,
                                            "analysis_request_hash": analysis_request_hash,
                                        }
                                    ),
                                }
                            )
                            if step.kind == "preprocessing_run"
                            else step
                            for step in current.steps
                        ),
                    }
                ),
                expected_version=current.version,
            )
        current_step = self._preprocessing_step(current)
        if current_step is None:
            raise ConflictError(
                "work_task_preprocessing_step_missing", "预处理 WorkTask 缺少唯一执行步骤。"
            )
        if (
            current_step.expected_artifacts
            and current_step.expected_artifacts != expected_artifacts
        ):
            raise ConflictError(
                "work_task_preprocessing_targets_mismatch",
                "持久任务中的预期产物与冻结预处理方案不一致。",
            )
        if (
            current.authority_refs.input_revision is None
            or current_step.authority_refs.input_revision is None
            or not current_step.expected_artifacts
        ):
            updated = current.model_copy(
                update={
                    "version": current.version + 1,
                    "authority_refs": current.authority_refs.model_copy(
                        update={"input_revision": plan.manifest_hash}
                    ),
                    "steps": tuple(
                        step.model_copy(
                            update={
                                "authority_refs": step.authority_refs.model_copy(
                                    update={"input_revision": plan.manifest_hash}
                                ),
                                "expected_artifacts": expected_artifacts,
                            }
                        )
                        if step.step_id == current_step.step_id
                        else step
                        for step in current.steps
                    ),
                }
            )
            return self._repository.save_task(updated, expected_version=current.version)
        if (
            current.authority_refs.analysis_intent_task_id is not None
            or current.authority_refs.analysis_request_hash is not None
        ):
            self._repository.ensure_preprocessing_composite_goal(current)
        return current

    def bind_preprocessing_run(
        self,
        *,
        task_id: str,
        conversation_id: str,
        project_id: str,
        plan_revision_id: str,
        run_id: str,
    ) -> WorkTask:
        """Bind a successfully queued Run to its existing conversation goal."""

        conversation = self._conversation_repository.get_conversation(conversation_id)
        if conversation.mode is not ConversationMode.WORK:
            raise InputValidationError(
                "work_task_conversation_mode_required", "持久预处理任务需要 Work 会话。"
            )
        if conversation.project_id not in {None, project_id}:
            raise ConflictError(
                "work_task_project_mismatch", "Work 会话已绑定其他项目，不能关联此运行。"
            )
        plan = self._plan_repository.get_plan(plan_revision_id)
        run = self._run_repository.get_run(run_id)
        skill_plan = plan.plan.get("skill_plan")
        if plan.plan.get("kind") != "skill_plan" or not isinstance(skill_plan, dict):
            raise ConflictError(
                "work_task_preprocessing_plan_required", "运行没有对应的预处理 SkillPlan。"
            )
        if not isinstance(skill_plan.get("preprocessing_parameters_hash"), str):
            raise ConflictError(
                "work_task_preprocessing_plan_required", "指标计算方案不能绑定为预处理任务。"
            )
        if plan.project_id != project_id or run.project_id != project_id:
            raise ConflictError(
                "work_task_run_project_mismatch", "运行或方案不属于当前 Work 项目。"
            )
        if run.plan_revision_id != plan_revision_id:
            raise ConflictError(
                "work_task_run_plan_mismatch", "运行没有使用当前 Work 任务绑定的预处理方案。"
            )
        task = self._repository.get_task(task_id)
        if (
            task.authority_refs.conversation_id != conversation_id
            or task.authority_refs.plan_revision_id != plan_revision_id
            or task.authority_refs.input_revision not in {None, plan.manifest_hash}
        ):
            raise ConflictError("work_task_binding_mismatch", "持久任务与当前会话或方案不匹配。")
        expected_id = self.preprocessing_task_id(conversation_id, plan_revision_id)
        if task_id != expected_id:
            raise ConflictError("work_task_binding_mismatch", "持久任务 ID 与权威绑定不匹配。")
        step = next(
            (
                item
                for item in task.steps
                if item.kind == "preprocessing_run"
                and item.authority_refs.plan_revision_id == plan_revision_id
            ),
            None,
        )
        if step is None:
            raise ConflictError("work_task_step_not_ready", "预处理任务步骤尚未准备启动。")
        if task.authority_refs.workflow_run_id is not None:
            try:
                return bind_step_to_run(
                    task,
                    step,
                    run_id=run_id,
                    waiting_reason="用户已确认启动；等待该预处理 Workflow Run 完成。",
                )
            except ValueError as exc:
                raise ConflictError(
                    "work_task_run_already_bound",
                    "该预处理任务已绑定其他 Run 或绑定状态不一致。",
                ) from exc
        if task.status is not TaskStatus.WAITING_USER:
            raise ConflictError(
                "work_task_not_waiting_for_start", "持久任务当前不在等待用户启动的状态。"
            )
        if step.status is not TaskStepStatus.READY:
            raise ConflictError("work_task_step_not_ready", "预处理任务步骤尚未准备启动。")
        updated = bind_step_to_run(
            task,
            step,
            run_id=run_id,
            waiting_reason="用户已确认启动；等待该预处理 Workflow Run 完成。",
        ).model_copy(
            update={
                "goal": WorkGoal(
                    description=task.goal.description,
                    completion_conditions=(
                        *tuple(
                            condition
                            for condition in task.goal.completion_conditions
                            if not condition.startswith("用户启动确认选择的 QC 要求")
                        ),
                        "该 Run 存在 approved 的头动 QC 审查记录。"
                        if run.qc_mode.value == "require"
                        else "该 Run 存在用户明确作出的 QC skip 记录。",
                    ),
                ),
            }
        )
        updated = WorkTask.model_validate(updated.model_dump())
        return self._repository.save_task(updated, expected_version=task.version)

    def transition_task(
        self,
        task_id: str,
        *,
        expected_version: int,
        target: TaskStatus,
        waiting_condition: WaitingCondition | None = None,
    ) -> WorkTask:
        current = self._repository.get_task(task_id)
        if current.version != expected_version:
            raise ConflictError(
                "revision_conflict",
                "自动化任务已更新。请重新读取后再继续。",
                resource="work_task",
                resource_id=task_id,
                expected=expected_version,
                actual=current.version,
            )
        updated = transition_task(
            current,
            target,
            waiting_condition=waiting_condition,
        )
        return self._repository.save_task(updated, expected_version=expected_version)

    def consume_workflow_event(
        self,
        task_id: str,
        event: WorkEventIdentity,
        *,
        claim_token: str | None = None,
    ) -> tuple[EventConsumptionDecision, WorkTask]:
        """Record a workflow notification once; workflow truth is reloaded elsewhere.

        This method deliberately does not interpret event payloads or copy Run,
        QC, or Artifact status. A successful receipt only advances the task's
        orchestration version and cursor so a later coordinator can reread the
        authoritative workflow records before deciding the next action.
        """

        current = self._repository.get_task(task_id)
        expected_run_id = current.authority_refs.workflow_run_id
        if len(current.steps) > 1:
            try:
                expected_run_id, _active_step = resolve_routed_workflow_run(current)
            except ValueError as exc:
                raise InputValidationError(
                    "workflow_event_route_unavailable",
                    "自动化任务当前没有唯一、有效的步骤运行事件路由。",
                    task_id=task_id,
                    reason=str(exc),
                ) from exc
        if expected_run_id is None or event.workflow_run_id != expected_run_id:
            raise InputValidationError(
                "workflow_event_run_mismatch",
                "运行事件与自动化任务绑定的运行不匹配。",
                task_id=task_id,
                expected_run_id=expected_run_id,
                event_run_id=event.workflow_run_id,
            )
        expected_stream_key = f"workflow:{expected_run_id}"
        if event.stream_key != expected_stream_key:
            raise InputValidationError(
                "workflow_event_stream_mismatch",
                "运行事件不属于该任务的标准 Workflow 事件游标。",
                task_id=task_id,
                expected_stream_key=expected_stream_key,
                event_stream_key=event.stream_key,
            )
        candidate = WorkTask.model_validate(
            {**current.model_dump(), "version": current.version + 1}
        )
        decision, _committed = self._repository.consume_event(
            task_id,
            event,
            expected_version=current.version,
            updated_task=candidate,
            claim_token=claim_token,
        )
        # Read after both applied and no-op decisions so a concurrent duplicate
        # returns current persisted state rather than a stale pre-read snapshot.
        return decision, self._repository.get_task(task_id)

    def reconcile_waiting_run(
        self,
        task_id: str,
        *,
        claim_token: str | None = None,
    ) -> WorkflowRunReconciliation:
        """Consume Run event identities, then reread Workflow, QC and Artifacts.

        Runtime event type and payload are deliberately ignored. Events only
        wake this use case and provide a stable identity for deduplication;
        readiness comes exclusively from the existing authoritative read ports.
        """

        task = self._repository.get_task(task_id)
        if task.authority_refs.workflow_run_id is None and len(task.steps) <= 1:
            raise InputValidationError(
                "workflow_run_reference_missing",
                "自动化任务没有绑定可核验的运行。",
                task_id=task_id,
            )
        try:
            run_id, bound_step = resolve_routed_workflow_run(task)
        except ValueError as exc:
            return WorkflowRunReconciliation(
                task=task,
                readiness=RunReadiness.INDETERMINATE,
                reason=f"任务步骤结构或 Run 引用无法唯一解析;未消费新事件。({exc})",
            )
        initial_bound_step = bound_step
        is_statistical = bound_step is not None and bound_step.kind == "statistical_run"
        is_waiting_for_bound_run = (
            task.status is TaskStatus.WAITING_RUN
            and task.waiting_condition is not None
            and task.waiting_condition.reference == run_id
        )
        is_waiting_for_statistical_result = (
            is_statistical
            and task.status is TaskStatus.WAITING_DEPENDENCY
            and task.waiting_condition is not None
            and task.waiting_condition.reference == f"statistical-result:{run_id}"
        )
        preprocessing_step = self._preprocessing_step(task)
        is_waiting_for_preprocessing_dependency = (
            preprocessing_step is not None
            and task.status is TaskStatus.WAITING_DEPENDENCY
            and task.waiting_condition is not None
            and task.waiting_condition.reference == f"downstream:{run_id}"
            and preprocessing_step.authority_refs.workflow_run_id == run_id
        )
        is_waiting_for_bound_qc = (
            not is_statistical
            and task.status is TaskStatus.WAITING_USER
            and task.waiting_condition is not None
            and task.waiting_condition.reference == f"qc:{run_id}"
        )
        if not (
            is_waiting_for_bound_run
            or is_waiting_for_bound_qc
            or is_waiting_for_statistical_result
            or is_waiting_for_preprocessing_dependency
        ):
            raise InputValidationError(
                "task_not_waiting_for_workflow_run",
                "自动化任务当前并未等待该运行或其 QC 决定。",
                task_id=task_id,
                task_status=task.status.value,
            )

        stream_key = f"workflow:{run_id}"
        cursor = next(
            (item.event_id for item in task.event_cursors if item.stream_key == stream_key),
            0,
        )
        events = self._event_repository.list_events(
            run_id=run_id,
            after_event_id=cursor,
            limit=1000,
        )
        # Validate the entire returned batch before consuming any identity so a
        # broken adapter cannot partially advance the task with a wrong-Run event.
        for event in events:
            if event.run_id != run_id:
                raise InputValidationError(
                    "workflow_event_run_mismatch",
                    "事件仓储返回了与任务绑定运行不一致的事件。",
                    task_id=task_id,
                    expected_run_id=run_id,
                    event_run_id=event.run_id,
                    event_id=event.event_id,
                )

        receipts: list[EventReconciliationReceipt] = []
        for event in sorted(events, key=lambda item: item.event_id)[:1000]:
            identity = WorkEventIdentity(
                stream_key=stream_key,
                workflow_run_id=run_id,
                event_id=event.event_id,
                dedupe_key=f"runtime-event:{event.event_id}",
            )
            decision, task = self.consume_workflow_event(
                task_id,
                identity,
                claim_token=claim_token,
            )
            receipts.append(EventReconciliationReceipt(event_id=event.event_id, decision=decision))

        # Consume can race with user/API state changes. Always use the persisted
        # task version before deciding whether this reconciliation may transition it.
        task = self._repository.get_task(task_id)
        try:
            current_run_id, bound_step = resolve_routed_workflow_run(task)
        except ValueError as exc:
            return WorkflowRunReconciliation(
                task=task,
                readiness=RunReadiness.INDETERMINATE,
                event_receipts=tuple(receipts),
                reason=f"事件消费后任务步骤结构或 Run 引用变化;本次不推进状态。({exc})",
            )
        if current_run_id != run_id or bound_step != initial_bound_step:
            return WorkflowRunReconciliation(
                task=task,
                readiness=RunReadiness.INDETERMINATE,
                event_receipts=tuple(receipts),
                reason="事件消费期间绑定步骤发生变化;本次只记录事件身份,需重新核验。",
            )
        is_statistical = bound_step is not None and bound_step.kind == "statistical_run"
        is_waiting_for_current_statistical_result = (
            is_statistical
            and task.status is TaskStatus.WAITING_DEPENDENCY
            and task.waiting_condition is not None
            and task.waiting_condition.reference == f"statistical-result:{run_id}"
        )
        is_waiting_for_current_run = (
            task.status is TaskStatus.WAITING_RUN
            and task.waiting_condition is not None
            and task.waiting_condition.reference == run_id
        )
        is_waiting_for_current_qc = (
            not is_statistical
            and task.status is TaskStatus.WAITING_USER
            and task.waiting_condition is not None
            and task.waiting_condition.reference == f"qc:{run_id}"
        )
        current_preprocessing_step = (
            bound_step
            if bound_step is not None and bound_step.kind == "preprocessing_run"
            else None
        )
        is_waiting_for_current_preprocessing_dependency = (
            current_preprocessing_step is not None
            and task.status is TaskStatus.WAITING_DEPENDENCY
            and task.waiting_condition is not None
            and task.waiting_condition.reference == f"downstream:{run_id}"
            and current_preprocessing_step.authority_refs.workflow_run_id == run_id
        )
        if not (
            is_waiting_for_current_statistical_result
            or is_waiting_for_current_run
            or is_waiting_for_current_qc
            or is_waiting_for_current_preprocessing_dependency
        ):
            return WorkflowRunReconciliation(
                task=task,
                readiness=RunReadiness.INDETERMINATE,
                event_receipts=tuple(receipts),
                reason="任务等待状态已并发变化;本次只记录事件身份,需重新读取后再协调。",
            )

        if len(task.steps) > 1:
            return WorkflowRunReconciliation(
                task=task,
                readiness=RunReadiness.INDETERMINATE,
                event_receipts=tuple(receipts),
                reason=(
                    "活动步骤 Run 事件已安全消费；多步骤 readiness 与完成收口尚未启用，"
                    "任务状态未推进。"
                ),
            )

        if is_statistical:
            return self._reconcile_statistical_run(
                task,
                run_id,
                tuple(receipts),
                claim_token=claim_token,
            )
        if bound_step is not None:
            return self._reconcile_preprocessing_run(
                task,
                bound_step,
                run_id,
                tuple(receipts),
                claim_token=claim_token,
            )

        try:
            stable_authority = self._read_stable_authoritative_snapshot(task, run_id)
        except NotFoundError:
            return WorkflowRunReconciliation(
                task=task,
                readiness=RunReadiness.INDETERMINATE,
                event_receipts=tuple(receipts),
                reason="绑定的 Run 或其权威记录暂不可读取;任务状态未推进。",
            )
        if stable_authority is None:
            return WorkflowRunReconciliation(
                task=task,
                readiness=RunReadiness.INDETERMINATE,
                event_receipts=tuple(receipts),
                reason=("Run 在三次权威状态读取期间都发生版本变化;保留任务原状态,稍后重新核验。"),
            )
        snapshot = stable_authority.snapshot
        try:
            run_state = WorkflowState(snapshot.run_state)
        except ValueError:
            return WorkflowRunReconciliation(
                task=task,
                readiness=RunReadiness.INDETERMINATE,
                event_receipts=tuple(receipts),
                authoritative=snapshot,
                reason="Workflow 返回了不受支持的状态；任务状态未推进。",
            )

        target_status: TaskStatus | None = None
        waiting_condition: WaitingCondition | None = None
        readiness = RunReadiness.INDETERMINATE
        reason = "当前 Workflow/QC 组合无法安全判定后续动作;保留任务状态供人工核验。"

        if snapshot.unresolved_referenced_artifact_ids:
            readiness = RunReadiness.BLOCKED
            target_status = TaskStatus.BLOCKED
            reason = "任务引用的 Artifact 不存在或不属于该项目;需先核验引用。"
        elif run_state in {
            WorkflowState.QUEUED,
            WorkflowState.RUNNING,
            WorkflowState.CANCELLING,
        }:
            readiness = RunReadiness.WAITING_RUN
            target_status = TaskStatus.WAITING_RUN
            waiting_condition = WaitingCondition(
                kind=WaitingKind.WORKFLOW_RUN,
                reference=run_id,
                reason="等待该 Workflow Run 进入可核验的终态。",
            )
            reason = "权威 Workflow 状态仍在排队、运行或取消中。"
        elif run_state is WorkflowState.QC_REVIEW:
            readiness = RunReadiness.WAITING_USER
            has_actionable_qc_review = (
                snapshot.qc_status in {"pending", "awaiting_review"}
                and snapshot.latest_qc_review_state in {None, "draft"}
            ) or (snapshot.qc_status == "failed" and snapshot.latest_qc_review_state == "draft")
            if has_actionable_qc_review:
                target_status = TaskStatus.WAITING_USER
                waiting_condition = WaitingCondition(
                    kind=WaitingKind.USER_DECISION,
                    reference=f"qc:{run_id}",
                    reason="Workflow 已到达 QC 决策边界;需要用户查看并明确处理 QC。",
                )
                reason = "QC 尚未明确批准或记录显式跳过;本次未创建或批准 QC。"
            else:
                readiness = RunReadiness.BLOCKED
                target_status = TaskStatus.BLOCKED
                reason = "Workflow 处于 QC_REVIEW,但 QC 状态或审查记录不一致。"
        elif run_state is WorkflowState.SUCCEEDED:
            if snapshot.qc_status == "approved" and snapshot.latest_qc_review_state == "approved":
                if current_preprocessing_step is not None:
                    return self._complete_preprocessing_task(
                        task,
                        run_id,
                        stable_authority,
                        tuple(receipts),
                        claim_token=claim_token,
                    )
                readiness = RunReadiness.WAITING_DEPENDENCY
                target_status = TaskStatus.WAITING_DEPENDENCY
                waiting_condition = WaitingCondition(
                    kind=WaitingKind.DEPENDENCY,
                    reference=f"downstream:{run_id}",
                    reason="运行与 QC 已核验;还需按目标检查所需 Artifact 和下一步骤。",
                )
                reason = (
                    "Workflow 已成功且权威 QC 审查为 approved;当前任务协议没有目标级"
                    " Artifact 完整性/下一步骤检查器,因此停在 waiting_dependency。"
                )
            elif snapshot.qc_status == "skipped" and snapshot.qc_skip_id is not None:
                if current_preprocessing_step is not None:
                    return self._complete_preprocessing_task(
                        task,
                        run_id,
                        stable_authority,
                        tuple(receipts),
                        claim_token=claim_token,
                    )
                readiness = RunReadiness.WAITING_DEPENDENCY
                target_status = TaskStatus.WAITING_DEPENDENCY
                waiting_condition = WaitingCondition(
                    kind=WaitingKind.DEPENDENCY,
                    reference=f"downstream:{run_id}",
                    reason="运行及显式 QC 跳过记录已核验;仍需检查目标级 Artifact 和下一步骤。",
                )
                reason = (
                    "Workflow 已成功且存在显式 QC 跳过记录;未隐含审批,仍需目标级"
                    " Artifact/下一步骤检查。"
                )
            elif snapshot.qc_status == "rejected" or snapshot.latest_qc_review_state == "rejected":
                readiness = RunReadiness.BLOCKED
                target_status = TaskStatus.BLOCKED
                reason = "权威 QC 记录为 rejected;任务已阻断,需用户决定后续处理。"
            elif snapshot.qc_status == "pending" and snapshot.latest_qc_review_state in {
                None,
                "draft",
            }:
                readiness = RunReadiness.WAITING_USER
                target_status = TaskStatus.WAITING_USER
                waiting_condition = WaitingCondition(
                    kind=WaitingKind.USER_DECISION,
                    reference=f"qc:{run_id}",
                    reason="运行已成功但 QC 仍 pending;等待用户检查或明确处理。",
                )
                reason = "运行成功但 QC 仍 pending;本次不会隐含批准或跳过。"
            else:
                readiness = RunReadiness.BLOCKED
                target_status = TaskStatus.BLOCKED
                reason = "运行成功,但 QC 状态与审查/跳过记录不一致,不能安全继续。"
        elif run_state in {WorkflowState.FAILED_RETRYABLE, WorkflowState.TIMED_OUT}:
            readiness = RunReadiness.WAITING_RUN
            target_status = TaskStatus.WAITING_RUN
            waiting_condition = WaitingCondition(
                kind=WaitingKind.WORKFLOW_RUN,
                reference=run_id,
                reason="Worker 正在按现有重试预算处理同一 Workflow Run。",
            )
            reason = (
                f"Workflow 暂处于 {run_state.value};任务继续等待 Worker 对同一 Run 的重试,"
                "不创建新 Run。"
            )
        elif run_state in {
            WorkflowState.FAILED_TERMINAL,
            WorkflowState.CANCELLED,
        }:
            readiness = RunReadiness.BLOCKED
            target_status = TaskStatus.BLOCKED
            reason = f"权威 Workflow 状态为 {run_state.value};本协调器不会创建新 Run 或重试。"

        updated_task = task
        if target_status is not None and target_status is not task.status:
            updated_task = transition_task(
                task,
                target_status,
                waiting_condition=waiting_condition,
            )
            updated_task = self._save_task(
                updated_task,
                expected_version=task.version,
                claim_token=claim_token,
            )
        elif (
            target_status
            in {
                TaskStatus.WAITING_RUN,
                TaskStatus.WAITING_USER,
                TaskStatus.WAITING_DEPENDENCY,
            }
            and task.status is target_status
            and task.waiting_condition != waiting_condition
        ):
            # Same-state changes are persisted through a versioned copy because
            # the pure transition graph intentionally disallows self-transitions.
            updated_task = WorkTask.model_validate(
                {
                    **task.model_dump(),
                    "waiting_condition": waiting_condition,
                    "version": task.version + 1,
                }
            )
            updated_task = self._save_task(
                updated_task,
                expected_version=task.version,
                claim_token=claim_token,
            )

        return WorkflowRunReconciliation(
            task=updated_task,
            readiness=readiness,
            event_receipts=tuple(receipts),
            authoritative=snapshot,
            reason=reason,
        )

    def _read_preprocessing_completion_evidence(
        self,
        task: WorkTask,
        run_id: str,
        authority: _StableRunAuthority,
    ) -> _PreprocessingCompletionEvidence:
        """Read and verify the frozen preprocessing target and its lineage."""

        step = self._preprocessing_step(task)
        plan_revision_id = task.authority_refs.plan_revision_id
        conversation_id = task.authority_refs.conversation_id
        snapshot = authority.snapshot
        if (
            step is None
            or plan_revision_id is None
            or conversation_id is None
            or task.authority_refs.workflow_run_id != run_id
            or authority.plan_revision_id != plan_revision_id
            or step.authority_refs.plan_revision_id != plan_revision_id
            or step.authority_refs.workflow_run_id != run_id
            or step.input_version is None
            or snapshot.run_id != run_id
            or snapshot.run_state != WorkflowState.SUCCEEDED.value
        ):
            raise _InvalidPreprocessingAuthority("WorkTask/step/Run authority references differ")

        if (
            snapshot.qc_status == "approved"
            and snapshot.latest_qc_review_state == "approved"
            and snapshot.latest_qc_review_id is not None
        ):
            qc_decision_kind = "approved"
            qc_decision_identity = snapshot.latest_qc_review_id
        elif snapshot.qc_status == "skipped" and snapshot.qc_skip_id is not None:
            qc_decision_kind = "skipped"
            qc_decision_identity = snapshot.qc_skip_id
        else:
            raise _InvalidPreprocessingAuthority("Run has no exact approved or skipped QC identity")

        conversation = self._conversation_repository.get_conversation(conversation_id)
        self._require_work_conversation(conversation, conversation_id, authority.project_id)
        if task.task_id != self.preprocessing_task_id(conversation_id, plan_revision_id):
            raise _InvalidPreprocessingAuthority("WorkTask ID does not match its frozen binding")
        plan = self._plan_repository.get_plan(plan_revision_id)
        if (
            plan.plan_revision_id != plan_revision_id
            or plan.project_id != authority.project_id
            or plan.state not in {PlanState.AWAITING_APPROVAL, PlanState.APPROVED}
            or plan.plan_hash != step.input_version
            or task.authority_refs.input_revision != plan.manifest_hash
            or step.authority_refs.input_revision != plan.manifest_hash
        ):
            raise _InvalidPreprocessingAuthority("WorkTask is not bound to this frozen Plan")
        expected = self._derive_preprocessing_expectations(plan, authority.project_id)
        if step.expected_artifacts and step.expected_artifacts != expected:
            raise _InvalidPreprocessingAuthority(
                "stored expected Artifacts differ from the frozen preprocessing Plan"
            )
        skill_plan = SkillPlan.model_validate(plan.plan["skill_plan"])
        values = dict(skill_plan.resolved_parameters)
        alff = (
            AlffFalffParameters.model_validate(values["alff_falff"])
            if "alff_falff" in values
            else None
        )
        reho = RehoParameters.model_validate(values["reho"]) if "reho" in values else None
        verified_ids = self._verify_preprocessing_artifacts(
            expected=expected,
            artifacts=authority.artifacts,
            project_id=authority.project_id,
            run_id=run_id,
            manifest_hash=plan.manifest_hash,
            alff=alff,
            reho=reho,
        )

        targets: list[dict[str, Any]] = []
        lineages: list[dict[str, Any]] = []
        by_identity = {
            (artifact.artifact_type, artifact.relative_path): artifact
            for artifact in authority.artifacts
        }
        for target in expected:
            artifact = by_identity[(target.artifact_type, target.relative_path)]
            lineage = ArtifactLineage.model_validate(artifact.provenance["lineage"])
            targets.append(
                {
                    "subject_id": target.subject_id,
                    "metric": target.metric,
                    "artifact_type": target.artifact_type,
                    "relative_path": target.relative_path,
                    "artifact_id": artifact.artifact_id,
                    "checksum": artifact.checksum,
                    "size_bytes": artifact.size_bytes,
                }
            )
            lineages.append(
                {
                    "artifact_id": artifact.artifact_id,
                    "lineage": lineage.model_dump(mode="json"),
                }
            )
        targets.sort(key=lambda item: (item["subject_id"], item["metric"], item["artifact_id"]))
        lineages.sort(key=lambda item: item["artifact_id"])
        target_artifact_fingerprint = content_hash(targets)
        lineage_fingerprint = content_hash(
            {
                "run_id": run_id,
                "run_version": snapshot.run_version,
                "plan_revision_id": plan_revision_id,
                "plan_hash": plan.plan_hash,
                "manifest_hash": plan.manifest_hash,
                "qc_decision_kind": qc_decision_kind,
                "qc_decision_identity": qc_decision_identity,
                "target_artifact_fingerprint": target_artifact_fingerprint,
                "lineages": lineages,
            }
        )
        return _PreprocessingCompletionEvidence(
            plan_revision_id=plan_revision_id,
            manifest_hash=plan.manifest_hash,
            expected_artifacts=expected,
            verified_artifact_ids=verified_ids,
            target_artifact_fingerprint=target_artifact_fingerprint,
            lineage_fingerprint=lineage_fingerprint,
            qc_decision_kind=qc_decision_kind,
            qc_decision_identity=qc_decision_identity,
        )

    def _complete_preprocessing_task(
        self,
        task: WorkTask,
        run_id: str,
        authority: _StableRunAuthority,
        receipts: tuple[EventReconciliationReceipt, ...],
        *,
        claim_token: str | None,
        readiness_assessment: _PreprocessingReadinessAssessment | None = None,
    ) -> WorkflowRunReconciliation:
        """Complete a preprocessing goal only after exact target Artifact checks."""

        expected: tuple[DpabiPreprocessingOutput, ...] = ()
        try:
            if readiness_assessment is not None:
                refreshed_authority = self._read_stable_authoritative_snapshot(task, run_id)
                if refreshed_authority is None:
                    return WorkflowRunReconciliation(
                        task=task,
                        readiness=RunReadiness.INDETERMINATE,
                        event_receipts=receipts,
                        authoritative=authority.snapshot,
                        reason="预处理 Run 在 readiness 检查后仍不稳定；本次未写入任务或 outbox。",
                    )
                if refreshed_authority.snapshot.unresolved_referenced_artifact_ids:
                    return WorkflowRunReconciliation(
                        task=task,
                        readiness=RunReadiness.INDETERMINATE,
                        event_receipts=receipts,
                        authoritative=refreshed_authority.snapshot,
                        reason=(
                            "预处理 Artifact 引用在最终复核时无法确认；本次未写入任务或 outbox。"
                        ),
                    )
                authority = refreshed_authority
            completion = self._read_preprocessing_completion_evidence(task, run_id, authority)
            expected = completion.expected_artifacts
        except NotFoundError as exc:
            if readiness_assessment is not None:
                return WorkflowRunReconciliation(
                    task=task,
                    readiness=RunReadiness.INDETERMINATE,
                    event_receipts=receipts,
                    authoritative=authority.snapshot,
                    reason="预处理完成证据在最终复核时发生变化；本次未保存任务或 outbox。",
                )
            return self._block_preprocessing_task(
                task,
                receipts,
                authority.snapshot,
                expected,
                reason=f"预处理任务引用的冻结对象或目标 Artifact 不存在；任务已阻断。({exc})",
                claim_token=claim_token,
            )
        except (
            ConflictError,
            InputValidationError,
            ValueError,
            TypeError,
            KeyError,
            AttributeError,
            ValidationError,
        ) as exc:
            if readiness_assessment is not None:
                return WorkflowRunReconciliation(
                    task=task,
                    readiness=RunReadiness.INDETERMINATE,
                    event_receipts=receipts,
                    authoritative=authority.snapshot,
                    reason="预处理完成证据在最终复核时无法确认；本次未保存任务或 outbox。",
                )
            return self._block_preprocessing_task(
                task,
                receipts,
                authority.snapshot,
                expected,
                reason=(
                    "预处理目标产物或冻结权威链无法通过核验；任务已阻断。"
                    f"({type(exc).__name__}: {exc})"
                ),
                claim_token=claim_token,
            )

        step = self._preprocessing_step(task)
        if step is None:
            raise _InvalidPreprocessingAuthority("completed preprocessing step disappeared")
        if readiness_assessment is not None:
            evidence = readiness_assessment.evidence
            assessed_completion = readiness_assessment.completion_evidence
            if (
                evidence is None
                or assessed_completion is None
                or readiness_assessment.outcome is not StepReadinessOutcome.READY_TO_FINALIZE
                or evidence.step_kind is not StepReadinessStepKind.PREPROCESSING_RUN
                or readiness_assessment.task_id != task.task_id
                or readiness_assessment.task_version != task.version
                or readiness_assessment.step_id != step.step_id
                or readiness_assessment.step_version != step.version
                or readiness_assessment.step_kind != step.kind
                or readiness_assessment.run_id != run_id
                or evidence.run_version != authority.snapshot.run_version
                or evidence.plan_revision_id != completion.plan_revision_id
                or evidence.frozen_input_fingerprint != completion.manifest_hash
                or evidence.result_identity != completion.target_artifact_fingerprint
                or evidence.lineage_fingerprint != completion.lineage_fingerprint
                or assessed_completion.plan_revision_id != completion.plan_revision_id
                or assessed_completion.manifest_hash != completion.manifest_hash
                or assessed_completion.target_artifact_fingerprint
                != completion.target_artifact_fingerprint
                or assessed_completion.lineage_fingerprint != completion.lineage_fingerprint
            ):
                return WorkflowRunReconciliation(
                    task=task,
                    readiness=RunReadiness.INDETERMINATE,
                    event_receipts=receipts,
                    authoritative=authority.snapshot,
                    reason="预处理证据与 readiness assessment 不一致；本次未写入。",
                )
        completed = complete_step(task, step, artifact_ids=completion.verified_artifact_ids)
        completed_step = self._preprocessing_step(completed)
        if completed_step is None:
            raise _InvalidPreprocessingAuthority("completed preprocessing step disappeared")
        completed = WorkTask.model_validate(
            completed.model_copy(
                update={
                    "steps": tuple(
                        item.model_copy(update={"expected_artifacts": expected})
                        if item.step_id == completed_step.step_id
                        else item
                        for item in completed.steps
                    )
                }
            ).model_dump()
        )
        with self._repository.atomic():
            completed = self._save_task(
                WorkTask.model_validate(completed.model_dump()),
                expected_version=task.version,
                claim_token=claim_token,
            )
            outboxes = self._repository.list_analysis_outboxes_for_preprocessing_task(task.task_id)
            for outbox in outboxes:
                if outbox.status not in {
                    AnalysisOutboxStatus.PENDING,
                    AnalysisOutboxStatus.READY,
                }:
                    continue
                if (
                    not self._outbox_matches_preprocessing_completion(
                        outbox,
                        task=task,
                        plan_revision_id=completion.plan_revision_id,
                        manifest_hash=completion.manifest_hash,
                        run_id=run_id,
                    )
                    or outbox.project_id != authority.project_id
                    or not self._outbox_intent_matches(outbox)
                ):
                    continue
                self._repository.mark_analysis_outbox_ready(
                    outbox.intent_task_id,
                    preprocessing_task_id=task.task_id,
                    plan_revision_id=completion.plan_revision_id,
                    manifest_hash=completion.manifest_hash,
                    run_id=run_id,
                    artifact_ids=completion.verified_artifact_ids,
                )
        return WorkflowRunReconciliation(
            task=completed,
            readiness=RunReadiness.COMPLETED,
            event_receipts=receipts,
            authoritative=authority.snapshot,
            reason="冻结预处理 Plan、QC 决策及所有精确目标 Artifact 均已核验；任务完成。",
        )

    def _outbox_intent_matches(self, outbox: AnalysisProposalOutbox) -> bool:
        """Resolve the request only through its exact persisted intent ID."""

        try:
            conversation = self._conversation_repository.get_conversation(outbox.conversation_id)
        except NotFoundError:
            return False
        if conversation.project_id != outbox.project_id:
            return False
        matches: list[Mapping[str, Any]] = []
        for message in conversation.messages:
            intent = message.payload.get("analysis_dependency")
            if isinstance(intent, dict) and intent.get("task_id") == outbox.intent_task_id:
                matches.append(intent)
        if not matches:
            return False
        for intent in matches:
            request = intent.get("analysis_request")
            if not (
                intent.get("task_kind") == "preprocess_then_analyze"
                and intent.get("conversation_id") == outbox.conversation_id
                and intent.get("project_id") == outbox.project_id
                and isinstance(request, str)
                and content_hash(request.strip()) == outbox.analysis_request_hash
            ):
                return False
        return True

    @staticmethod
    def _outbox_matches_preprocessing_completion(
        outbox: AnalysisProposalOutbox,
        *,
        task: WorkTask,
        plan_revision_id: str,
        manifest_hash: str,
        run_id: str,
    ) -> bool:
        refs = task.authority_refs
        return (
            outbox.preprocessing_task_id == task.task_id
            and outbox.conversation_id == refs.conversation_id
            and outbox.plan_revision_id == plan_revision_id
            and outbox.manifest_hash == manifest_hash
            and outbox.run_id == run_id
            and refs.plan_revision_id == plan_revision_id
            and refs.input_revision == manifest_hash
            and refs.workflow_run_id == run_id
        )

    def _block_preprocessing_task(
        self,
        task: WorkTask,
        receipts: tuple[EventReconciliationReceipt, ...],
        snapshot: AuthoritativeRunSnapshot,
        expected: tuple[DpabiPreprocessingOutput, ...],
        *,
        reason: str,
        claim_token: str | None,
    ) -> WorkflowRunReconciliation:
        updated = task
        if task.status in {
            TaskStatus.WAITING_RUN,
            TaskStatus.WAITING_USER,
            TaskStatus.WAITING_DEPENDENCY,
        }:
            step = self._preprocessing_step(task)
            updates: dict[str, Any] = {}
            if step is not None:
                blocked_statuses = {
                    TaskStepStatus.READY,
                    TaskStepStatus.RUNNING,
                    TaskStepStatus.WAITING,
                }
                if step.status in blocked_statuses:
                    blocked_step = transition_task_step(step, TaskStepStatus.BLOCKED)
                    updates["steps"] = tuple(
                        blocked_step if item.step_id == step.step_id else item
                        for item in task.steps
                    )
                if expected:
                    current_step = updates.get("steps", task.steps)
                    updates["steps"] = tuple(
                        item.model_copy(update={"expected_artifacts": expected})
                        if item.step_id == step.step_id
                        else item
                        for item in current_step
                    )
            updated = transition_task(task, TaskStatus.BLOCKED).model_copy(update=updates)
            updated = self._save_task(
                WorkTask.model_validate(updated.model_dump()),
                expected_version=task.version,
                claim_token=claim_token,
            )
        return WorkflowRunReconciliation(
            task=updated,
            readiness=RunReadiness.BLOCKED,
            event_receipts=receipts,
            authoritative=snapshot,
            reason=reason,
        )

    def _verify_preprocessing_artifacts(
        self,
        *,
        expected: tuple[DpabiPreprocessingOutput, ...],
        artifacts: tuple[ArtifactView, ...],
        project_id: str,
        run_id: str,
        manifest_hash: str,
        alff: AlffFalffParameters | None,
        reho: RehoParameters | None,
    ) -> tuple[str, ...]:
        if not expected or not artifacts:
            raise _InvalidPreprocessingAuthority("frozen Plan or Run has no expected outputs")
        ids = [item.artifact_id for item in artifacts]
        paths = [item.relative_path for item in artifacts]
        if len(ids) != len(set(ids)) or len(paths) != len(set(paths)):
            raise _InvalidPreprocessingAuthority("registered Artifact IDs or paths are duplicated")
        by_identity: dict[tuple[str, str], list[ArtifactView]] = {}
        for artifact in artifacts:
            if (
                artifact.project_id != project_id
                or artifact.run_id != run_id
                or not artifact.artifact_id
                or not artifact.relative_path
                or len(artifact.checksum) != 64
                or any(char not in "0123456789abcdef" for char in artifact.checksum)
                or artifact.size_bytes < 1
            ):
                raise _InvalidPreprocessingAuthority(
                    "registered Artifact identity or integrity is invalid"
                )
            by_identity.setdefault((artifact.artifact_type, artifact.relative_path), []).append(
                artifact
            )

        auto_mask = False
        metric_parameters = tuple(item for item in (alff, reho) if item is not None)
        mask_sources = {item.mask_source for item in metric_parameters}
        if len(mask_sources) > 1:
            raise _InvalidPreprocessingAuthority("metric mask sources disagree")
        if metric_parameters:
            auto_mask = next(iter(mask_sources)) is MetricMaskSource.DPABI_AUTO

        targets: dict[tuple[str, str], ArtifactView] = {}
        lineages: dict[str, ArtifactLineage] = {}
        kind_for_metric = {
            "timeseries": ArtifactKind.FUNCTIONAL_TIMESERIES,
            "brain_mask": ArtifactKind.BRAIN_MASK,
            "alff": ArtifactKind.ALFF_MAP,
            "falff": ArtifactKind.FALFF_MAP,
            "reho": ArtifactKind.REHO_MAP,
        }
        for target in expected:
            identity = (target.artifact_type, target.relative_path)
            matches = by_identity.get(identity, [])
            if len(matches) != 1:
                raise _InvalidPreprocessingAuthority(
                    f"target {target.artifact_type} at {target.relative_path} "
                    f"has {len(matches)} matches"
                )
            artifact = matches[0]
            raw_lineage = artifact.provenance.get("lineage")
            if not isinstance(raw_lineage, Mapping):
                raise _InvalidPreprocessingAuthority("target Artifact has no typed lineage")
            lineage = ArtifactLineage.model_validate(raw_lineage)
            if (
                lineage.artifact_id != artifact.artifact_id
                or lineage.kind is not kind_for_metric[target.metric]
                or lineage.subject_id != target.subject_id
                or lineage.subject_manifest_hash != manifest_hash
                or not lineage.metadata_verified
                or len(lineage.producer_step_hash) != 64
                or any(char not in "0123456789abcdef" for char in lineage.producer_step_hash)
            ):
                raise _InvalidPreprocessingAuthority(
                    "target Artifact lineage differs from frozen output"
                )
            if target.metric in {"alff", "falff", "reho"}:
                if lineage.metric_scaling is not target.scaling:
                    raise _InvalidPreprocessingAuthority(
                        "metric scaling differs from frozen output"
                    )
            elif lineage.metric_scaling is not None:
                raise _InvalidPreprocessingAuthority("non-metric output carries a metric scaling")
            targets[identity] = artifact
            lineages[artifact.artifact_id] = lineage

        auto_masks = {
            item.subject_id: targets[(item.artifact_type, item.relative_path)]
            for item in expected
            if item.metric == "brain_mask"
        }
        registered_mask_ids = {
            item.mask_artifact_id
            for item in metric_parameters
            if item.mask_source is MetricMaskSource.REGISTERED_ARTIFACT
        }
        if len(registered_mask_ids) > 1:
            raise _InvalidPreprocessingAuthority("frozen metrics use different registered masks")

        for artifact in targets.values():
            lineage = lineages[artifact.artifact_id]
            if lineage.mask_artifact_id is None:
                continue
            mask_artifact: ArtifactView
            if lineage.mask_artifact_id in lineages:
                mask_artifact = next(
                    item for item in artifacts if item.artifact_id == lineage.mask_artifact_id
                )
                mask_lineage = lineages[lineage.mask_artifact_id]
            else:
                mask_artifact = self._artifact_repository.get_artifact(lineage.mask_artifact_id)
                if mask_artifact.artifact_id != lineage.mask_artifact_id:
                    raise _InvalidPreprocessingAuthority(
                        "mask lookup returned a different Artifact"
                    )
                if mask_artifact.project_id != project_id:
                    raise _InvalidPreprocessingAuthority("mask Artifact belongs to another project")
                raw_mask_lineage = mask_artifact.provenance.get("lineage")
                if not isinstance(raw_mask_lineage, Mapping):
                    raise _InvalidPreprocessingAuthority("referenced mask has no typed lineage")
                mask_lineage = ArtifactLineage.model_validate(raw_mask_lineage)
            if (
                mask_lineage.artifact_id != mask_artifact.artifact_id
                or mask_lineage.kind is not ArtifactKind.BRAIN_MASK
                or not mask_lineage.metadata_verified
                or len(mask_lineage.producer_step_hash) != 64
                or any(char not in "0123456789abcdef" for char in mask_lineage.producer_step_hash)
                or lineage.mask_grid_signature != mask_lineage.grid_signature
            ):
                raise _InvalidPreprocessingAuthority(
                    "referenced mask is not typed or grid-compatible"
                )
            if lineage.kind in {
                ArtifactKind.ALFF_MAP,
                ArtifactKind.FALFF_MAP,
                ArtifactKind.REHO_MAP,
            }:
                if auto_mask:
                    expected_mask = auto_masks.get(lineage.subject_id or "")
                    if (
                        expected_mask is None
                        or expected_mask.artifact_id != mask_artifact.artifact_id
                    ):
                        raise _InvalidPreprocessingAuthority(
                            "metric output does not use its DPABI auto mask"
                        )
                elif registered_mask_ids and lineage.mask_artifact_id not in registered_mask_ids:
                    raise _InvalidPreprocessingAuthority(
                        "metric output uses a different registered mask"
                    )
        return tuple(sorted(item.artifact_id for item in targets.values()))

    def _reconcile_preprocessing_run(
        self,
        task: WorkTask,
        step: TaskStep,
        run_id: str,
        receipts: tuple[EventReconciliationReceipt, ...],
        *,
        claim_token: str | None = None,
    ) -> WorkflowRunReconciliation:
        assessment = self._step_readiness_registry.inspect(task, step, run_id)
        if assessment.outcome is StepReadinessOutcome.INDETERMINATE:
            authority = (
                assessment.stable_authority
                if isinstance(assessment, _PreprocessingReadinessAssessment)
                else None
            )
            return WorkflowRunReconciliation(
                task=task,
                readiness=RunReadiness.INDETERMINATE,
                event_receipts=receipts,
                authoritative=authority.snapshot if authority is not None else None,
                reason=assessment.reason,
            )

        preprocessing_assessment = self._current_preprocessing_assessment(
            task, step, run_id, assessment
        )
        if preprocessing_assessment is None:
            return WorkflowRunReconciliation(
                task=task,
                readiness=RunReadiness.INDETERMINATE,
                event_receipts=receipts,
                reason="预处理 assessment 与当前绑定不一致；本次未推进状态。",
            )
        authority = preprocessing_assessment.stable_authority
        if authority is None:
            return WorkflowRunReconciliation(
                task=task,
                readiness=RunReadiness.INDETERMINATE,
                event_receipts=receipts,
                reason="预处理 readiness assessment 缺少稳定权威快照；本次未推进状态。",
            )
        snapshot = authority.snapshot

        if preprocessing_assessment.outcome is StepReadinessOutcome.BLOCKED:
            if preprocessing_assessment.reason_code == "preprocessing_evidence_invalid":
                return self._block_preprocessing_task(
                    task,
                    receipts,
                    snapshot,
                    (),
                    reason=preprocessing_assessment.reason,
                    claim_token=claim_token,
                )
            return self._apply_preprocessing_task_state(
                task,
                receipts,
                snapshot,
                target=TaskStatus.BLOCKED,
                readiness=RunReadiness.BLOCKED,
                waiting_condition=None,
                reason=preprocessing_assessment.reason,
                claim_token=claim_token,
            )

        if preprocessing_assessment.outcome is StepReadinessOutcome.READY_TO_FINALIZE:
            return self._complete_preprocessing_task(
                task,
                run_id,
                authority,
                receipts,
                readiness_assessment=preprocessing_assessment,
                claim_token=claim_token,
            )

        waiting_state = {
            StepReadinessOutcome.WAITING_RUN: (TaskStatus.WAITING_RUN, RunReadiness.WAITING_RUN),
            StepReadinessOutcome.WAITING_USER: (TaskStatus.WAITING_USER, RunReadiness.WAITING_USER),
            StepReadinessOutcome.WAITING_DEPENDENCY: (
                TaskStatus.WAITING_DEPENDENCY,
                RunReadiness.WAITING_DEPENDENCY,
            ),
        }.get(preprocessing_assessment.outcome)
        condition = preprocessing_assessment.waiting_condition
        if waiting_state is None or condition is None:
            return WorkflowRunReconciliation(
                task=task,
                readiness=RunReadiness.INDETERMINATE,
                event_receipts=receipts,
                authoritative=snapshot,
                reason="预处理等待 assessment 缺少受支持的状态或等待条件；本次未推进状态。",
            )
        target, readiness = waiting_state
        return self._apply_preprocessing_task_state(
            task,
            receipts,
            snapshot,
            target=target,
            readiness=readiness,
            waiting_condition=condition,
            reason=preprocessing_assessment.reason,
            claim_token=claim_token,
        )

    @staticmethod
    def _current_preprocessing_assessment(
        task: WorkTask,
        step: TaskStep,
        run_id: str,
        assessment: StepReadinessAssessment,
    ) -> _PreprocessingReadinessAssessment | None:
        if not isinstance(assessment, _PreprocessingReadinessAssessment):
            return None
        authority = assessment.stable_authority
        if (
            authority is None
            or assessment.task_id != task.task_id
            or assessment.task_version != task.version
            or assessment.step_id != step.step_id
            or assessment.step_version != step.version
            or assessment.step_kind != "preprocessing_run"
            or step.kind != "preprocessing_run"
            or assessment.run_id != run_id
            or authority.snapshot.run_id != run_id
        ):
            return None
        if assessment.outcome is StepReadinessOutcome.READY_TO_FINALIZE:
            evidence = assessment.evidence
            completion = assessment.completion_evidence
            if (
                evidence is None
                or completion is None
                or evidence.step_kind is not StepReadinessStepKind.PREPROCESSING_RUN
                or evidence.run_version != authority.snapshot.run_version
                or evidence.plan_revision_id != completion.plan_revision_id
                or evidence.plan_revision_id != task.authority_refs.plan_revision_id
                or evidence.plan_revision_id != step.authority_refs.plan_revision_id
                or evidence.frozen_input_fingerprint != completion.manifest_hash
                or evidence.frozen_input_fingerprint != task.authority_refs.input_revision
                or evidence.frozen_input_fingerprint != step.authority_refs.input_revision
                or evidence.result_identity != completion.target_artifact_fingerprint
                or evidence.lineage_fingerprint != completion.lineage_fingerprint
            ):
                return None
        elif assessment.evidence is not None or assessment.completion_evidence is not None:
            return None
        return assessment

    def _apply_preprocessing_task_state(
        self,
        task: WorkTask,
        receipts: tuple[EventReconciliationReceipt, ...],
        snapshot: AuthoritativeRunSnapshot,
        *,
        target: TaskStatus,
        readiness: RunReadiness,
        waiting_condition: WaitingCondition | None,
        reason: str,
        claim_token: str | None,
    ) -> WorkflowRunReconciliation:
        updated = task
        if (
            target
            in {
                TaskStatus.WAITING_RUN,
                TaskStatus.WAITING_USER,
                TaskStatus.WAITING_DEPENDENCY,
            }
            and waiting_condition is None
        ):
            return WorkflowRunReconciliation(
                task=task,
                readiness=RunReadiness.INDETERMINATE,
                event_receipts=receipts,
                authoritative=snapshot,
                reason="预处理 waiting 状态缺少等待条件；本次未推进状态。",
            )
        if task.status is not target or task.waiting_condition != waiting_condition:
            if task.status is not target:
                updated = transition_task(task, target, waiting_condition=waiting_condition)
            else:
                updated = WorkTask.model_validate(
                    {
                        **task.model_dump(),
                        "waiting_condition": (
                            waiting_condition.model_dump()
                            if waiting_condition is not None
                            else None
                        ),
                        "version": task.version + 1,
                    }
                )
            updated = self._save_task(
                updated,
                expected_version=task.version,
                claim_token=claim_token,
            )
        return WorkflowRunReconciliation(
            task=updated,
            readiness=readiness,
            event_receipts=receipts,
            authoritative=snapshot,
            reason=reason,
        )

    def _reconcile_statistical_run(
        self,
        task: WorkTask,
        run_id: str,
        receipts: tuple[EventReconciliationReceipt, ...],
        *,
        claim_token: str | None = None,
    ) -> WorkflowRunReconciliation:
        if len(task.steps) != 1:
            return WorkflowRunReconciliation(
                task=task,
                readiness=RunReadiness.INDETERMINATE,
                event_receipts=receipts,
                reason="统计 WorkTask 步骤结构无法唯一解析；任务状态未推进。",
            )
        step = task.steps[0]
        assessment = self._step_readiness_registry.inspect(task, step, run_id)
        if assessment.outcome is StepReadinessOutcome.INDETERMINATE:
            authoritative = (
                assessment.stable_snapshot.authoritative
                if isinstance(assessment, _StatisticalReadinessAssessment)
                and assessment.stable_snapshot is not None
                else None
            )
            return WorkflowRunReconciliation(
                task=task,
                readiness=RunReadiness.INDETERMINATE,
                event_receipts=receipts,
                authoritative=authoritative,
                reason=assessment.reason,
            )

        statistical_assessment = self._current_statistical_assessment(
            task, step, run_id, assessment
        )
        if statistical_assessment is None:
            return WorkflowRunReconciliation(
                task=task,
                readiness=RunReadiness.INDETERMINATE,
                event_receipts=receipts,
                reason=(
                    "统计 readiness assessment 与当前 Task/Step/Run 证据不一致；本次未推进状态。"
                ),
            )
        stable_snapshot = statistical_assessment.stable_snapshot
        snapshot = stable_snapshot.authoritative if stable_snapshot is not None else None

        if statistical_assessment.outcome is StepReadinessOutcome.BLOCKED:
            return self._block_statistical_task(
                task,
                run_id,
                receipts,
                reason=statistical_assessment.reason,
                authoritative=snapshot,
                claim_token=claim_token,
            )
        if statistical_assessment.outcome is StepReadinessOutcome.READY_TO_FINALIZE:
            if snapshot is None:
                return WorkflowRunReconciliation(
                    task=task,
                    readiness=RunReadiness.INDETERMINATE,
                    event_receipts=receipts,
                    reason="统计 readiness assessment 缺少稳定权威快照；本次未推进状态。",
                )
            return self._complete_statistical_task(
                task,
                run_id,
                receipts,
                snapshot,
                readiness_assessment=statistical_assessment,
                claim_token=claim_token,
            )

        if snapshot is None or statistical_assessment.waiting_condition is None:
            return WorkflowRunReconciliation(
                task=task,
                readiness=RunReadiness.INDETERMINATE,
                event_receipts=receipts,
                authoritative=snapshot,
                reason="统计等待 assessment 缺少稳定快照或等待条件；本次未推进状态。",
            )
        waiting_state = {
            StepReadinessOutcome.WAITING_RUN: (TaskStatus.WAITING_RUN, RunReadiness.WAITING_RUN),
            StepReadinessOutcome.WAITING_USER: (TaskStatus.WAITING_USER, RunReadiness.WAITING_USER),
            StepReadinessOutcome.WAITING_DEPENDENCY: (
                TaskStatus.WAITING_DEPENDENCY,
                RunReadiness.WAITING_DEPENDENCY,
            ),
        }.get(statistical_assessment.outcome)
        if waiting_state is None:
            return WorkflowRunReconciliation(
                task=task,
                readiness=RunReadiness.INDETERMINATE,
                event_receipts=receipts,
                authoritative=snapshot,
                reason="统计 readiness outcome 未受协调器支持；本次未推进状态。",
            )
        target, readiness = waiting_state
        return self._apply_statistical_task_state(
            task,
            run_id,
            receipts,
            snapshot,
            target=target,
            readiness=readiness,
            waiting_condition=statistical_assessment.waiting_condition,
            reason=statistical_assessment.reason,
            claim_token=claim_token,
        )

    @staticmethod
    def _current_statistical_assessment(
        task: WorkTask,
        step: TaskStep,
        run_id: str,
        assessment: StepReadinessAssessment,
    ) -> _StatisticalReadinessAssessment | None:
        if not isinstance(assessment, _StatisticalReadinessAssessment):
            return None
        snapshot = assessment.stable_snapshot
        if snapshot is None:
            return (
                None if assessment.outcome is StepReadinessOutcome.READY_TO_FINALIZE else assessment
            )
        authoritative = snapshot.authoritative
        if (
            assessment.task_id != task.task_id
            or assessment.task_version != task.version
            or assessment.step_id != step.step_id
            or assessment.step_version != step.version
            or assessment.step_kind != "statistical_run"
            or step.kind != "statistical_run"
            or assessment.run_id != run_id
            or authoritative.run_id != run_id
        ):
            return None
        if assessment.outcome is StepReadinessOutcome.READY_TO_FINALIZE:
            evidence = assessment.evidence
            if (
                evidence is None
                or evidence.step_kind is not StepReadinessStepKind.STATISTICAL_RUN
                or evidence.run_version != authoritative.run_version
                or evidence.plan_revision_id != snapshot.plan_revision_id
                or evidence.plan_revision_id != task.authority_refs.plan_revision_id
                or evidence.frozen_input_fingerprint != snapshot.frozen_input_fingerprint
                or evidence.frozen_input_fingerprint != task.authority_refs.input_revision
                or evidence.result_identity != authoritative.verified_statistical_result_id
                or evidence.result_version != snapshot.result_version
                or evidence.result_bundle_fingerprint != snapshot.result_bundle_fingerprint
                or evidence.lineage_fingerprint != snapshot.result_lineage_fingerprint
                or snapshot.result_bundle_fingerprint is None
                or snapshot.result_version is None
            ):
                return None
        return assessment

    def _read_stable_statistical_snapshot(
        self,
        task: WorkTask,
        run_id: str,
        *,
        max_attempts: int = 3,
    ) -> tuple[_StatisticalReadinessSnapshot | None, str | None]:
        for _attempt in range(max_attempts):
            run_before = self._run_repository.get_run(run_id)
            run_failure: str | None = None
            if run_before.run_id != run_id:
                run_failure = "统计 Run 读取端口返回了其他 Run；任务已阻断。"

            plan = None
            design: StatisticalDesignRevision | None = None
            project_id = run_before.project_id
            try:
                if run_before.plan_revision_id != task.authority_refs.plan_revision_id:
                    raise _InvalidStatisticalAuthority(
                        "WorkTask 与统计 Run 引用的 Plan 不一致；任务已阻断。"
                    )
                if task.authority_refs.workflow_run_id != run_id:
                    raise _InvalidStatisticalAuthority(
                        "WorkTask 与统计 Run 的绑定 ID 不一致；任务已阻断。"
                    )
                if task.authority_refs.conversation_id is None:
                    raise _InvalidStatisticalAuthority("统计 WorkTask 缺少会话绑定；任务已阻断。")
                conversation = self._conversation_repository.get_conversation(
                    task.authority_refs.conversation_id
                )
                self._require_work_conversation(
                    conversation, conversation.conversation_id, project_id
                )
                if task.authority_refs.plan_revision_id is None:
                    raise _InvalidStatisticalAuthority(
                        "统计 WorkTask 缺少冻结 Plan 引用；任务已阻断。"
                    )
                plan = self._plan_repository.get_plan(task.authority_refs.plan_revision_id)
                step = self._statistical_step(task)
                if step is None or not step.input_version:
                    raise _InvalidStatisticalAuthority(
                        "统计 WorkTask 缺少唯一提案哈希；任务已阻断。"
                    )
                context, design = self._statistical_plan_context(
                    plan,
                    conversation_id=conversation.conversation_id,
                    project_id=project_id,
                    proposal_hash=step.input_version,
                )
                if task.authority_refs.input_revision != context["draft_hash"]:
                    raise _InvalidStatisticalAuthority(
                        "统计 WorkTask 的草稿修订与冻结 Plan work_context 不一致；任务已阻断。"
                    )
                if plan.plan_revision_id != run_before.plan_revision_id:
                    raise _InvalidStatisticalAuthority(
                        "统计 Run 实际 Plan revision 与当前冻结 Plan 不一致；任务已阻断。"
                    )
                if (
                    step is None
                    or step.status is not TaskStepStatus.WAITING
                    or step.authority_refs.plan_revision_id != plan.plan_revision_id
                    or step.authority_refs.workflow_run_id != run_id
                ):
                    raise _InvalidStatisticalAuthority(
                        "统计步骤的 Plan/Run 权威引用不完整或不一致；任务已阻断。"
                    )
                if not self._analysis_proposal_task_identity_matches(
                    task, conversation.conversation_id, context["proposal_hash"]
                ):
                    raise _InvalidStatisticalAuthority(
                        "统计 WorkTask ID 与 conversation/proposal 绑定不一致；任务已阻断。"
                    )
            except NotFoundError:
                run_failure = "统计 WorkTask 引用的会话或冻结 Plan 不存在；任务已阻断。"
                plan = None
                design = None
            except (_InvalidStatisticalAuthority, ConflictError, InputValidationError) as exc:
                run_failure = str(exc)
                plan = None
                design = None

            try:
                artifacts = self._artifact_repository.list_artifacts(run_id)
            except NotFoundError:
                artifacts = []
                run_failure = run_failure or "统计 Run 的 Artifact 记录暂不可读取。"
            if any(
                artifact.run_id != run_id or artifact.project_id != project_id
                for artifact in artifacts
            ):
                run_failure = (
                    run_failure or "统计 Artifact 仓储返回了其他项目或 Run 的记录；任务已阻断。"
                )
            artifact_ids = [artifact.artifact_id for artifact in artifacts]
            if len(artifact_ids) != len(set(artifact_ids)):
                run_failure = (
                    run_failure or "统计 Artifact 仓储返回了重复 Artifact ID；任务已阻断。"
                )

            summaries: list[StatisticalResultView] = []
            detail: StatisticalResultDetailView | None = None
            if run_failure is None and run_before.state is WorkflowState.SUCCEEDED:
                summaries = self._artifact_repository.list_statistical_results(
                    project_id=project_id,
                    run_id=run_id,
                )
                if any(
                    result.project_id != project_id or result.run_id != run_id
                    for result in summaries
                ):
                    run_failure = "统计结果列表包含其他项目或 Run 的记录；任务已阻断。"
                elif len(summaries) > 1:
                    run_failure = "同一统计 Run 注册了多个 StatisticalResult；任务已阻断。"
                elif (
                    summaries
                    and design is not None
                    and summaries[0].design_revision_id != design.revision_id
                ):
                    run_failure = (
                        "StatisticalResult.design_revision_id 与冻结 Plan design 不一致；"
                        "任务已阻断。"
                    )

            verified_id: str | None = None
            result_artifact_ids: tuple[str, ...] = ()
            result_lineage_fingerprint: str | None = None
            if summaries and run_failure is None and plan is not None and design is not None:
                try:
                    detail = self._artifact_repository.get_statistical_result(
                        summaries[0].result_id
                    )
                    result_artifact_ids = self._verify_statistical_result(
                        summary=summaries[0],
                        detail=detail,
                        plan=plan,
                        design=design,
                        run_id=run_id,
                        project_id=project_id,
                        registered_artifacts=artifacts,
                    )
                    verified_id = detail.result_id
                    result_lineage_fingerprint = self._statistical_result_lineage_fingerprint(
                        detail,
                        artifacts,
                        result_artifact_ids,
                    )
                except NotFoundError:
                    run_failure = "StatisticalResult 列表项对应的 detail 不存在；任务已阻断。"
                except (ValueError, TypeError, KeyError, AttributeError, ValidationError) as exc:
                    run_failure = (
                        "StatisticalResult 权威凭据无法通过核验；任务已阻断。"
                        f"({type(exc).__name__})"
                    )

            run_after = self._run_repository.get_run(run_id)
            if run_after.run_id != run_id:
                raise _InvalidStatisticalAuthority("统计 Run 读取端口返回了其他 Run；任务已阻断。")
            if run_before.version != run_after.version:
                continue
            snapshot = AuthoritativeRunSnapshot(
                run_id=run_id,
                run_state=run_after.state.value,
                run_version=run_after.version,
                attempt=run_after.attempt,
                qc_mode=run_after.qc_mode.value,
                qc_status=run_after.qc_status,
                registered_artifact_ids=tuple(sorted(item.artifact_id for item in artifacts)),
                registered_artifact_types=tuple(sorted({item.artifact_type for item in artifacts})),
                statistical_result_ids=tuple(result.result_id for result in summaries),
                verified_statistical_result_id=verified_id,
                result_artifact_ids=result_artifact_ids,
            )
            input_revision = task.authority_refs.input_revision
            frozen_input_fingerprint = (
                input_revision
                if input_revision is not None and re.fullmatch(r"[a-f0-9]{64}", input_revision)
                else None
            )
            readiness_snapshot = _StatisticalReadinessSnapshot(
                authoritative=snapshot,
                plan_revision_id=(plan.plan_revision_id if plan is not None else None),
                frozen_input_fingerprint=frozen_input_fingerprint,
                result_bundle_fingerprint=(
                    detail.bundle_hash if verified_id is not None and detail is not None else None
                ),
                result_version=detail.version
                if verified_id is not None and detail is not None
                else None,
                result_lineage_fingerprint=result_lineage_fingerprint,
            )
            return readiness_snapshot, run_failure
        return None, None

    @staticmethod
    def _statistical_result_lineage_fingerprint(
        detail: StatisticalResultDetailView,
        artifacts: list[Any] | tuple[ArtifactView, ...],
        result_artifact_ids: tuple[str, ...],
    ) -> str:
        result_artifact_ids_set = set(result_artifact_ids)
        return content_hash(
            {
                "result_id": detail.result_id,
                "result_version": detail.version,
                "design_revision_id": detail.design_revision_id,
                "bundle_hash": detail.bundle_hash,
                "artifacts": [
                    {
                        "artifact_id": artifact.artifact_id,
                        "checksum": artifact.checksum,
                        "size_bytes": artifact.size_bytes,
                        "provenance_hash": content_hash(artifact.provenance),
                    }
                    for artifact in sorted(artifacts, key=lambda item: item.artifact_id)
                    if artifact.artifact_id in result_artifact_ids_set
                ],
            }
        )

    @staticmethod
    def _verify_statistical_result(
        *,
        summary: StatisticalResultView,
        detail: StatisticalResultDetailView,
        plan: Any,
        design: StatisticalDesignRevision,
        run_id: str,
        project_id: str,
        registered_artifacts: list[Any],
    ) -> tuple[str, ...]:
        manifest, report_design, report = verified_result_design(detail)
        report_result = report.get("result")
        frozen_bindings = report.get("frozen_bindings")
        if not isinstance(report_result, Mapping) or not isinstance(frozen_bindings, Mapping):
            raise ValueError("report identities are missing")
        detail_identity = (
            detail.result_id,
            detail.project_id,
            detail.run_id,
            detail.design_revision_id,
            detail.mode,
            detail.non_scientific,
            detail.bundle_hash,
            detail.version,
            detail.artifact_count,
            detail.cluster_count,
        )
        summary_identity = (
            summary.result_id,
            summary.project_id,
            summary.run_id,
            summary.design_revision_id,
            summary.mode,
            summary.non_scientific,
            summary.bundle_hash,
            summary.version,
            summary.artifact_count,
            summary.cluster_count,
        )
        if detail_identity != summary_identity:
            raise ValueError("result list/detail identities disagree")
        if (
            detail.project_id != project_id
            or detail.run_id != run_id
            or detail.design_revision_id != design.revision_id
            or report_design.revision_id != design.revision_id
            or manifest.design_revision_id != design.revision_id
            or manifest.run_id != run_id
            or manifest.result_id != detail.result_id
            or frozen_bindings.get("design_revision_id") != design.revision_id
            or frozen_bindings.get("plan_hash") != plan.plan_hash
            or report_result.get("result_id") != detail.result_id
            or report_result.get("run_id") != run_id
            or report_result.get("mode") != detail.mode
            or report_result.get("non_scientific") != detail.non_scientific
            or report_result.get("non_scientific_reason") != detail.non_scientific_reason
            or detail.artifact_count != len(manifest.artifacts)
            or detail.cluster_count != len(manifest.clusters)
        ):
            raise ValueError("report, manifest, detail, Plan or Run identities disagree")
        if (
            detail.mode != StatisticalResultMode.REAL.value
            or manifest.mode is not StatisticalResultMode.REAL
            or detail.non_scientific
            or manifest.non_scientific
            or any(artifact.placeholder for artifact in manifest.artifacts)
        ):
            raise ValueError("result is synthetic, marked non-scientific, or has placeholders")

        registered_by_id = {item.artifact_id: item for item in registered_artifacts}
        artifact_ids: list[str] = []
        for metadata in manifest.artifacts:
            artifact = registered_by_id.get(metadata.artifact_id)
            if artifact is None:
                raise ValueError("result manifest Artifact is not registered for this Run")
            if (
                artifact.project_id != project_id
                or artifact.run_id != run_id
                or artifact.artifact_type != metadata.artifact_type
                or artifact.relative_path != metadata.relative_path
                or artifact.checksum != metadata.checksum_sha256
                or artifact.size_bytes != metadata.size_bytes
                or content_hash(artifact.provenance) != metadata.provenance_hash
            ):
                raise ValueError(
                    "result manifest Artifact metadata differs from registered Artifact"
                )
            artifact_ids.append(metadata.artifact_id)
        if len(set(artifact_ids)) != len(artifact_ids) or not artifact_ids:
            raise ValueError("verified statistical result Artifact IDs are empty or duplicated")
        return tuple(sorted(artifact_ids))

    def _apply_statistical_task_state(
        self,
        task: WorkTask,
        run_id: str,
        receipts: tuple[EventReconciliationReceipt, ...],
        snapshot: AuthoritativeRunSnapshot,
        *,
        target: TaskStatus,
        readiness: RunReadiness,
        waiting_condition: WaitingCondition,
        reason: str,
        claim_token: str | None = None,
    ) -> WorkflowRunReconciliation:
        updated = task
        if task.status is not target or task.waiting_condition != waiting_condition:
            if task.status is TaskStatus.WAITING_DEPENDENCY and target is TaskStatus.WAITING_RUN:
                return self._block_statistical_task(
                    task,
                    run_id,
                    receipts,
                    reason="统计任务等待结果时 Run 又回到非终态，任务与 Workflow 状态不一致。",
                    authoritative=snapshot,
                    claim_token=claim_token,
                )
            if task.status is not target:
                updated = transition_task(task, target, waiting_condition=waiting_condition)
            else:
                updated = WorkTask.model_validate(
                    {
                        **task.model_dump(),
                        "waiting_condition": waiting_condition.model_dump(),
                        "version": task.version + 1,
                    }
                )
            updated = self._save_task(
                updated,
                expected_version=task.version,
                claim_token=claim_token,
            )
        return WorkflowRunReconciliation(
            task=updated,
            readiness=readiness,
            event_receipts=receipts,
            authoritative=snapshot,
            reason=reason,
        )

    def _complete_statistical_task(
        self,
        task: WorkTask,
        run_id: str,
        receipts: tuple[EventReconciliationReceipt, ...],
        snapshot: AuthoritativeRunSnapshot,
        *,
        readiness_assessment: _StatisticalReadinessAssessment | None = None,
        claim_token: str | None = None,
    ) -> WorkflowRunReconciliation:
        step = self._statistical_step(task)
        if (
            step is None
            or step.status is not TaskStepStatus.WAITING
            or snapshot.verified_statistical_result_id is None
            or not snapshot.result_artifact_ids
        ):
            return self._block_statistical_task(
                task,
                run_id,
                receipts,
                reason="统计结果虽存在，但 WorkTask 步骤或 Artifact 完成绑定无效；任务已阻断。",
                authoritative=snapshot,
                claim_token=claim_token,
            )
        result_artifact_ids = tuple(sorted(set(snapshot.result_artifact_ids)))
        completed = complete_step(task, step, artifact_ids=result_artifact_ids)
        try:
            finalization_record = self._statistical_result_interpretation_record(
                task=task,
                run_id=run_id,
                snapshot=snapshot,
                result_artifact_ids=result_artifact_ids,
            )
        except (
            NotFoundError,
            ConflictError,
            InputValidationError,
            ValueError,
            TypeError,
            KeyError,
            ValidationError,
        ) as exc:
            if readiness_assessment is not None:
                return WorkflowRunReconciliation(
                    task=task,
                    readiness=RunReadiness.INDETERMINATE,
                    event_receipts=receipts,
                    authoritative=snapshot,
                    reason=(
                        "统计最终权威重读暂无法与 readiness 证据一致匹配；"
                        "本次未写入 Task 或解释续接记录。"
                    ),
                )
            return self._block_statistical_task(
                task,
                run_id,
                receipts,
                reason=(
                    "统计结果通过初步核验，但无法建立稳定的解释续接绑定；"
                    f"任务已阻断。({type(exc).__name__})"
                ),
                authoritative=snapshot,
                claim_token=claim_token,
            )
        if readiness_assessment is not None:
            evidence = readiness_assessment.evidence
            if (
                not isinstance(finalization_record, _StatisticalFinalizationEvidence)
                or evidence is None
                or not self._statistical_finalization_matches_assessment(
                    finalization_record, evidence
                )
            ):
                return WorkflowRunReconciliation(
                    task=task,
                    readiness=RunReadiness.INDETERMINATE,
                    event_receipts=receipts,
                    authoritative=snapshot,
                    reason="统计 readiness 证据与最终重读的结果版本或 lineage 不一致；本次未收口。",
                )
            outbox = finalization_record.outbox
        elif isinstance(finalization_record, _StatisticalFinalizationEvidence):
            outbox = finalization_record.outbox
        else:
            # Kept for existing internal callers that predate readiness handlers.
            outbox = finalization_record
        # WorkTask completion and durable READY enqueue share the same SQLite
        # UoW. A failed outbox write rolls back the completed task as well.
        with self._repository.atomic():
            completed = self._save_task(
                WorkTask.model_validate(completed.model_dump()),
                expected_version=task.version,
                claim_token=claim_token,
            )
            self._repository.ensure_result_interpretation_outbox(outbox)
        return WorkflowRunReconciliation(
            task=completed,
            readiness=RunReadiness.COMPLETED,
            event_receipts=receipts,
            authoritative=snapshot,
            reason="唯一真实统计结果及其 Artifact 已与冻结设计和 Plan 绑定并通过核验；任务已完成。",
        )

    @staticmethod
    def _statistical_finalization_matches_assessment(
        finalization: _StatisticalFinalizationEvidence,
        evidence: StepReadinessEvidenceSummary,
    ) -> bool:
        outbox = finalization.outbox
        return (
            evidence.result_kind is StepReadinessResultKind.STATISTICAL_RESULT
            and evidence.result_identity == outbox.result_id
            and evidence.result_version == outbox.result_version
            and evidence.result_bundle_fingerprint == outbox.result_bundle_hash
            and evidence.plan_revision_id == outbox.plan_revision_id
            and evidence.lineage_fingerprint == finalization.lineage_fingerprint
        )

    def _statistical_result_interpretation_record(
        self,
        *,
        task: WorkTask,
        run_id: str,
        snapshot: AuthoritativeRunSnapshot,
        result_artifact_ids: tuple[str, ...],
    ) -> _StatisticalFinalizationEvidence:
        """Build the immutable outbox identity from the already verified result chain."""
        conversation_id = task.authority_refs.conversation_id
        plan_id = task.authority_refs.plan_revision_id
        result_id = snapshot.verified_statistical_result_id
        step = self._statistical_step(task)
        if conversation_id is None or plan_id is None or result_id is None or step is None:
            raise ConflictError(
                "result_interpretation_binding_missing",
                "已完成统计任务缺少会话、Plan、步骤或真实结果引用。",
            )
        conversation = self._conversation_repository.get_conversation(conversation_id)
        run = self._run_repository.get_run(run_id)
        self._require_work_conversation(conversation, conversation_id, run.project_id)
        if conversation.project_id != run.project_id:
            raise ConflictError(
                "result_interpretation_conversation_mismatch",
                "统计会话没有绑定到结果所属项目。",
            )
        if (
            run.run_id != run_id
            or run.state is not WorkflowState.SUCCEEDED
            or run.version != snapshot.run_version
            or run.plan_revision_id != plan_id
        ):
            raise ConflictError(
                "result_interpretation_run_changed",
                "统计 Run 在完成续接绑定前发生变化。",
            )
        plan = self._plan_repository.get_plan(plan_id)
        if plan.project_id != run.project_id or plan.state is not PlanState.APPROVED:
            raise ConflictError(
                "result_interpretation_plan_mismatch", "统计 Plan 不是当前 Run 的已批准方案。"
            )
        context, design = self._statistical_plan_context(
            plan,
            conversation_id=conversation_id,
            project_id=run.project_id,
            proposal_hash=step.input_version or "",
        )
        if (
            not self._analysis_proposal_task_identity_matches(
                task, conversation_id, context["proposal_hash"]
            )
            or task.authority_refs.input_revision != context["draft_hash"]
        ):
            raise ConflictError(
                "result_interpretation_task_binding_mismatch",
                "统计 WorkTask 与冻结统计提案不匹配。",
            )
        summaries = self._artifact_repository.list_statistical_results(
            project_id=run.project_id, run_id=run_id
        )
        if len(summaries) != 1 or summaries[0].result_id != result_id:
            raise ConflictError(
                "result_interpretation_result_not_unique", "Run 没有唯一且匹配的统计结果。"
            )
        detail = self._artifact_repository.get_statistical_result(result_id)
        artifacts = self._artifact_repository.list_artifacts(run_id)
        verified_artifact_ids = self._verify_statistical_result(
            summary=summaries[0],
            detail=detail,
            plan=plan,
            design=design,
            run_id=run_id,
            project_id=run.project_id,
            registered_artifacts=artifacts,
        )
        if (
            verified_artifact_ids != result_artifact_ids
            or detail.mode != StatisticalResultMode.REAL.value
            or detail.non_scientific
        ):
            raise ConflictError(
                "result_interpretation_result_not_scientific",
                "结果不是与当前 WorkTask 绑定的真实科学 StatisticalResult。",
            )
        # Verify the reproducibility bundle now, before publishing READY.
        result_model_facts(detail)
        return _StatisticalFinalizationEvidence(
            outbox=StatisticalResultInterpretationOutbox(
                task_id=task.task_id,
                conversation_id=conversation_id,
                project_id=run.project_id,
                run_id=run_id,
                plan_revision_id=plan_id,
                result_id=detail.result_id,
                result_bundle_hash=detail.bundle_hash,
                result_version=detail.version,
                artifact_ids=verified_artifact_ids,
            ),
            lineage_fingerprint=self._statistical_result_lineage_fingerprint(
                detail,
                artifacts,
                verified_artifact_ids,
            ),
        )

    def _block_statistical_task(
        self,
        task: WorkTask,
        run_id: str,
        receipts: tuple[EventReconciliationReceipt, ...],
        *,
        reason: str,
        authoritative: AuthoritativeRunSnapshot | None = None,
        claim_token: str | None = None,
    ) -> WorkflowRunReconciliation:
        updated = task
        if task.status in {TaskStatus.WAITING_RUN, TaskStatus.WAITING_DEPENDENCY}:
            updates: dict[str, Any] = {}
            step = self._statistical_step(task)
            if step is not None and step.status is TaskStepStatus.WAITING:
                blocked_step = transition_task_step(step, TaskStepStatus.BLOCKED)
                updates["steps"] = tuple(
                    blocked_step if item.step_id == step.step_id else item for item in task.steps
                )
            updated = transition_task(task, TaskStatus.BLOCKED).model_copy(update=updates)
            updated = self._save_task(
                WorkTask.model_validate(updated.model_dump()),
                expected_version=task.version,
                claim_token=claim_token,
            )
        return WorkflowRunReconciliation(
            task=updated,
            readiness=RunReadiness.BLOCKED,
            event_receipts=receipts,
            authoritative=authoritative,
            reason=reason,
        )

    @staticmethod
    def _has_statistical_step(task: WorkTask) -> bool:
        return any(step.kind == "statistical_run" for step in task.steps)

    @staticmethod
    def _preprocessing_step(task: WorkTask) -> TaskStep | None:
        steps = tuple(step for step in task.steps if step.kind == "preprocessing_run")
        return steps[0] if len(steps) == 1 else None

    def _derive_preprocessing_expectations(
        self,
        plan: Any,
        project_id: str,
    ) -> tuple[DpabiPreprocessingOutput, ...]:
        """Rebuild exact DPABI output identities from a frozen Plan and manifest."""

        if plan.project_id != project_id or plan.plan.get("kind") != "skill_plan":
            raise _InvalidPreprocessingAuthority("Plan project or kind does not match the task")
        manifest_id = plan.plan.get("dataset_manifest_id")
        raw_skill_plan = plan.plan.get("skill_plan")
        if not isinstance(manifest_id, str) or not isinstance(raw_skill_plan, Mapping):
            raise _InvalidPreprocessingAuthority("frozen Plan is missing its manifest or SkillPlan")
        skill_plan = SkillPlan.model_validate(raw_skill_plan)
        body = skill_plan.model_dump(mode="json")
        for key in ("plan_id", "plan_hash", "warnings"):
            body.pop(key, None)
        if (
            skill_plan.plan_hash != plan.plan_hash
            or stable_hash(body) != plan.plan_hash
            or skill_plan.project_id != project_id
            or skill_plan.input_manifest_hash != plan.manifest_hash
        ):
            raise _InvalidPreprocessingAuthority("frozen SkillPlan hash or project binding differs")

        manifest = self._project_repository.get_manifest(manifest_id)
        manifest_content = self._project_repository.get_manifest_content(manifest_id)
        dataset = self._project_repository.get_dataset(manifest.dataset_id)
        if (
            dataset.project_id != project_id
            or skill_plan.dataset_ref != manifest.dataset_id
            or manifest.content_hash != plan.manifest_hash
            or content_hash(manifest_content) != manifest.content_hash
        ):
            raise _InvalidPreprocessingAuthority("frozen dataset manifest binding or hash differs")
        subject_ids = tuple(subject.subject_id for subject in manifest.subjects)
        if not subject_ids or len(subject_ids) != len(set(subject_ids)):
            raise _InvalidPreprocessingAuthority(
                "frozen manifest subject identities are empty or duplicated"
            )

        resolved = dict(skill_plan.resolved_parameters)
        if len(resolved) != len(skill_plan.resolved_parameters):
            raise _InvalidPreprocessingAuthority("frozen SkillPlan has duplicate parameter names")
        allowed = {"preprocessing", "alff_falff", "reho", "base_cfg_artifact_id"}
        if set(resolved) - allowed:
            raise _InvalidPreprocessingAuthority("frozen SkillPlan contains unsupported parameters")
        preprocessing = (
            PreprocessingParameters.model_validate(resolved["preprocessing"])
            if "preprocessing" in resolved
            else None
        )
        alff = (
            AlffFalffParameters.model_validate(resolved["alff_falff"])
            if "alff_falff" in resolved
            else None
        )
        reho = RehoParameters.model_validate(resolved["reho"]) if "reho" in resolved else None
        if preprocessing is None or skill_plan.preprocessing_parameters_hash != stable_hash(
            preprocessing.model_dump(mode="json")
        ):
            raise _InvalidPreprocessingAuthority(
                "preprocessing parameter hash differs from SkillPlan"
            )
        metric_parameters = tuple(item for item in (alff, reho) if item is not None)
        mask_sources = {item.mask_source for item in metric_parameters}
        if len(mask_sources) > 1:
            raise _InvalidPreprocessingAuthority("requested metrics disagree on their mask source")
        auto_mask = (
            bool(metric_parameters) and next(iter(mask_sources)) is MetricMaskSource.DPABI_AUTO
        )
        outputs = enumerate_dpabi_preprocessing_outputs(
            subject_ids,
            alff,
            reho,
            auto_mask=auto_mask,
        )
        if len(outputs) > 20_000:
            raise _InvalidPreprocessingAuthority(
                "frozen preprocessing output contract exceeds the durable WorkTask limit"
            )
        return outputs

    def _save_task(
        self,
        task: WorkTask,
        *,
        expected_version: int,
        claim_token: str | None,
    ) -> WorkTask:
        if claim_token is None:
            return self._repository.save_task(task, expected_version=expected_version)
        return self._repository.save_task(
            task,
            expected_version=expected_version,
            claim_token=claim_token,
        )

    def _read_stable_authoritative_snapshot(
        self,
        task: WorkTask,
        run_id: str,
        *,
        max_attempts: int = 3,
    ) -> _StableRunAuthority | None:
        """Read dependent authority records bracketed by a stable Run version."""

        for _attempt in range(max_attempts):
            run_before = self._run_repository.get_run(run_id)
            if run_before.run_id != run_id:
                raise InputValidationError(
                    "authoritative_run_mismatch",
                    "运行读取端口返回了与任务绑定 ID 不一致的记录。",
                    expected_run_id=run_id,
                    actual_run_id=run_before.run_id,
                )

            review = self._qc_repository.get_latest_qc_review_for_run(run_id)
            skip = self._qc_repository.get_qc_skip_for_run(run_id)
            artifacts = self._artifact_repository.list_artifacts(run_id)
            if review is not None and review.run_id != run_id:
                raise InputValidationError(
                    "authoritative_qc_mismatch",
                    "QC 读取端口返回了其他运行的审查记录。",
                    expected_run_id=run_id,
                    actual_run_id=review.run_id,
                )
            if skip is not None and skip.run_id != run_id:
                raise InputValidationError(
                    "authoritative_qc_skip_mismatch",
                    "QC 读取端口返回了其他运行的跳过记录。",
                    expected_run_id=run_id,
                    actual_run_id=skip.run_id,
                )
            if any(
                artifact.run_id != run_id or artifact.project_id != run_before.project_id
                for artifact in artifacts
            ):
                raise InputValidationError(
                    "authoritative_artifact_mismatch",
                    "Artifact 读取端口返回了其他项目或运行的产物。",
                    expected_run_id=run_id,
                )
            artifact_ids = [artifact.artifact_id for artifact in artifacts]
            if len(artifact_ids) != len(set(artifact_ids)):
                raise InputValidationError(
                    "authoritative_artifact_duplicate",
                    "Artifact 读取端口返回了重复的 Artifact ID。",
                    expected_run_id=run_id,
                )

            resolved_references: list[str] = []
            unresolved_references: list[str] = []
            for artifact_id in sorted(set(task.authority_refs.artifact_ids)):
                try:
                    referenced_artifact = self._artifact_repository.get_artifact(artifact_id)
                except NotFoundError:
                    unresolved_references.append(artifact_id)
                    continue
                if (
                    referenced_artifact.artifact_id != artifact_id
                    or referenced_artifact.project_id != run_before.project_id
                ):
                    unresolved_references.append(artifact_id)
                else:
                    resolved_references.append(artifact_id)

            run_after = self._run_repository.get_run(run_id)
            if run_after.run_id != run_id:
                raise InputValidationError(
                    "authoritative_run_mismatch",
                    "运行读取端口返回了与任务绑定 ID 不一致的记录。",
                    expected_run_id=run_id,
                    actual_run_id=run_after.run_id,
                )
            if run_before.version != run_after.version:
                continue
            if run_before.project_id != run_after.project_id or getattr(
                run_before, "plan_revision_id", None
            ) != getattr(run_after, "plan_revision_id", None):
                raise InputValidationError(
                    "authoritative_run_binding_changed",
                    "Workflow Run 的项目或 Plan 绑定在读取期间发生变化。",
                    expected_run_id=run_id,
                )

            registered_ids = {artifact.artifact_id for artifact in artifacts}
            snapshot = AuthoritativeRunSnapshot(
                run_id=run_after.run_id,
                run_state=run_after.state.value,
                run_version=run_after.version,
                attempt=run_after.attempt,
                qc_mode=run_after.qc_mode.value,
                qc_status=run_after.qc_status,
                latest_qc_review_id=(
                    review.review.review_revision_id if review is not None else None
                ),
                latest_qc_review_state=review.state.value if review is not None else None,
                qc_skip_id=skip.qc_skip_id if skip is not None else None,
                registered_artifact_ids=tuple(sorted(registered_ids)),
                registered_artifact_types=tuple(sorted({item.artifact_type for item in artifacts})),
                resolved_referenced_artifact_ids=tuple(resolved_references),
                unresolved_referenced_artifact_ids=tuple(unresolved_references),
            )
            return _StableRunAuthority(
                snapshot,
                project_id=run_after.project_id,
                plan_revision_id=getattr(run_after, "plan_revision_id", None),
                artifacts=tuple(artifacts),
            )
        return None
