"""Immutable QC reviews and approvals."""

from __future__ import annotations

from datetime import UTC, datetime

from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from neuroagent.application.contracts import (
    QcReviewApprove,
    QcReviewCreate,
    QcReviewState,
    QcReviewView,
    RunView,
    WorkflowState,
)
from neuroagent.application.errors import ConflictError, InputValidationError, NotFoundError
from neuroagent.application.hashing import canonical_json, content_hash
from neuroagent.domain.fmri.artifacts import ArtifactKind, ArtifactLineage
from neuroagent.domain.fmri.qc import QcReviewRevision
from neuroagent.infrastructure.persistence.models import (
    ArtifactRow,
    DatasetRow,
    JobRow,
    ManifestRevisionRow,
    PlanRevisionRow,
    QcApprovalRow,
    QcReviewRow,
    WorkflowRunRow,
)
from neuroagent.infrastructure.persistence.repository_mixins._base import (
    RepositoryBaseMixin,
    _id,
    _load,
)
from neuroagent.workflow.state import validate_workflow_transition


class QcReviewMixin(RepositoryBaseMixin):
    # -- immutable QC reviews and approvals -------------------------------

    @staticmethod
    def _artifact_lineage(row: ArtifactRow) -> ArtifactLineage:
        provenance = _load(row.provenance_json, {})
        lineage_data = provenance.get("lineage") if isinstance(provenance, dict) else None
        if not isinstance(lineage_data, dict):
            raise InputValidationError(
                "artifact_lineage_missing",
                "科研 Artifact 必须登记服务端校验的类型化 lineage。",
                artifact_id=row.artifact_id,
            )
        try:
            lineage = ArtifactLineage.model_validate(lineage_data)
        except ValidationError as exc:
            raise InputValidationError(
                "artifact_lineage_invalid",
                "科研 Artifact 的登记 lineage 无效。",
                artifact_id=row.artifact_id,
            ) from exc
        if lineage.artifact_id != row.artifact_id:
            raise InputValidationError(
                "artifact_lineage_identity_mismatch",
                "Artifact ID 与登记 lineage 不一致。",
                artifact_id=row.artifact_id,
            )
        return lineage

    @staticmethod
    def _manifest_subject_ids_for_plan(session: Session, plan: PlanRevisionRow) -> tuple[str, ...]:
        plan_json = _load(plan.plan_json, {})
        manifest_id = plan_json.get("dataset_manifest_id")
        rows: list[ManifestRevisionRow]
        if isinstance(manifest_id, str):
            manifest = session.get(ManifestRevisionRow, manifest_id)
            if manifest is None:
                raise NotFoundError("manifest_revision", manifest_id)
            dataset = session.get(DatasetRow, manifest.dataset_id)
            if dataset is None or dataset.project_id != plan.project_id:
                raise ConflictError(
                    "qc_manifest_project_mismatch",
                    "来源计划的 manifest 不属于当前项目。",
                )
            rows = [manifest]
        else:
            rows = list(
                session.scalars(
                    select(ManifestRevisionRow)
                    .join(DatasetRow, DatasetRow.dataset_id == ManifestRevisionRow.dataset_id)
                    .where(
                        DatasetRow.project_id == plan.project_id,
                        ManifestRevisionRow.content_hash == plan.manifest_hash,
                    )
                    .order_by(ManifestRevisionRow.created_at.desc())
                ).all()
            )
        if not rows or any(row.content_hash != plan.manifest_hash for row in rows):
            raise ConflictError(
                "qc_manifest_unavailable",
                "QC 必须绑定可验证的冻结 manifest revision。",
            )
        subject_sets: set[tuple[str, ...]] = set()
        for row in rows:
            content = _load(row.content_json, {})
            subjects = content.get("subjects")
            if not isinstance(subjects, list):
                raise InputValidationError(
                    "qc_manifest_invalid",
                    "冻结 manifest 缺少结构化受试者清单。",
                )
            ordered_subject_ids: list[str] = []
            seen_subject_ids: set[str] = set()
            seen_entries: set[tuple[str, str | None]] = set()
            for item in subjects:
                if not isinstance(item, dict):
                    raise InputValidationError(
                        "qc_manifest_subjects_invalid",
                        "冻结 manifest 的受试者/会话条目格式无效。",
                    )
                subject_id = item.get("subject_id")
                session_id = item.get("session_id")
                if not isinstance(subject_id, str) or not subject_id.strip():
                    raise InputValidationError(
                        "qc_manifest_subjects_invalid",
                        "冻结 manifest 的受试者 ID 无效。",
                    )
                if session_id is not None and (
                    not isinstance(session_id, str) or not session_id.strip()
                ):
                    raise InputValidationError(
                        "qc_manifest_subjects_invalid",
                        "冻结 manifest 的会话 ID 无效。",
                    )
                entry_key = (subject_id, session_id)
                if entry_key in seen_entries:
                    raise InputValidationError(
                        "qc_manifest_subjects_invalid",
                        "冻结 manifest 包含重复的受试者/会话条目。",
                    )
                seen_entries.add(entry_key)
                if subject_id not in seen_subject_ids:
                    seen_subject_ids.add(subject_id)
                    ordered_subject_ids.append(subject_id)
            subject_sets.add(tuple(ordered_subject_ids))
        if len(subject_sets) != 1:
            raise ConflictError(
                "qc_manifest_ambiguous",
                "同一 manifest hash 对应不一致的受试者清单。",
            )
        return next(iter(subject_sets))

    def create_qc_review(self, request: QcReviewCreate) -> QcReviewView:
        with self._write_session() as session:
            run = session.get(WorkflowRunRow, request.run_id)
            if run is None:
                raise NotFoundError("run", request.run_id)
            self._check_version("run", run.version, request.expected_run_version)
            if WorkflowState(run.state) is not WorkflowState.QC_REVIEW:
                raise ConflictError(
                    "qc_not_pending",
                    "运行当前不处于 QC 审核状态。",
                    state=run.state,
                )
            plan = session.get(PlanRevisionRow, run.plan_revision_id)
            if plan is None:
                raise NotFoundError("plan_revision", run.plan_revision_id)
            manifest_subject_ids = self._manifest_subject_ids_for_plan(session, plan)
            included = set(request.included_subject_ids)
            excluded = set(request.excluded_subject_ids)
            if included | excluded != set(manifest_subject_ids):
                raise InputValidationError(
                    "qc_subject_manifest_mismatch",
                    "QC 纳入与排除受试者必须完整且精确地划分冻结 manifest。",
                    expected_subject_ids=list(manifest_subject_ids),
                    received_subject_ids=sorted(included | excluded),
                )
            if len(set(request.metric_artifact_ids)) != len(request.metric_artifact_ids):
                raise InputValidationError(
                    "duplicate_qc_artifact",
                    "QC 指标 Artifact ID 必须唯一。",
                )
            referenced_ids = {
                *request.metric_artifact_ids,
                *(
                    artifact_id
                    for check in request.checks
                    for artifact_id in check.evidence_artifact_ids
                ),
            }
            artifact_rows: dict[str, ArtifactRow] = {}
            for artifact_id in referenced_ids:
                artifact = session.get(ArtifactRow, artifact_id)
                if artifact is None:
                    raise NotFoundError("artifact", artifact_id)
                if artifact.run_id != request.run_id:
                    raise ConflictError(
                        "qc_artifact_run_mismatch",
                        "QC 只能引用当前运行产生的 Artifact。",
                        artifact_id=artifact_id,
                    )
                artifact_rows[artifact_id] = artifact
            metric_kinds = {
                ArtifactKind.ALFF_MAP,
                ArtifactKind.FALFF_MAP,
                ArtifactKind.REHO_MAP,
            }
            metric_subjects: set[str] = set()
            metric_keys: set[tuple[object, ...]] = set()
            for artifact_id in request.metric_artifact_ids:
                lineage = self._artifact_lineage(artifact_rows[artifact_id])
                if lineage.kind not in metric_kinds or not lineage.metadata_verified:
                    raise InputValidationError(
                        "qc_metric_lineage_invalid",
                        "QC 指标输入必须是具有已验证元数据的受试者级 ALFF/fALFF/ReHo Artifact。",
                        artifact_id=artifact_id,
                    )
                if lineage.subject_manifest_hash != plan.manifest_hash:
                    raise ConflictError(
                        "qc_artifact_manifest_mismatch",
                        "QC 指标 Artifact 与冻结 manifest 不一致。",
                        artifact_id=artifact_id,
                    )
                assert lineage.subject_id is not None
                if lineage.subject_id not in set(manifest_subject_ids):
                    raise InputValidationError(
                        "qc_artifact_subject_unknown",
                        "QC 指标 Artifact 的 subject_id 不属于冻结 manifest。",
                        artifact_id=artifact_id,
                        subject_id=lineage.subject_id,
                    )
                key = (
                    lineage.subject_id,
                    lineage.session_id,
                    lineage.condition,
                    lineage.kind,
                    lineage.metric_scaling,
                )
                if key in metric_keys:
                    raise InputValidationError(
                        "duplicate_qc_metric_lineage",
                        "QC 指标清单包含重复的受试者/会话/条件/指标/缩放组合。",
                        artifact_id=artifact_id,
                    )
                metric_keys.add(key)
                metric_subjects.add(lineage.subject_id)
                assert lineage.mask_artifact_id is not None
                mask_row = session.get(ArtifactRow, lineage.mask_artifact_id)
                if mask_row is None or mask_row.run_id != request.run_id:
                    raise InputValidationError(
                        "qc_metric_mask_invalid",
                        "QC 指标 lineage 引用的 mask 必须属于当前运行。",
                        artifact_id=artifact_id,
                    )
                mask_lineage = self._artifact_lineage(mask_row)
                if (
                    mask_lineage.kind is not ArtifactKind.BRAIN_MASK
                    or not mask_lineage.metadata_verified
                    or mask_lineage.subject_manifest_hash != plan.manifest_hash
                    or mask_lineage.grid_signature != lineage.grid_signature
                ):
                    raise InputValidationError(
                        "qc_metric_mask_incompatible",
                        "QC 指标必须引用同 manifest、同网格且元数据已验证的类型化 mask。",
                        artifact_id=artifact_id,
                    )
            missing_included = included - metric_subjects
            if missing_included:
                raise InputValidationError(
                    "qc_included_subject_artifact_missing",
                    "每个纳入统计的受试者都必须至少有一个 QC 指标 Artifact。",
                    subject_ids=sorted(missing_included),
                )
            revision = (
                session.scalar(
                    select(func.max(QcReviewRow.revision)).where(
                        QcReviewRow.run_id == request.run_id
                    )
                )
                or 0
            ) + 1
            review_revision_id = _id()
            content = {
                "input_manifest_hash": plan.manifest_hash,
                "metric_artifact_ids": list(request.metric_artifact_ids),
                "checks": [check.model_dump(mode="json") for check in request.checks],
                "included_subject_ids": list(request.included_subject_ids),
                "excluded_subject_ids": list(request.excluded_subject_ids),
                "exclusion_reasons": [list(item) for item in request.exclusion_reasons],
            }
            review_hash = content_hash(content)
            try:
                QcReviewRevision.model_validate(
                    {
                        "review_revision_id": review_revision_id,
                        **content,
                        "approved": False,
                        "approved_by": None,
                        "approval_reason": None,
                        "content_hash": review_hash,
                    }
                )
            except ValidationError as exc:
                raise InputValidationError(
                    "qc_review_invalid",
                    "QC review 内容未通过完整性校验。",
                ) from exc
            row = QcReviewRow(
                review_revision_id=review_revision_id,
                run_id=request.run_id,
                project_id=run.project_id,
                revision=revision,
                version=1,
                state=QcReviewState.DRAFT.value,
                content_hash=review_hash,
                content_json=canonical_json(content),
            )
            session.add(row)
            session.flush()
            return self._qc_review(row)

    def get_qc_review(self, review_revision_id: str) -> QcReviewView:
        with self.database.session_factory() as session:
            row = session.get(QcReviewRow, review_revision_id)
            if row is None:
                raise NotFoundError("qc_review_revision", review_revision_id)
            approval = session.scalar(
                select(QcApprovalRow)
                .where(QcApprovalRow.review_revision_id == review_revision_id)
                .order_by(QcApprovalRow.created_at.desc())
                .limit(1)
            )
            return self._qc_review(row, approval)

    def approve_qc_review(
        self, review_revision_id: str, request: QcReviewApprove
    ) -> tuple[QcReviewView, RunView]:
        with self._write_session() as session:
            row = session.get(QcReviewRow, review_revision_id)
            if row is None:
                raise NotFoundError("qc_review_revision", review_revision_id)
            self._check_version("qc_review_revision", row.version, request.expected_review_version)
            if QcReviewState(row.state) is not QcReviewState.DRAFT:
                raise ConflictError(
                    "qc_review_not_draft",
                    "只有 draft 状态的 QC review 可以审批。",
                    state=row.state,
                )
            if row.content_hash != request.review_hash:
                raise ConflictError(
                    "qc_review_hash_mismatch",
                    "QC 审批哈希与冻结内容不一致。",
                    expected=row.content_hash,
                    received=request.review_hash,
                )
            run = session.get(WorkflowRunRow, row.run_id)
            if run is None:
                raise NotFoundError("run", row.run_id)
            self._check_version("run", run.version, request.expected_run_version)
            if WorkflowState(run.state) is not WorkflowState.QC_REVIEW:
                raise ConflictError(
                    "qc_not_pending",
                    "运行当前不处于 QC 审核状态。",
                    state=run.state,
                )
            draft = self._qc_review(row).review
            try:
                QcReviewRevision.model_validate(
                    {
                        **draft.model_dump(mode="json"),
                        "approved": request.approved,
                        "approved_by": request.actor if request.approved else None,
                        "approval_reason": request.reason if request.approved else None,
                    }
                )
            except ValidationError as exc:
                raise InputValidationError(
                    "qc_review_approval_blocked",
                    "QC review 存在阻断检查或审批证据不完整。",
                ) from exc
            approval = QcApprovalRow(
                qc_approval_id=_id(),
                review_revision_id=review_revision_id,
                review_hash=request.review_hash,
                actor=request.actor,
                decision="approved" if request.approved else "rejected",
                reason=request.reason,
            )
            session.add(approval)
            row.state = (
                QcReviewState.APPROVED.value if request.approved else QcReviewState.REJECTED.value
            )
            row.version += 1
            row.updated_at = datetime.now(UTC)
            target = WorkflowState.SUCCEEDED if request.approved else WorkflowState.FAILED_TERMINAL
            validate_workflow_transition(WorkflowState.QC_REVIEW, target)
            run.state = target.value
            run.error = None if request.approved else f"QC rejected: {request.reason}"
            run.version += 1
            run.updated_at = datetime.now(UTC)
            session.flush()
            return self._qc_review(row, approval if request.approved else None), self._run(run)

    def get_approved_qc_review(self, review_revision_id: str) -> QcReviewView:
        view = self.get_qc_review(review_revision_id)
        if view.state is not QcReviewState.APPROVED:
            raise ConflictError(
                "qc_review_not_approved",
                "统计设计只能引用已批准的 QC review revision。",
                state=view.state.value,
            )
        return view

    def retry_run(self, run_id: str, *, expected_version: int) -> RunView:
        with self._write_session() as session:
            run = session.get(WorkflowRunRow, run_id)
            if run is None:
                raise NotFoundError("run", run_id)
            self._check_version("run", run.version, expected_version)
            state = WorkflowState(run.state)
            if state not in {WorkflowState.FAILED_RETRYABLE, WorkflowState.TIMED_OUT}:
                raise ConflictError(
                    "run_not_retryable", "当前运行状态不允许重试。", state=state.value
                )
            job = session.scalar(select(JobRow).where(JobRow.run_id == run_id))
            if job is None:
                raise NotFoundError("job_for_run", run_id)
            if job.attempt >= job.max_attempts:
                raise ConflictError(
                    "retry_budget_exhausted",
                    "运行已耗尽已批准计划的重试预算。",
                    attempt=job.attempt,
                    max_attempts=job.max_attempts,
                )
            validate_workflow_transition(state, WorkflowState.QUEUED)
            run.state = WorkflowState.QUEUED.value
            run.version += 1
            run.error = None
            run.cancel_requested = False
            run.updated_at = datetime.now(UTC)
            job.state = "queued"
            job.lease_owner = None
            job.lease_expires_at = None
            job.updated_at = datetime.now(UTC)
            session.flush()
            return self._run(run)
