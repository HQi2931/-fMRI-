"""SQLAlchemy row-to-contract mapping used by the SQLite persistence adapter."""

from __future__ import annotations

from neuroagent.application.contracts.plans import (
    ApprovalDecision,
    ApprovalView,
    PlanRevisionView,
    ValidationIssue,
)
from neuroagent.application.contracts.projects import (
    DatasetProfile,
    DatasetSplitView,
    DatasetView,
    DemographicsRevisionView,
    ManifestRevisionView,
    ProjectView,
    SubjectManifestEntry,
    WorkspaceStageCandidate,
)
from neuroagent.application.contracts.runs import (
    ArtifactView,
    QcExecutionMode,
    QcReviewState,
    QcReviewView,
    RunView,
)
from neuroagent.application.contracts.statistics import (
    StatisticalResultDetailView,
    StatisticalResultView,
)
from neuroagent.domain.fmri.qc import QcCheck, QcReviewRevision
from neuroagent.domain.lifecycle import PlanState, WorkflowState
from neuroagent.infrastructure.persistence.models import (
    ApprovalRow,
    ArtifactRow,
    DatasetRow,
    DatasetSplitRevisionRow,
    DemographicsRevisionRow,
    ManifestRevisionRow,
    PlanRevisionRow,
    ProjectRow,
    QcApprovalRow,
    QcReviewRow,
    StatisticalResultRow,
    WorkflowRunRow,
)
from neuroagent.infrastructure.persistence.repository_mixins._base import _as_utc, _load


class RepositoryMapperMixin:
    """Translate persistence rows into immutable application read models."""

    @staticmethod
    def _project(row: ProjectRow) -> ProjectView:
        return ProjectView(
            project_id=row.project_id,
            name=row.name,
            source_roots=_load(row.source_roots_json, []),
            work_root=row.work_root,
            version=row.version,
            created_at=_as_utc(row.created_at),
        )

    @staticmethod
    def _dataset(row: DatasetRow) -> DatasetView:
        return DatasetView(
            dataset_id=row.dataset_id,
            project_id=row.project_id,
            name=row.name,
            source_path=row.source_path,
            version=row.version,
            current_manifest_id=row.current_manifest_id,
            created_at=_as_utc(row.created_at),
        )

    @staticmethod
    def _manifest(row: ManifestRevisionRow) -> ManifestRevisionView:
        value = _load(row.content_json, {})
        return ManifestRevisionView(
            manifest_id=row.manifest_id,
            dataset_id=row.dataset_id,
            revision=row.revision,
            content_hash=row.content_hash,
            profile=DatasetProfile.model_validate(value["profile"]),
            subjects=[SubjectManifestEntry.model_validate(item) for item in value["subjects"]],
            selected_input_stage=value.get("selected_input_stage"),
            stage_candidates=[
                WorkspaceStageCandidate.model_validate(item)
                for item in value.get("stage_candidates", [])
            ],
            created_at=_as_utc(row.created_at),
        )

    @staticmethod
    def _demographics(row: DemographicsRevisionRow) -> DemographicsRevisionView:
        value = _load(row.content_json, {})
        return DemographicsRevisionView(
            demographics_id=row.demographics_id,
            dataset_id=row.dataset_id,
            revision=row.revision,
            content_hash=row.content_hash,
            row_count=len(value["rows"]),
            columns=value["columns"],
            missing_subject_ids=value["missing_subject_ids"],
            extra_subject_ids=value["extra_subject_ids"],
            created_at=_as_utc(row.created_at),
        )

    @staticmethod
    def _split(row: DatasetSplitRevisionRow) -> DatasetSplitView:
        value = _load(row.content_json, {})
        return DatasetSplitView(
            split_id=row.split_id,
            dataset_id=row.dataset_id,
            revision=row.revision,
            content_hash=row.content_hash,
            seed=value["seed"],
            stratify_by=value.get("stratify_by"),
            train_subject_ids=value["train_subject_ids"],
            validation_subject_ids=value["validation_subject_ids"],
            test_subject_ids=value["test_subject_ids"],
            created_at=_as_utc(row.created_at),
        )

    @staticmethod
    def _plan(row: PlanRevisionRow) -> PlanRevisionView:
        return PlanRevisionView(
            plan_revision_id=row.plan_revision_id,
            project_id=row.project_id,
            revision=row.revision,
            version=row.version,
            plan_hash=row.plan_hash,
            manifest_hash=row.manifest_hash,
            environment_hash=row.environment_hash,
            state=PlanState(row.state),
            plan=_load(row.plan_json, {}),
            validation_issues=[
                ValidationIssue.model_validate(item)
                for item in _load(row.validation_issues_json, [])
            ],
            supersedes_plan_revision_id=row.supersedes_plan_revision_id,
            created_at=_as_utc(row.created_at),
            updated_at=_as_utc(row.updated_at),
        )

    @staticmethod
    def _approval(row: ApprovalRow) -> ApprovalView:
        return ApprovalView(
            approval_id=row.approval_id,
            plan_revision_id=row.plan_revision_id,
            plan_hash=row.plan_hash,
            actor=row.actor,
            decision=ApprovalDecision(row.decision),
            reason=row.reason,
            created_at=_as_utc(row.created_at),
        )

    @staticmethod
    def _run(row: WorkflowRunRow) -> RunView:
        qc_mode = QcExecutionMode(row.qc_mode or "require")
        qc_status = row.qc_status or (
            "skipped"
            if qc_mode is QcExecutionMode.SKIP
            else (
                "awaiting_review"
                if WorkflowState(row.state) is WorkflowState.QC_REVIEW
                else "pending"
            )
        )
        return RunView(
            run_id=row.run_id,
            project_id=row.project_id,
            plan_revision_id=row.plan_revision_id,
            state=WorkflowState(row.state),
            version=row.version,
            attempt=row.attempt,
            cancel_requested=row.cancel_requested,
            error=row.error,
            qc_mode=qc_mode,
            qc_status=qc_status,
            created_at=_as_utc(row.created_at),
            updated_at=_as_utc(row.updated_at),
        )

    @staticmethod
    def _qc_review(row: QcReviewRow, approval: QcApprovalRow | None = None) -> QcReviewView:
        content = _load(row.content_json, {})
        approved = QcReviewState(row.state) is QcReviewState.APPROVED
        review = QcReviewRevision(
            review_revision_id=row.review_revision_id,
            review_scope=content.get("review_scope", "pre_statistics"),
            input_manifest_hash=content["input_manifest_hash"],
            metric_artifact_ids=tuple(content["metric_artifact_ids"]),
            checks=tuple(QcCheck.model_validate(item) for item in content["checks"]),
            included_subject_ids=tuple(content["included_subject_ids"]),
            excluded_subject_ids=tuple(content["excluded_subject_ids"]),
            exclusion_reasons=tuple(tuple(item) for item in content["exclusion_reasons"]),
            approved=approved,
            approved_by=approval.actor if approved and approval is not None else None,
            approval_reason=approval.reason if approved and approval is not None else None,
            content_hash=row.content_hash,
        )
        return QcReviewView(
            review=review,
            run_id=row.run_id,
            project_id=row.project_id,
            revision=row.revision,
            version=row.version,
            state=QcReviewState(row.state),
            created_at=_as_utc(row.created_at),
            updated_at=_as_utc(row.updated_at),
        )

    @staticmethod
    def _artifact(row: ArtifactRow) -> ArtifactView:
        return ArtifactView(
            artifact_id=row.artifact_id,
            project_id=row.project_id,
            run_id=row.run_id,
            artifact_type=row.artifact_type,
            relative_path=row.relative_path,
            checksum=row.checksum,
            size_bytes=row.size_bytes,
            provenance=_load(row.provenance_json, {}),
            created_at=_as_utc(row.created_at),
        )

    @staticmethod
    def _statistical_result_view(row: StatisticalResultRow) -> StatisticalResultView:
        manifest = _load(row.manifest_json, {})
        artifacts = manifest.get("artifacts", [])
        clusters = manifest.get("clusters", [])
        return StatisticalResultView(
            result_id=row.result_id,
            project_id=row.project_id,
            run_id=row.run_id,
            design_revision_id=row.design_revision_id,
            mode=row.mode,
            non_scientific=row.non_scientific,
            non_scientific_reason=row.non_scientific_reason,
            bundle_hash=row.bundle_hash,
            artifact_count=len(artifacts),
            cluster_count=len(clusters),
            version=row.version,
            created_at=_as_utc(row.created_at),
        )

    @staticmethod
    def _statistical_result_detail(row: StatisticalResultRow) -> StatisticalResultDetailView:
        summary = RepositoryMapperMixin._statistical_result_view(row)
        return StatisticalResultDetailView(
            **summary.model_dump(),
            manifest=_load(row.manifest_json, {}),
            report_markdown=row.report_markdown,
            report_json=row.report_json,
        )
