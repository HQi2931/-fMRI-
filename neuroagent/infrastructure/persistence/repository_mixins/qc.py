"""Immutable QC reviews and approvals."""

from __future__ import annotations

from datetime import UTC, datetime

from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from neuroagent.application.contracts.runs import (
    HeadMotionQcCreate,
    QcExecutionMode,
    QcReviewApprove,
    QcReviewCreate,
    QcReviewState,
    QcReviewView,
    QcSkipCreate,
    QcSkipView,
    RunView,
)
from neuroagent.application.errors import ConflictError, InputValidationError, NotFoundError
from neuroagent.application.hashing import canonical_json, content_hash
from neuroagent.domain.fmri.artifacts import ArtifactKind, ArtifactLineage
from neuroagent.domain.fmri.qc import (
    HeadMotionQcReport,
    QcCheck,
    QcReviewRevision,
    QcReviewScope,
    QcSeverity,
)
from neuroagent.domain.lifecycle import WorkflowState
from neuroagent.infrastructure.persistence.models import (
    ArtifactRow,
    DatasetRow,
    ManifestRevisionRow,
    PlanRevisionRow,
    QcApprovalRow,
    QcReviewRow,
    QcSkipRow,
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
            if request.review_scope is QcReviewScope.PRE_STATISTICS and missing_included:
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
                "review_scope": request.review_scope.value,
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

    def create_head_motion_qc_review(
        self, run_id: str, request: HeadMotionQcCreate
    ) -> QcReviewView:
        with self._write_session() as session:
            run = session.get(WorkflowRunRow, run_id)
            if run is None:
                raise NotFoundError("run", run_id)
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
            report_row = self._latest_head_motion_report_row(session, run_id, required=False)
            if report_row is None:
                content = self._source_head_motion_qc_content_for_metric_only(
                    session, run=run, plan=plan, required=True
                )
            else:
                content = self._head_motion_qc_content(
                    session,
                    run=run,
                    plan=plan,
                    report_row=report_row,
                    criteria=request.criteria.model_dump(mode="json"),
                )
            assert content is not None
            return self._insert_or_get_qc_review(session, run=run, content=content)

    def _auto_create_head_motion_qc_review_in_session(
        self,
        session: Session,
        *,
        run: WorkflowRunRow,
        plan: PlanRevisionRow,
        registered_rows: list[ArtifactRow],
    ) -> QcReviewView | None:
        reports = [row for row in registered_rows if row.artifact_type == "qc.head_motion_report"]
        if not reports:
            content = self._source_head_motion_qc_content_for_metric_only(
                session, run=run, plan=plan, required=False
            )
            if content is None:
                return None
            return self._insert_or_get_qc_review(session, run=run, content=content)
        if len(reports) != 1:
            raise InputValidationError(
                "head_motion_qc_report_ambiguous",
                "同一次运行只能登记一个头动 QC 报告。",
            )
        content = self._head_motion_qc_content(
            session,
            run=run,
            plan=plan,
            report_row=reports[0],
            criteria=None,
        )
        return self._insert_or_get_qc_review(session, run=run, content=content)

    @staticmethod
    def _latest_head_motion_report_row(
        session: Session, run_id: str, *, required: bool = True
    ) -> ArtifactRow | None:
        row = session.scalar(
            select(ArtifactRow)
            .where(
                ArtifactRow.run_id == run_id,
                ArtifactRow.artifact_type == "qc.head_motion_report",
            )
            .order_by(ArtifactRow.created_at.desc(), ArtifactRow.artifact_id.desc())
            .limit(1)
        )
        if row is None and required:
            raise ConflictError(
                "head_motion_qc_report_missing",
                "当前运行尚未登记头动 QC 报告。",
            )
        return row

    def _source_head_motion_qc_content_for_metric_only(
        self,
        session: Session,
        *,
        run: WorkflowRunRow,
        plan: PlanRevisionRow,
        required: bool,
    ) -> dict[str, object] | None:
        plan_json = _load(plan.plan_json, {})
        skill_plan = plan_json.get("skill_plan") if isinstance(plan_json, dict) else None
        if not isinstance(skill_plan, dict):
            if required:
                raise ConflictError("metric_only_qc_plan_missing", "运行计划缺少 SkillPlan。")
            return None
        input_artifact_id = skill_plan.get("input_artifact_id")
        if not isinstance(input_artifact_id, str) or not input_artifact_id:
            if required:
                raise ConflictError(
                    "metric_only_source_artifact_missing",
                    "metric-only 运行缺少来源预处理 Artifact。",
                )
            return None
        source_artifact = session.get(ArtifactRow, input_artifact_id)
        if source_artifact is None:
            if required:
                raise NotFoundError("artifact", input_artifact_id)
            return None
        source_lineage = self._artifact_lineage(source_artifact)
        if source_lineage.subject_manifest_hash != plan.manifest_hash:
            raise ConflictError(
                "metric_only_source_manifest_mismatch",
                "来源预处理 Artifact 与当前计划 manifest 不一致。",
            )
        source_review_row = session.scalar(
            select(QcReviewRow)
            .where(
                QcReviewRow.run_id == source_artifact.run_id,
                QcReviewRow.state == QcReviewState.APPROVED.value,
            )
            .order_by(QcReviewRow.revision.desc())
            .limit(1)
        )
        if source_review_row is None:
            if required:
                raise ConflictError(
                    "metric_only_source_head_motion_qc_missing",
                    "metric-only 运行需要来源预处理运行已批准的头动 QC。",
                )
            return None
        source_approval = session.scalar(
            select(QcApprovalRow)
            .where(QcApprovalRow.review_revision_id == source_review_row.review_revision_id)
            .order_by(QcApprovalRow.created_at.desc())
            .limit(1)
        )
        source_review = self._qc_review(source_review_row, source_approval)
        if source_review.review.review_scope not in {
            QcReviewScope.HEAD_MOTION,
            QcReviewScope.PRE_STATISTICS,
        }:
            if required:
                raise ConflictError(
                    "metric_only_source_head_motion_qc_scope_invalid",
                    "来源 QC 不是可复用的头动 QC。",
                )
            return None
        if source_review.review.input_manifest_hash != plan.manifest_hash:
            raise ConflictError(
                "metric_only_source_qc_manifest_mismatch",
                "来源头动 QC 与当前计划 manifest 不一致。",
            )
        metric_rows = list(
            session.scalars(
                select(ArtifactRow)
                .where(ArtifactRow.run_id == run.run_id, ArtifactRow.artifact_type.like("metric.%"))
                .order_by(ArtifactRow.created_at, ArtifactRow.artifact_id)
            ).all()
        )
        if not metric_rows:
            if required:
                raise ConflictError(
                    "metric_only_metric_artifacts_missing",
                    "当前运行尚未登记指标 Artifact。",
                )
            return None
        metric_subjects: set[str] = set()
        for row in metric_rows:
            lineage = self._artifact_lineage(row)
            if lineage.subject_manifest_hash != plan.manifest_hash:
                raise ConflictError(
                    "metric_only_artifact_manifest_mismatch",
                    "指标 Artifact 与冻结 manifest 不一致。",
                    artifact_id=row.artifact_id,
                )
            if lineage.subject_id is not None:
                metric_subjects.add(lineage.subject_id)
        missing_subjects = set(source_review.review.included_subject_ids) - metric_subjects
        if missing_subjects:
            raise InputValidationError(
                "metric_only_included_subject_artifact_missing",
                "来源 QC 纳入的受试者必须在本次指标运行中有指标 Artifact。",
                subject_ids=sorted(missing_subjects),
            )
        source_evidence_ids = tuple(
            dict.fromkeys(
                artifact_id
                for check in source_review.review.checks
                for artifact_id in check.evidence_artifact_ids
            )
        )
        checks = [
            QcCheck(
                code="SOURCE_HEAD_MOTION_QC_APPROVED",
                severity=QcSeverity.BLOCKING,
                passed=True,
                evidence_artifact_ids=source_evidence_ids,
                message=(
                    "Metric-only run reused an approved head-motion QC review from the "
                    "source preprocessing run."
                ),
            ),
            QcCheck(
                code="METRIC_ARTIFACTS_AVAILABLE",
                severity=QcSeverity.BLOCKING,
                passed=True,
                evidence_artifact_ids=tuple(row.artifact_id for row in metric_rows),
                message="Metric artifacts are available for pre-statistics QC.",
            ),
        ]
        return {
            "review_scope": QcReviewScope.PRE_STATISTICS.value,
            "input_manifest_hash": plan.manifest_hash,
            "metric_artifact_ids": [row.artifact_id for row in metric_rows],
            "checks": [check.model_dump(mode="json") for check in checks],
            "included_subject_ids": list(source_review.review.included_subject_ids),
            "excluded_subject_ids": list(source_review.review.excluded_subject_ids),
            "exclusion_reasons": [list(item) for item in source_review.review.exclusion_reasons],
        }

    def _head_motion_qc_content(
        self,
        session: Session,
        *,
        run: WorkflowRunRow,
        plan: PlanRevisionRow,
        report_row: ArtifactRow,
        criteria: dict[str, object] | None,
    ) -> dict[str, object]:
        manifest_subject_ids = self._manifest_subject_ids_for_plan(session, plan)
        provenance = _load(report_row.provenance_json, {})
        report_data = provenance.get("head_motion_qc") if isinstance(provenance, dict) else None
        if not isinstance(report_data, dict):
            raise InputValidationError(
                "head_motion_qc_report_invalid",
                "头动 QC Artifact 缺少结构化报告证据。",
                artifact_id=report_row.artifact_id,
            )
        try:
            report = HeadMotionQcReport.model_validate(report_data)
        except ValidationError as exc:
            raise InputValidationError(
                "head_motion_qc_report_invalid",
                "头动 QC Artifact 的结构化报告无效。",
                artifact_id=report_row.artifact_id,
            ) from exc
        if criteria is not None and report.criteria.model_dump(mode="json") != criteria:
            raise ConflictError(
                "head_motion_qc_criteria_mismatch",
                "已登记头动 QC 报告的阈值与本次请求不一致。",
            )
        if report.input_manifest_hash != plan.manifest_hash:
            raise ConflictError(
                "head_motion_qc_manifest_mismatch",
                "头动 QC 报告与冻结 manifest 不一致。",
            )
        if set(report.included_subject_ids) | set(report.excluded_subject_ids) != set(
            manifest_subject_ids
        ):
            raise InputValidationError(
                "head_motion_qc_subject_manifest_mismatch",
                "头动 QC 纳入与排除受试者必须完整覆盖冻结 manifest。",
            )
        metric_rows = list(
            session.scalars(
                select(ArtifactRow)
                .where(ArtifactRow.run_id == run.run_id, ArtifactRow.artifact_type.like("metric.%"))
                .order_by(ArtifactRow.created_at, ArtifactRow.artifact_id)
            ).all()
        )
        review_scope = QcReviewScope.PRE_STATISTICS if metric_rows else QcReviewScope.HEAD_MOTION
        included_pass = all(
            item.passed
            for item in report.subjects
            if item.subject_id in report.included_subject_ids
        )
        checks = [
            QcCheck(
                code="HEAD_MOTION_EVIDENCE_COMPLETE",
                severity=QcSeverity.BLOCKING,
                passed=True,
                evidence_artifact_ids=(report_row.artifact_id,),
                message="DPABI RealignParameter evidence was parsed and matched to the manifest.",
            ),
            QcCheck(
                code="HEAD_MOTION_INCLUDED_SUBJECTS_PASS",
                severity=QcSeverity.BLOCKING,
                passed=included_pass,
                evidence_artifact_ids=(report_row.artifact_id,),
                message=(
                    f"{len(report.included_subject_ids)} included subjects satisfy the frozen "
                    "head-motion thresholds."
                ),
            ),
            QcCheck(
                code="HEAD_MOTION_EXCLUSIONS_RECORDED",
                severity=QcSeverity.WARNING,
                passed=not report.excluded_subject_ids,
                evidence_artifact_ids=(report_row.artifact_id,),
                message=(
                    f"{len(report.excluded_subject_ids)} subjects were excluded by head-motion QC."
                ),
            ),
            QcCheck(
                code="HEAD_MOTION_INCLUDED_SUBJECTS_NONEMPTY",
                severity=QcSeverity.BLOCKING,
                passed=bool(report.included_subject_ids),
                evidence_artifact_ids=(report_row.artifact_id,),
                message="At least one subject must remain after head-motion QC.",
            ),
        ]
        if review_scope is QcReviewScope.PRE_STATISTICS:
            checks.append(
                QcCheck(
                    code="METRIC_ARTIFACTS_AVAILABLE",
                    severity=QcSeverity.BLOCKING,
                    passed=True,
                    evidence_artifact_ids=tuple(row.artifact_id for row in metric_rows),
                    message="Metric artifacts are available for pre-statistics QC.",
                )
            )
        return {
            "review_scope": review_scope.value,
            "input_manifest_hash": plan.manifest_hash,
            "metric_artifact_ids": [row.artifact_id for row in metric_rows],
            "checks": [check.model_dump(mode="json") for check in checks],
            "included_subject_ids": list(report.included_subject_ids),
            "excluded_subject_ids": list(report.excluded_subject_ids),
            "exclusion_reasons": [list(item) for item in report.exclusion_reasons],
        }

    def _insert_or_get_qc_review(
        self,
        session: Session,
        *,
        run: WorkflowRunRow,
        content: dict[str, object],
    ) -> QcReviewView:
        review_hash = content_hash(content)
        existing = session.scalar(
            select(QcReviewRow)
            .where(QcReviewRow.run_id == run.run_id, QcReviewRow.content_hash == review_hash)
            .order_by(QcReviewRow.revision.desc())
            .limit(1)
        )
        if existing is not None:
            return self._qc_review(existing)
        revision = (
            session.scalar(
                select(func.max(QcReviewRow.revision)).where(QcReviewRow.run_id == run.run_id)
            )
            or 0
        ) + 1
        review_revision_id = _id()
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
            run_id=run.run_id,
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
        with self._read_session() as session:
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

    def get_latest_qc_review_for_run(self, run_id: str) -> QcReviewView | None:
        with self._read_session() as session:
            row = session.scalar(
                select(QcReviewRow)
                .where(QcReviewRow.run_id == run_id)
                .order_by(QcReviewRow.revision.desc())
                .limit(1)
            )
            if row is None:
                return None
            approval = session.scalar(
                select(QcApprovalRow)
                .where(QcApprovalRow.review_revision_id == row.review_revision_id)
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
            run.qc_status = "approved" if request.approved else "rejected"
            run.version += 1
            run.updated_at = datetime.now(UTC)
            session.flush()
            return self._qc_review(row, approval if request.approved else None), self._run(run)

    @staticmethod
    def _qc_skip(row: QcSkipRow) -> QcSkipView:
        return QcSkipView(
            qc_skip_id=row.qc_skip_id,
            run_id=row.run_id,
            project_id=row.project_id,
            run_version=row.run_version,
            decision_hash=row.decision_hash,
            actor=row.actor,
            reason=row.reason,
            qc_review_revision_id=row.qc_review_revision_id,
            created_at=row.created_at,
        )

    def get_qc_skip_for_run(self, run_id: str) -> QcSkipView | None:
        with self._read_session() as session:
            row = session.scalar(select(QcSkipRow).where(QcSkipRow.run_id == run_id))
            return None if row is None else self._qc_skip(row)

    def skip_qc(self, run_id: str, request: QcSkipCreate) -> tuple[QcSkipView, RunView]:
        with self._write_session() as session:
            run = session.get(WorkflowRunRow, run_id)
            if run is None:
                raise NotFoundError("run", run_id)
            existing = session.scalar(select(QcSkipRow).where(QcSkipRow.run_id == run_id))
            if existing is not None:
                if existing.actor == request.actor and existing.reason == request.reason:
                    return self._qc_skip(existing), self._run(run)
                raise ConflictError("qc_skip_already_recorded", "该运行已记录 QC 跳过决定。")
            self._check_version("run", run.version, request.expected_run_version)
            state = WorkflowState(run.state)
            if state is not WorkflowState.QC_REVIEW:
                raise ConflictError(
                    "qc_skip_not_available",
                    "只有完成计算后等待 QC 的运行可以跳过 QC。",
                    state=state.value,
                )
            review = session.scalars(
                select(QcReviewRow)
                .where(QcReviewRow.run_id == run_id)
                .order_by(QcReviewRow.revision.desc())
                .limit(1)
            ).first()
            decision_payload = {
                "run_id": run_id,
                "project_id": run.project_id,
                "run_version": run.version,
                "actor": request.actor,
                "reason": request.reason,
                "qc_review_revision_id": review.review_revision_id if review else None,
            }
            record = QcSkipRow(
                qc_skip_id=_id(),
                run_id=run_id,
                project_id=run.project_id,
                run_version=run.version,
                decision_hash=content_hash(decision_payload),
                actor=request.actor,
                reason=request.reason,
                qc_review_revision_id=review.review_revision_id if review else None,
            )
            session.add(record)
            validate_workflow_transition(state, WorkflowState.SUCCEEDED)
            run.state = WorkflowState.SUCCEEDED.value
            run.qc_mode = QcExecutionMode.SKIP.value
            run.qc_status = "skipped"
            run.version += 1
            run.updated_at = datetime.now(UTC)
            session.flush()
            return self._qc_skip(record), self._run(run)

    def get_approved_qc_review(self, review_revision_id: str) -> QcReviewView:
        view = self.get_qc_review(review_revision_id)
        if view.state is not QcReviewState.APPROVED:
            raise ConflictError(
                "qc_review_not_approved",
                "统计设计只能引用已批准的 QC review revision。",
                state=view.state.value,
            )
        return view
