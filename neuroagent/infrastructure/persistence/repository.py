"""SQLAlchemy repository implementing immutable revisions and atomic job claims."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

from sqlalchemy import text
from sqlalchemy.orm import Session

from neuroagent.application.contracts import (
    ApprovalDecision,
    ApprovalView,
    ArtifactView,
    DatasetProfile,
    DatasetSplitView,
    DatasetView,
    DemographicsRevisionView,
    ManifestRevisionView,
    PlanRevisionView,
    PlanState,
    ProjectView,
    QcReviewState,
    QcReviewView,
    RunView,
    StatisticalResultDetailView,
    StatisticalResultView,
    SubjectManifestEntry,
    ValidationIssue,
    WorkflowState,
)
from neuroagent.application.errors import ConflictError
from neuroagent.domain.fmri.qc import QcCheck, QcReviewRevision
from neuroagent.infrastructure.persistence.database import Database
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
from neuroagent.infrastructure.persistence.repository_mixins import (
    ArtifactEventMixin,
    IdempotencyMixin,
    JobExecutionMixin,
    ModelAgentMixin,
    PlanApprovalMixin,
    ProjectDatasetMixin,
    QcReviewMixin,
    RunMixin,
)
from neuroagent.infrastructure.persistence.repository_mixins._base import (
    _as_utc,
    _load,
)
from neuroagent.infrastructure.persistence.repository_mixins._base import (
    _validate_artifact_relative_path as _validate_artifact_relative_path,
)


class SqliteRepository(
    IdempotencyMixin,
    ProjectDatasetMixin,
    PlanApprovalMixin,
    RunMixin,
    QcReviewMixin,
    JobExecutionMixin,
    ArtifactEventMixin,
    ModelAgentMixin,
):
    def __init__(self, database: Database) -> None:
        self.database = database
        self._transaction_session: ContextVar[Session | None] = ContextVar(
            f"repository_transaction_{id(self)}",
            default=None,
        )

    @contextmanager
    def _immediate_write_transaction(self) -> Iterator[Session]:
        """Open a SQLite transaction that owns the writer lock before any read-modify-write."""

        with self.database.session_factory() as session:
            session.execute(text("BEGIN IMMEDIATE"))
            try:
                yield session
            except BaseException:
                session.rollback()
                raise
            else:
                session.commit()

    @contextmanager
    def atomic(self) -> Iterator[None]:
        """Join all repository writes in this context to one database transaction."""

        active = self._transaction_session.get()
        if active is not None:
            yield
            return
        with self._immediate_write_transaction() as session:
            token = self._transaction_session.set(session)
            try:
                yield
            finally:
                self._transaction_session.reset(token)

    @contextmanager
    def _write_session(self) -> Iterator[Session]:
        active = self._transaction_session.get()
        if active is not None:
            yield active
            return
        with self._immediate_write_transaction() as session:
            yield session

    # -- response conversion -------------------------------------------------

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
        return RunView(
            run_id=row.run_id,
            project_id=row.project_id,
            plan_revision_id=row.plan_revision_id,
            state=WorkflowState(row.state),
            version=row.version,
            attempt=row.attempt,
            cancel_requested=row.cancel_requested,
            error=row.error,
            created_at=_as_utc(row.created_at),
            updated_at=_as_utc(row.updated_at),
        )

    @staticmethod
    def _qc_review(row: QcReviewRow, approval: QcApprovalRow | None = None) -> QcReviewView:
        content = _load(row.content_json, {})
        approved = QcReviewState(row.state) is QcReviewState.APPROVED
        review = QcReviewRevision(
            review_revision_id=row.review_revision_id,
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
        summary = SqliteRepository._statistical_result_view(row)
        return StatisticalResultDetailView(
            **summary.model_dump(),
            manifest=_load(row.manifest_json, {}),
            report_markdown=row.report_markdown,
            report_json=row.report_json,
        )

    @staticmethod
    def _check_version(resource: str, actual: int, expected: int) -> None:
        if actual != expected:
            raise ConflictError(
                "revision_conflict",
                f"{resource} 版本已变化, 请刷新后重试。",
                expected=expected,
                actual=actual,
            )
