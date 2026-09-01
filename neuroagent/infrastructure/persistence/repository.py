"""SQLAlchemy repository implementing immutable revisions and atomic job claims."""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime, timedelta
from pathlib import PurePosixPath, PureWindowsPath
from typing import Any, cast
from uuid import uuid4

from pydantic import BaseModel, ValidationError
from sqlalchemy import CursorResult, delete, func, or_, select, text, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from neuroagent.agent.models import GatewayResult
from neuroagent.application.contracts import (
    AgentTaskView,
    ApprovalCreate,
    ApprovalDecision,
    ApprovalView,
    ArtifactView,
    DatasetProfile,
    DatasetSplitView,
    DatasetView,
    DemographicsRevisionView,
    ManifestRevisionView,
    ModelProfileInput,
    ModelProfileView,
    PlanRevisionView,
    PlanState,
    ProjectView,
    QcReviewApprove,
    QcReviewCreate,
    QcReviewState,
    QcReviewView,
    RuntimeEventView,
    RunView,
    StatisticalResultDetailView,
    StatisticalResultView,
    SubjectManifestEntry,
    ValidationIssue,
    WorkflowState,
)
from neuroagent.application.errors import ConflictError, InputValidationError, NotFoundError
from neuroagent.application.hashing import canonical_json, content_hash
from neuroagent.application.ports import ClaimedJob
from neuroagent.domain.fmri.artifacts import ArtifactKind, ArtifactLineage
from neuroagent.domain.fmri.qc import QcCheck, QcReviewRevision
from neuroagent.infrastructure.persistence.database import Database
from neuroagent.infrastructure.persistence.models import (
    AgentTaskRow,
    ApprovalRow,
    ArtifactRow,
    DatasetRow,
    DatasetSplitRevisionRow,
    DemographicsRevisionRow,
    IdempotencyRow,
    JobRow,
    ManifestRevisionRow,
    ModelProfileRow,
    PlanRevisionRow,
    ProjectRow,
    QcApprovalRow,
    QcReviewRow,
    RuntimeEventRow,
    StatisticalResultRow,
    WorkflowRunRow,
)
from neuroagent.observability.events import redact_event_payload
from neuroagent.observability.tracing import current_trace_id
from neuroagent.workflow.state import validate_plan_transition, validate_workflow_transition


def _id() -> str:
    return str(uuid4())


def _load(value: str | None, default: Any = None) -> Any:
    if value is None:
        return default
    return json.loads(value)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _validate_artifact_relative_path(value: object) -> str:
    """Return a canonical POSIX artifact path or fail closed.

    Artifact paths are persisted independently of the host platform, so Windows
    path syntax must be rejected explicitly even when this code runs on Windows.
    """

    if not isinstance(value, str):
        raise InputValidationError(
            "artifact_path_invalid",
            "产物路径必须是运行目录内的规范相对路径。",
        )
    relative_path = value
    parsed = PurePosixPath(relative_path)
    windows_path = PureWindowsPath(relative_path)
    if (
        not relative_path
        or relative_path == "."
        or "\\" in relative_path
        or parsed.is_absolute()
        or bool(windows_path.drive)
        or ".." in parsed.parts
        or parsed.as_posix() != relative_path
    ):
        raise InputValidationError(
            "artifact_path_invalid",
            "产物路径必须是运行目录内的规范相对路径。",
            relative_path=relative_path,
        )
    return relative_path


class SqliteRepository:
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

    # -- idempotency ---------------------------------------------------------
    def begin_idempotent_request(
        self,
        scope: str,
        key: str,
        request_hash: str,
        owner_token: str,
        lease_seconds: int,
    ) -> dict[str, Any] | None:
        now = datetime.now(UTC)
        lease_expires_at = now + timedelta(seconds=lease_seconds)
        with self._write_session() as session:
            record_id = _id()
            inserted = cast(
                CursorResult[Any],
                session.execute(
                    sqlite_insert(IdempotencyRow)
                    .values(
                        record_id=record_id,
                        scope=scope,
                        idempotency_key=key,
                        request_hash=request_hash,
                        status="pending",
                        response_json=None,
                        owner_token=owner_token,
                        lease_expires_at=lease_expires_at,
                        updated_at=now,
                    )
                    .on_conflict_do_nothing(
                        index_elements=[
                            IdempotencyRow.scope,
                            IdempotencyRow.idempotency_key,
                        ]
                    )
                ),
            )
            if inserted.rowcount == 1:
                return None
            row = session.scalar(
                select(IdempotencyRow).where(
                    IdempotencyRow.scope == scope,
                    IdempotencyRow.idempotency_key == key,
                )
            )
            if row is None:
                raise ConflictError(
                    "idempotency_race", "幂等请求发生并发冲突, 请重试。", scope=scope
                )
            if row.request_hash != request_hash:
                raise ConflictError(
                    "idempotency_key_reused",
                    "同一 Idempotency-Key 不能用于不同请求。",
                    scope=scope,
                )
            if row.status == "completed" and row.response_json is not None:
                return cast(dict[str, Any], _load(row.response_json, {}))
            if row.status != "pending":
                raise ConflictError(
                    "idempotency_request_in_progress",
                    "相同写请求仍在处理中, 请稍后使用同一 Idempotency-Key 重试。",
                    scope=scope,
                )
            if row.lease_expires_at is not None and _as_utc(row.lease_expires_at) > now:
                raise ConflictError(
                    "idempotency_request_in_progress",
                    "相同写请求仍在处理中, 请稍后使用同一 Idempotency-Key 重试。",
                    scope=scope,
                )
            row.owner_token = owner_token
            row.lease_expires_at = lease_expires_at
            row.updated_at = now
            row.response_json = None
            session.flush()
            return None

    def complete_idempotent_request(
        self,
        scope: str,
        key: str,
        request_hash: str,
        owner_token: str,
        response: BaseModel,
    ) -> None:
        with self._write_session() as session:
            result = cast(
                CursorResult[Any],
                session.execute(
                    update(IdempotencyRow)
                    .where(
                        IdempotencyRow.scope == scope,
                        IdempotencyRow.idempotency_key == key,
                        IdempotencyRow.request_hash == request_hash,
                        IdempotencyRow.status == "pending",
                        IdempotencyRow.owner_token == owner_token,
                    )
                    .values(
                        status="completed",
                        response_json=canonical_json(response.model_dump(mode="json")),
                        lease_expires_at=None,
                        updated_at=datetime.now(UTC),
                    )
                ),
            )
            if result.rowcount != 1:
                raise ConflictError(
                    "idempotency_completion_conflict",
                    "无法完成幂等请求记录, 写操作结果需要人工核对。",
                    scope=scope,
                )

    def renew_idempotent_request(
        self,
        scope: str,
        key: str,
        request_hash: str,
        owner_token: str,
        lease_seconds: int,
    ) -> bool:
        now = datetime.now(UTC)
        with self._write_session() as session:
            result = cast(
                CursorResult[Any],
                session.execute(
                    update(IdempotencyRow)
                    .where(
                        IdempotencyRow.scope == scope,
                        IdempotencyRow.idempotency_key == key,
                        IdempotencyRow.request_hash == request_hash,
                        IdempotencyRow.status == "pending",
                        IdempotencyRow.owner_token == owner_token,
                    )
                    .values(
                        lease_expires_at=now + timedelta(seconds=lease_seconds),
                        updated_at=now,
                    )
                ),
            )
            return result.rowcount == 1

    def release_idempotent_request(
        self, scope: str, key: str, request_hash: str, owner_token: str
    ) -> None:
        with self._write_session() as session:
            session.execute(
                delete(IdempotencyRow).where(
                    IdempotencyRow.scope == scope,
                    IdempotencyRow.idempotency_key == key,
                    IdempotencyRow.request_hash == request_hash,
                    IdempotencyRow.status == "pending",
                    IdempotencyRow.owner_token == owner_token,
                )
            )

    def get_idempotent_response(
        self, scope: str, key: str, request_hash: str
    ) -> dict[str, Any] | None:
        """Compatibility read used by diagnostics; it never reserves a key."""
        with self.database.session_factory() as session:
            row = session.scalar(
                select(IdempotencyRow).where(
                    IdempotencyRow.scope == scope,
                    IdempotencyRow.idempotency_key == key,
                    IdempotencyRow.request_hash == request_hash,
                    IdempotencyRow.status == "completed",
                )
            )
            if row is None or row.response_json is None:
                return None
            return cast(dict[str, Any], _load(row.response_json, {}))

    # -- projects and datasets ----------------------------------------------
    def create_project(self, name: str, source_roots: list[str], work_root: str) -> ProjectView:
        with self._write_session() as session:
            row = ProjectRow(
                project_id=_id(),
                name=name,
                source_roots_json=canonical_json(source_roots),
                work_root=work_root,
            )
            session.add(row)
            session.flush()
            return self._project(row)

    def get_project(self, project_id: str) -> ProjectView:
        with self.database.session_factory() as session:
            row = session.get(ProjectRow, project_id)
            if row is None:
                raise NotFoundError("project", project_id)
            return self._project(row)

    def list_projects(self) -> list[ProjectView]:
        with self.database.session_factory() as session:
            rows = session.scalars(select(ProjectRow).order_by(ProjectRow.created_at)).all()
            return [self._project(row) for row in rows]

    def create_dataset(
        self,
        project_id: str,
        *,
        name: str,
        source_path: str,
        expected_project_version: int,
    ) -> DatasetView:
        with self._write_session() as session:
            project = session.get(ProjectRow, project_id)
            if project is None:
                raise NotFoundError("project", project_id)
            if project.version != expected_project_version:
                raise ConflictError(
                    "revision_conflict",
                    "项目版本已变化, 请刷新后重试。",
                    expected=expected_project_version,
                    actual=project.version,
                )
            project.version += 1
            row = DatasetRow(
                dataset_id=_id(),
                project_id=project_id,
                name=name,
                source_path=source_path,
            )
            session.add(row)
            session.flush()
            return self._dataset(row)

    def get_dataset(self, dataset_id: str) -> DatasetView:
        with self.database.session_factory() as session:
            row = session.get(DatasetRow, dataset_id)
            if row is None:
                raise NotFoundError("dataset", dataset_id)
            return self._dataset(row)

    def create_manifest(
        self, dataset_id: str, *, expected_version: int, content: dict[str, Any]
    ) -> ManifestRevisionView:
        with self._write_session() as session:
            dataset = session.get(DatasetRow, dataset_id)
            if dataset is None:
                raise NotFoundError("dataset", dataset_id)
            self._check_version("dataset", dataset.version, expected_version)
            revision = (
                session.scalar(
                    select(func.max(ManifestRevisionRow.revision)).where(
                        ManifestRevisionRow.dataset_id == dataset_id
                    )
                )
                or 0
            ) + 1
            row = ManifestRevisionRow(
                manifest_id=_id(),
                dataset_id=dataset_id,
                revision=revision,
                content_hash=content_hash(content),
                content_json=canonical_json(content),
            )
            session.add(row)
            session.flush()
            dataset.current_manifest_id = row.manifest_id
            dataset.version += 1
            return self._manifest(row)

    def get_manifest(self, manifest_id: str) -> ManifestRevisionView:
        with self.database.session_factory() as session:
            row = session.get(ManifestRevisionRow, manifest_id)
            if row is None:
                raise NotFoundError("manifest", manifest_id)
            return self._manifest(row)

    def get_manifest_content(self, manifest_id: str) -> dict[str, Any]:
        with self.database.session_factory() as session:
            row = session.get(ManifestRevisionRow, manifest_id)
            if row is None:
                raise NotFoundError("manifest", manifest_id)
            return cast(dict[str, Any], _load(row.content_json, {}))

    def create_demographics(
        self, dataset_id: str, *, expected_version: int, content: dict[str, Any]
    ) -> DemographicsRevisionView:
        with self._write_session() as session:
            dataset = session.get(DatasetRow, dataset_id)
            if dataset is None:
                raise NotFoundError("dataset", dataset_id)
            self._check_version("dataset", dataset.version, expected_version)
            revision = (
                session.scalar(
                    select(func.max(DemographicsRevisionRow.revision)).where(
                        DemographicsRevisionRow.dataset_id == dataset_id
                    )
                )
                or 0
            ) + 1
            row = DemographicsRevisionRow(
                demographics_id=_id(),
                dataset_id=dataset_id,
                revision=revision,
                content_hash=content_hash(content),
                content_json=canonical_json(content),
            )
            session.add(row)
            dataset.version += 1
            session.flush()
            return self._demographics(row)

    def get_demographics_content(self, demographics_id: str) -> tuple[str, dict[str, Any]]:
        with self.database.session_factory() as session:
            row = session.get(DemographicsRevisionRow, demographics_id)
            if row is None:
                raise NotFoundError("demographics_revision", demographics_id)
            return row.dataset_id, _load(row.content_json, {})

    def get_demographics(self, demographics_id: str) -> DemographicsRevisionView:
        with self.database.session_factory() as session:
            row = session.get(DemographicsRevisionRow, demographics_id)
            if row is None:
                raise NotFoundError("demographics_revision", demographics_id)
            return self._demographics(row)

    def create_split(
        self, dataset_id: str, *, expected_version: int, content: dict[str, Any]
    ) -> DatasetSplitView:
        with self._write_session() as session:
            dataset = session.get(DatasetRow, dataset_id)
            if dataset is None:
                raise NotFoundError("dataset", dataset_id)
            self._check_version("dataset", dataset.version, expected_version)
            revision = (
                session.scalar(
                    select(func.max(DatasetSplitRevisionRow.revision)).where(
                        DatasetSplitRevisionRow.dataset_id == dataset_id
                    )
                )
                or 0
            ) + 1
            row = DatasetSplitRevisionRow(
                split_id=_id(),
                dataset_id=dataset_id,
                revision=revision,
                content_hash=content_hash(content),
                content_json=canonical_json(content),
            )
            session.add(row)
            dataset.version += 1
            session.flush()
            return self._split(row)

    def get_split(self, split_id: str) -> DatasetSplitView:
        with self.database.session_factory() as session:
            row = session.get(DatasetSplitRevisionRow, split_id)
            if row is None:
                raise NotFoundError("dataset_split_revision", split_id)
            return self._split(row)

    # -- immutable plans and approvals --------------------------------------
    def create_plan(
        self,
        *,
        project_id: str,
        expected_project_version: int,
        plan: dict[str, Any],
        plan_hash: str,
        manifest_hash: str,
        environment_hash: str,
        supersedes_id: str | None,
    ) -> PlanRevisionView:
        with self._write_session() as session:
            project = session.get(ProjectRow, project_id)
            if project is None:
                raise NotFoundError("project", project_id)
            self._check_version("project", project.version, expected_project_version)
            revision = (
                session.scalar(
                    select(func.max(PlanRevisionRow.revision)).where(
                        PlanRevisionRow.project_id == project_id
                    )
                )
                or 0
            ) + 1
            if supersedes_id:
                old = session.get(PlanRevisionRow, supersedes_id)
                if old is None:
                    raise NotFoundError("plan_revision", supersedes_id)
                if old.project_id != project_id:
                    raise ConflictError(
                        "cross_project_supersede",
                        "不能替代其他项目的计划。",
                    )
                old_state = PlanState(old.state)
                validate_plan_transition(old_state, PlanState.SUPERSEDED)
                old.state = PlanState.SUPERSEDED.value
                old.version += 1
                old.updated_at = datetime.now(UTC)
            row = PlanRevisionRow(
                plan_revision_id=_id(),
                project_id=project_id,
                revision=revision,
                state=PlanState.DRAFT.value,
                plan_hash=plan_hash,
                manifest_hash=manifest_hash,
                environment_hash=environment_hash,
                plan_json=canonical_json(plan),
                validation_issues_json="[]",
                supersedes_plan_revision_id=supersedes_id,
            )
            session.add(row)
            session.flush()
            return self._plan(row)

    def get_plan(self, plan_revision_id: str) -> PlanRevisionView:
        with self.database.session_factory() as session:
            row = session.get(PlanRevisionRow, plan_revision_id)
            if row is None:
                raise NotFoundError("plan_revision", plan_revision_id)
            return self._plan(row)

    def validate_plan(
        self,
        plan_revision_id: str,
        *,
        expected_version: int,
        issues: list[ValidationIssue],
    ) -> PlanRevisionView:
        with self._write_session() as session:
            row = session.get(PlanRevisionRow, plan_revision_id)
            if row is None:
                raise NotFoundError("plan_revision", plan_revision_id)
            self._check_version("plan_revision", row.version, expected_version)
            current = PlanState(row.state)
            validate_plan_transition(current, PlanState.VALIDATING)
            target = (
                PlanState.DRAFT
                if any(issue.severity == "blocking" for issue in issues)
                else PlanState.AWAITING_APPROVAL
            )
            validate_plan_transition(PlanState.VALIDATING, target)
            row.state = target.value
            row.validation_issues_json = canonical_json(
                [issue.model_dump(mode="json") for issue in issues]
            )
            row.version += 1
            row.updated_at = datetime.now(UTC)
            session.flush()
            return self._plan(row)

    def record_approval(
        self, plan_revision_id: str, request: ApprovalCreate
    ) -> tuple[ApprovalView, PlanRevisionView]:
        with self._write_session() as session:
            plan = session.get(PlanRevisionRow, plan_revision_id)
            if plan is None:
                raise NotFoundError("plan_revision", plan_revision_id)
            self._check_version("plan_revision", plan.version, request.expected_version)
            if PlanState(plan.state) is not PlanState.AWAITING_APPROVAL:
                raise ConflictError(
                    "plan_not_awaiting_approval",
                    "计划当前不处于待审批状态。",
                    state=plan.state,
                )
            if plan.plan_hash != request.plan_hash:
                raise ConflictError(
                    "approval_hash_mismatch",
                    "审批哈希与当前不可变计划不一致。",
                    expected=plan.plan_hash,
                    received=request.plan_hash,
                )
            target = (
                PlanState.APPROVED
                if request.decision is ApprovalDecision.APPROVED
                else PlanState.DRAFT
            )
            validate_plan_transition(PlanState.AWAITING_APPROVAL, target)
            approval = ApprovalRow(
                approval_id=_id(),
                plan_revision_id=plan_revision_id,
                plan_hash=request.plan_hash,
                actor=request.actor,
                decision=request.decision.value,
                reason=request.reason,
            )
            session.add(approval)
            plan.state = target.value
            plan.version += 1
            plan.updated_at = datetime.now(UTC)
            session.flush()
            return self._approval(approval), self._plan(plan)

    def has_valid_approval(self, plan_revision_id: str, plan_hash: str) -> bool:
        with self.database.session_factory() as session:
            return (
                session.scalar(
                    select(func.count(ApprovalRow.approval_id)).where(
                        ApprovalRow.plan_revision_id == plan_revision_id,
                        ApprovalRow.plan_hash == plan_hash,
                        ApprovalRow.decision == ApprovalDecision.APPROVED.value,
                    )
                )
                or 0
            ) > 0

    # -- workflow/jobs -------------------------------------------------------
    def create_run(
        self,
        *,
        project_id: str,
        plan_revision_id: str,
        expected_plan_hash: str,
        max_attempts: int,
        payload: dict[str, Any],
    ) -> RunView:
        with self._write_session() as session:
            plan = session.get(PlanRevisionRow, plan_revision_id)
            if plan is None:
                raise NotFoundError("plan_revision", plan_revision_id)
            if plan.plan_hash != expected_plan_hash:
                raise ConflictError(
                    "run_plan_hash_mismatch",
                    "运行请求的计划哈希与当前不可变计划不一致。",
                    expected=plan.plan_hash,
                    received=expected_plan_hash,
                )
            if plan.project_id != project_id:
                raise ConflictError("cross_project_plan", "计划不属于指定项目。")
            if PlanState(plan.state) is not PlanState.APPROVED:
                raise ConflictError(
                    "plan_not_approved", "只能从已批准且未失效的计划创建运行。", state=plan.state
                )
            valid = session.scalar(
                select(func.count(ApprovalRow.approval_id)).where(
                    ApprovalRow.plan_revision_id == plan_revision_id,
                    ApprovalRow.plan_hash == plan.plan_hash,
                    ApprovalRow.decision == ApprovalDecision.APPROVED.value,
                )
            )
            if not valid:
                raise ConflictError("approval_missing", "未找到与计划哈希匹配的有效审批记录。")
            run = WorkflowRunRow(
                run_id=_id(),
                project_id=project_id,
                plan_revision_id=plan_revision_id,
                state=WorkflowState.QUEUED.value,
            )
            session.add(run)
            session.flush()
            session.add(
                JobRow(
                    job_id=_id(),
                    run_id=run.run_id,
                    executor_type="mock",
                    state="queued",
                    payload_json=canonical_json(payload),
                    max_attempts=max_attempts,
                )
            )
            session.flush()
            return self._run(run)

    def get_run(self, run_id: str) -> RunView:
        with self.database.session_factory() as session:
            row = session.get(WorkflowRunRow, run_id)
            if row is None:
                raise NotFoundError("run", run_id)
            return self._run(row)

    def list_runs(
        self,
        *,
        project_id: str | None = None,
        state: WorkflowState | None = None,
    ) -> list[RunView]:
        with self.database.session_factory() as session:
            query = select(WorkflowRunRow)
            if project_id is not None:
                if session.get(ProjectRow, project_id) is None:
                    raise NotFoundError("project", project_id)
                query = query.where(WorkflowRunRow.project_id == project_id)
            if state is not None:
                query = query.where(WorkflowRunRow.state == state.value)
            rows = session.scalars(
                query.order_by(WorkflowRunRow.created_at.desc(), WorkflowRunRow.run_id)
            ).all()
            return [self._run(row) for row in rows]

    def transition_run(
        self,
        run_id: str,
        *,
        expected_version: int,
        target: WorkflowState,
        error: str | None = None,
        increment_attempt: bool = False,
    ) -> RunView:
        now = datetime.now(UTC)
        with self._write_session() as session:
            values: dict[str, Any] = {
                "state": target.value,
                "version": expected_version + 1,
                "updated_at": now,
                "error": error,
            }
            if increment_attempt:
                values["attempt"] = WorkflowRunRow.attempt + 1
            result = cast(
                CursorResult[Any],
                session.execute(
                    update(WorkflowRunRow)
                    .where(
                        WorkflowRunRow.run_id == run_id,
                        WorkflowRunRow.version == expected_version,
                    )
                    .values(**values)
                ),
            )
            if result.rowcount != 1:
                exists = session.get(WorkflowRunRow, run_id)
                if exists is None:
                    raise NotFoundError("run", run_id)
                raise ConflictError(
                    "revision_conflict",
                    "运行版本已变化, 请刷新后重试。",
                    expected=expected_version,
                    actual=exists.version,
                )
            row = session.get(WorkflowRunRow, run_id)
            assert row is not None
            return self._run(row)

    def request_cancel(self, run_id: str, expected_version: int) -> RunView:
        with self._write_session() as session:
            run = session.get(WorkflowRunRow, run_id)
            if run is None:
                raise NotFoundError("run", run_id)
            self._check_version("run", run.version, expected_version)
            state = WorkflowState(run.state)
            if state is WorkflowState.QUEUED:
                validate_workflow_transition(state, WorkflowState.CANCELLED)
                run.state = WorkflowState.CANCELLED.value
                job = session.scalar(select(JobRow).where(JobRow.run_id == run_id))
                if job:
                    job.state = "cancelled"
            elif state is WorkflowState.RUNNING:
                validate_workflow_transition(state, WorkflowState.CANCELLING)
                run.state = WorkflowState.CANCELLING.value
            elif state in {WorkflowState.FAILED_RETRYABLE, WorkflowState.TIMED_OUT}:
                validate_workflow_transition(state, WorkflowState.CANCELLED)
                run.state = WorkflowState.CANCELLED.value
                job = session.scalar(select(JobRow).where(JobRow.run_id == run_id))
                if job:
                    job.state = "cancelled"
            elif state is WorkflowState.CANCELLING:
                return self._run(run)
            else:
                raise ConflictError(
                    "run_not_cancellable", "当前运行状态不能取消。", state=state.value
                )
            run.cancel_requested = True
            run.version += 1
            run.updated_at = datetime.now(UTC)
            session.flush()
            return self._run(run)

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

    def claim_next_job(self, worker_id: str, lease_seconds: int) -> ClaimedJob | None:
        now = datetime.now(UTC)
        with self._write_session() as session:
            expired = session.scalars(
                select(JobRow).where(
                    JobRow.state == "running",
                    JobRow.lease_expires_at.is_not(None),
                    JobRow.lease_expires_at < now,
                )
            ).all()
            for exhausted in expired:
                run = session.get(WorkflowRunRow, exhausted.run_id)
                if run is None:
                    exhausted.state = "failed_terminal"
                    exhausted.error = "worker lease expired after retry budget was exhausted"
                    exhausted.lease_owner = None
                    exhausted.lease_expires_at = None
                    exhausted.updated_at = now
                    continue
                current = WorkflowState(run.state)
                if not (
                    exhausted.attempt >= exhausted.max_attempts
                    or run.cancel_requested
                    or current is WorkflowState.CANCELLING
                ):
                    continue
                target = (
                    WorkflowState.CANCELLED
                    if run.cancel_requested or current is WorkflowState.CANCELLING
                    else WorkflowState.FAILED_TERMINAL
                )
                if current is not target:
                    if current is WorkflowState.RUNNING and target is WorkflowState.CANCELLED:
                        validate_workflow_transition(current, WorkflowState.CANCELLING)
                        validate_workflow_transition(
                            WorkflowState.CANCELLING, WorkflowState.CANCELLED
                        )
                        run.version += 1
                    else:
                        validate_workflow_transition(current, target)
                    run.state = target.value
                    run.version += 1
                run.error = (
                    None
                    if target is WorkflowState.CANCELLED
                    else "worker lease expired after retry budget was exhausted"
                )
                run.updated_at = now
                exhausted.state = (
                    "cancelled" if target is WorkflowState.CANCELLED else "failed_terminal"
                )
                exhausted.error = run.error
                exhausted.lease_owner = None
                exhausted.lease_expires_at = None
                exhausted.updated_at = now
                session.add(
                    RuntimeEventRow(
                        trace_id=current_trace_id(),
                        project_id=run.project_id,
                        run_id=run.run_id,
                        event_type=(
                            "JobLeaseCancellationRecovered"
                            if target is WorkflowState.CANCELLED
                            else "JobRetryBudgetExhausted"
                        ),
                        severity=("warning" if target is WorkflowState.CANCELLED else "error"),
                        payload_json=canonical_json(
                            {
                                "job_id": exhausted.job_id,
                                "attempt": exhausted.attempt,
                                "max_attempts": exhausted.max_attempts,
                                "to_state": target.value,
                            }
                        ),
                    )
                )
            candidate = session.scalar(
                select(JobRow)
                .where(
                    JobRow.attempt < JobRow.max_attempts,
                    or_(
                        JobRow.state == "queued",
                        (JobRow.state == "running")
                        & (JobRow.lease_expires_at.is_not(None))
                        & (JobRow.lease_expires_at < now),
                    ),
                )
                .order_by(JobRow.created_at, JobRow.job_id)
                .limit(1)
            )
            if candidate is None:
                return None
            previous_job_state = candidate.state
            result = cast(
                CursorResult[Any],
                session.execute(
                    update(JobRow)
                    .where(
                        JobRow.job_id == candidate.job_id,
                        JobRow.attempt < JobRow.max_attempts,
                        or_(
                            JobRow.state == "queued",
                            (JobRow.state == "running")
                            & (JobRow.lease_expires_at.is_not(None))
                            & (JobRow.lease_expires_at < now),
                        ),
                    )
                    .execution_options(synchronize_session=False)
                    .values(
                        state="running",
                        lease_owner=worker_id,
                        lease_expires_at=now + timedelta(seconds=lease_seconds),
                        attempt=JobRow.attempt + 1,
                        updated_at=now,
                    )
                ),
            )
            if result.rowcount != 1:
                return None
            session.refresh(candidate)
            run = session.get(WorkflowRunRow, candidate.run_id)
            if run is None:
                raise NotFoundError("run", candidate.run_id)
            previous_run_state = WorkflowState(run.state)
            if previous_run_state in {
                WorkflowState.FAILED_RETRYABLE,
                WorkflowState.TIMED_OUT,
            }:
                validate_workflow_transition(previous_run_state, WorkflowState.QUEUED)
                validate_workflow_transition(WorkflowState.QUEUED, WorkflowState.RUNNING)
                run.version += 2
                run.state = WorkflowState.RUNNING.value
            elif previous_run_state is WorkflowState.QUEUED:
                validate_workflow_transition(previous_run_state, WorkflowState.RUNNING)
                run.version += 1
                run.state = WorkflowState.RUNNING.value
            elif previous_run_state is not WorkflowState.RUNNING:
                raise ConflictError(
                    "job_run_state_mismatch",
                    "可领取 Job 的运行状态与任务状态不一致。",
                    job_state=previous_job_state,
                    run_state=previous_run_state.value,
                )
            elif previous_job_state != "running":
                raise ConflictError(
                    "job_run_state_mismatch",
                    "排队 Job 不能绑定到已经运行的 Workflow。",
                    job_state=previous_job_state,
                    run_state=previous_run_state.value,
                )
            else:
                # Reclaiming an expired lease mutates the observable attempt
                # and lease owner even though the workflow remains RUNNING.
                # Advance the optimistic-concurrency version for that change.
                run.version += 1
            run.attempt += 1
            run.error = None
            run.updated_at = now
            session.add(
                RuntimeEventRow(
                    trace_id=current_trace_id(),
                    project_id=run.project_id,
                    run_id=run.run_id,
                    event_type=(
                        "JobLeaseReclaimed"
                        if previous_job_state == "running"
                        else "WorkflowTransitioned"
                    ),
                    severity="warning" if previous_job_state == "running" else "info",
                    payload_json=canonical_json(
                        {
                            "job_id": candidate.job_id,
                            "worker_id": worker_id,
                            "from_state": previous_run_state.value,
                            "to_state": WorkflowState.RUNNING.value,
                            "attempt": candidate.attempt,
                            "max_attempts": candidate.max_attempts,
                        }
                    ),
                )
            )
            return {
                "job_id": candidate.job_id,
                "run_id": candidate.run_id,
                "payload": _load(candidate.payload_json, {}),
                "attempt": candidate.attempt,
                "max_attempts": candidate.max_attempts,
            }

    def is_cancel_requested(self, run_id: str) -> bool:
        with self.database.session_factory() as session:
            row = session.get(WorkflowRunRow, run_id)
            return bool(row and row.cancel_requested)

    def renew_job_lease(self, job_id: str, worker_id: str, lease_seconds: int) -> bool:
        now = datetime.now(UTC)
        with self._write_session() as session:
            result = cast(
                CursorResult[Any],
                session.execute(
                    update(JobRow)
                    .where(
                        JobRow.job_id == job_id,
                        JobRow.state == "running",
                        JobRow.lease_owner == worker_id,
                        JobRow.lease_expires_at.is_not(None),
                        JobRow.lease_expires_at > now,
                    )
                    .values(
                        lease_expires_at=now + timedelta(seconds=lease_seconds),
                        updated_at=now,
                    )
                ),
            )
            return result.rowcount == 1

    @staticmethod
    def _owned_active_job(
        session: Session, job_id: str, worker_id: str, now: datetime
    ) -> JobRow | None:
        job = session.get(JobRow, job_id)
        if (
            job is None
            or job.state != "running"
            or job.lease_owner != worker_id
            or job.lease_expires_at is None
            or _as_utc(job.lease_expires_at) <= now
        ):
            return None
        return job

    @staticmethod
    def _add_run_event(
        session: Session,
        *,
        run: WorkflowRunRow,
        event_type: str,
        severity: str,
        payload: dict[str, object],
    ) -> None:
        session.add(
            RuntimeEventRow(
                trace_id=current_trace_id(),
                project_id=run.project_id,
                run_id=run.run_id,
                event_type=event_type,
                severity=severity,
                payload_json=canonical_json(redact_event_payload(payload)),
            )
        )

    def finalize_job_success(
        self,
        job_id: str,
        worker_id: str,
        *,
        result: dict[str, object],
        artifacts: tuple[dict[str, Any], ...],
        actor: str,
    ) -> bool:
        """Atomically register artifacts and move both Job and Run to their success gates."""

        now = datetime.now(UTC)
        with self._write_session() as session:
            job = self._owned_active_job(session, job_id, worker_id, now)
            if job is None:
                return False
            run = session.get(WorkflowRunRow, job.run_id)
            if run is None:
                raise NotFoundError("run", job.run_id)
            if run.cancel_requested or WorkflowState(run.state) is WorkflowState.CANCELLING:
                return self._finalize_cancel_in_session(
                    session,
                    job=job,
                    run=run,
                    now=now,
                    error="cancellation won race with executor completion",
                    actor=actor,
                )
            current = WorkflowState(run.state)
            payload = _load(job.payload_json, {})
            is_statistics_mock = (
                isinstance(payload, dict) and payload.get("run_kind") == "statistics_mock"
            )
            target = WorkflowState.SUCCEEDED if is_statistics_mock else WorkflowState.QC_REVIEW
            validate_workflow_transition(current, target)
            self._register_artifacts_in_session(
                session,
                project_id=run.project_id,
                run=run,
                artifacts=artifacts,
            )
            job.state = "succeeded"
            job.result_json = canonical_json(result)
            job.error = None
            job.lease_owner = None
            job.lease_expires_at = None
            job.updated_at = now
            run.state = target.value
            run.version += 1
            run.error = None
            run.updated_at = now
            self._add_run_event(
                session,
                run=run,
                event_type="WorkflowTransitioned",
                severity="info",
                payload={
                    "actor": actor,
                    "job_id": job.job_id,
                    "from_state": current.value,
                    "to_state": target.value,
                    "artifact_count": len(artifacts),
                    "reason": (
                        "synthetic statistics mock completed"
                        if is_statistics_mock
                        else "executor completed; manual QC required"
                    ),
                    "synthetic": is_statistics_mock,
                },
            )
            session.flush()
            return True

    def finalize_job_failure(
        self,
        job_id: str,
        worker_id: str,
        *,
        requested_state: WorkflowState,
        error: str,
        actor: str,
    ) -> bool:
        """Atomically requeue or terminalize a failed job with its Workflow state."""

        if requested_state not in {
            WorkflowState.FAILED_RETRYABLE,
            WorkflowState.TIMED_OUT,
            WorkflowState.FAILED_TERMINAL,
        }:
            raise ValueError("requested_state must be a failure state")
        now = datetime.now(UTC)
        with self._write_session() as session:
            job = self._owned_active_job(session, job_id, worker_id, now)
            if job is None:
                return False
            run = session.get(WorkflowRunRow, job.run_id)
            if run is None:
                raise NotFoundError("run", job.run_id)
            if run.cancel_requested or WorkflowState(run.state) is WorkflowState.CANCELLING:
                return self._finalize_cancel_in_session(
                    session,
                    job=job,
                    run=run,
                    now=now,
                    error=error,
                    actor=actor,
                )
            current = WorkflowState(run.state)
            can_retry = (
                requested_state in {WorkflowState.FAILED_RETRYABLE, WorkflowState.TIMED_OUT}
                and job.attempt < job.max_attempts
            )
            target = requested_state if can_retry else WorkflowState.FAILED_TERMINAL
            validate_workflow_transition(current, target)
            job.state = "queued" if can_retry else "failed_terminal"
            job.result_json = None
            job.error = error
            job.lease_owner = None
            job.lease_expires_at = None
            job.updated_at = now
            run.state = target.value
            run.version += 1
            run.error = error
            run.updated_at = now
            self._add_run_event(
                session,
                run=run,
                event_type="WorkflowTransitioned",
                severity="warning" if can_retry else "error",
                payload={
                    "actor": actor,
                    "job_id": job.job_id,
                    "from_state": current.value,
                    "to_state": target.value,
                    "attempt": job.attempt,
                    "max_attempts": job.max_attempts,
                    "reason": "executor did not complete",
                    "error": error,
                },
            )
            session.flush()
            return True

    def finalize_job_cancel(
        self,
        job_id: str,
        worker_id: str,
        *,
        error: str | None,
        actor: str,
    ) -> bool:
        """Atomically acknowledge cancellation for both the Job and Workflow."""

        now = datetime.now(UTC)
        with self._write_session() as session:
            job = self._owned_active_job(session, job_id, worker_id, now)
            if job is None:
                return False
            run = session.get(WorkflowRunRow, job.run_id)
            if run is None:
                raise NotFoundError("run", job.run_id)
            return self._finalize_cancel_in_session(
                session,
                job=job,
                run=run,
                now=now,
                error=error,
                actor=actor,
            )

    def _finalize_cancel_in_session(
        self,
        session: Session,
        *,
        job: JobRow,
        run: WorkflowRunRow,
        now: datetime,
        error: str | None,
        actor: str,
    ) -> bool:
        current = WorkflowState(run.state)
        if current is WorkflowState.RUNNING:
            validate_workflow_transition(current, WorkflowState.CANCELLING)
            run.state = WorkflowState.CANCELLING.value
            run.version += 1
            current = WorkflowState.CANCELLING
        validate_workflow_transition(current, WorkflowState.CANCELLED)
        run.state = WorkflowState.CANCELLED.value
        run.version += 1
        run.cancel_requested = True
        run.error = None
        run.updated_at = now
        job.state = "cancelled"
        job.result_json = None
        job.error = error
        job.lease_owner = None
        job.lease_expires_at = None
        job.updated_at = now
        self._add_run_event(
            session,
            run=run,
            event_type="WorkflowTransitioned",
            severity="warning",
            payload={
                "actor": actor,
                "job_id": job.job_id,
                "from_state": current.value,
                "to_state": WorkflowState.CANCELLED.value,
                "reason": "executor cancellation finalized",
                "error": error,
            },
        )
        session.flush()
        return True

    def expire_job_lease_for_test(self, run_id: str) -> None:
        """Test seam used to model a worker crash without sleeping."""
        with self._write_session() as session:
            row = session.scalar(select(JobRow).where(JobRow.run_id == run_id))
            if row is None:
                raise NotFoundError("job_for_run", run_id)
            row.state = "running"
            row.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)

    # -- artifacts and events ----------------------------------------------
    def register_artifacts(
        self, project_id: str, run_id: str, artifacts: tuple[dict[str, Any], ...]
    ) -> None:
        with self._write_session() as session:
            run = session.get(WorkflowRunRow, run_id)
            if run is None:
                raise NotFoundError("run", run_id)
            self._register_artifacts_in_session(
                session,
                project_id=project_id,
                run=run,
                artifacts=artifacts,
            )

    def _register_artifacts_in_session(
        self,
        session: Session,
        *,
        project_id: str,
        run: WorkflowRunRow,
        artifacts: tuple[dict[str, Any], ...],
    ) -> None:
        """Validate every artifact before adding any of them to the transaction."""

        if run.project_id != project_id:
            raise ConflictError("cross_project_run", "运行不属于指定项目。")
        plan = session.get(PlanRevisionRow, run.plan_revision_id)
        if plan is None:
            raise NotFoundError("plan_revision", run.plan_revision_id)

        prepared: list[ArtifactRow] = []
        for artifact in artifacts:
            relative_path = _validate_artifact_relative_path(artifact["relative_path"])
            artifact_id = _id()
            provenance = dict(artifact.get("provenance", {}))
            lineage_data = provenance.get("lineage")
            if lineage_data is not None:
                if not isinstance(lineage_data, dict):
                    raise InputValidationError(
                        "artifact_lineage_invalid",
                        "Artifact lineage 必须是结构化对象。",
                    )
                bound_lineage = {**lineage_data, "artifact_id": artifact_id}
                try:
                    lineage = ArtifactLineage.model_validate(bound_lineage)
                except ValidationError as exc:
                    raise InputValidationError(
                        "artifact_lineage_invalid",
                        "Artifact lineage 未通过类型校验。",
                    ) from exc
                if lineage.subject_manifest_hash != plan.manifest_hash:
                    raise ConflictError(
                        "artifact_manifest_mismatch",
                        "Artifact lineage 必须绑定来源计划的 manifest。",
                    )
                provenance["lineage"] = lineage.model_dump(mode="json")
            prepared.append(
                ArtifactRow(
                    artifact_id=artifact_id,
                    project_id=project_id,
                    run_id=run.run_id,
                    artifact_type=str(artifact["artifact_type"]),
                    relative_path=relative_path,
                    checksum=str(artifact["checksum"]),
                    size_bytes=int(artifact["size_bytes"]),
                    provenance_json=canonical_json(provenance),
                )
            )
        session.add_all(prepared)

    def list_artifacts(self, run_id: str) -> list[ArtifactView]:
        with self.database.session_factory() as session:
            rows = session.scalars(
                select(ArtifactRow)
                .where(ArtifactRow.run_id == run_id)
                .order_by(ArtifactRow.created_at)
            ).all()
            return [self._artifact(row) for row in rows]

    def get_artifact(self, artifact_id: str) -> ArtifactView:
        with self.database.session_factory() as session:
            row = session.get(ArtifactRow, artifact_id)
            if row is None:
                raise NotFoundError("artifact", artifact_id)
            return self._artifact(row)

    def assert_artifacts_belong_to_run(self, artifact_ids: tuple[str, ...], run_id: str) -> None:
        with self.database.session_factory() as session:
            for artifact_id in artifact_ids:
                row = session.get(ArtifactRow, artifact_id)
                if row is None:
                    raise NotFoundError("artifact", artifact_id)
                if row.run_id != run_id:
                    raise ConflictError(
                        "artifact_run_mismatch",
                        "Artifact 不属于冻结的 QC 运行。",
                        artifact_id=artifact_id,
                    )

    def create_statistical_result(
        self,
        *,
        project_id: str,
        run_id: str,
        design_revision_id: str,
        mode: str,
        non_scientific: bool,
        non_scientific_reason: str | None,
        bundle_hash: str,
        manifest: dict[str, Any],
        report_markdown: str,
        report_json: str,
        actor: str,
    ) -> StatisticalResultView:
        """Persist one frozen statistical report; identical re-registration is idempotent."""

        now = datetime.now(UTC)
        result_id = str(manifest.get("result_id") or "")
        if not result_id:
            raise InputValidationError("statistical_result_id_missing", "结果清单缺少 result_id。")
        with self._write_session() as session:
            run = session.get(WorkflowRunRow, run_id)
            if run is None:
                raise NotFoundError("run", run_id)
            if run.project_id != project_id:
                raise ConflictError(
                    "cross_project_run",
                    "统计结果所属运行不属于指定项目。",
                    expected=run.project_id,
                    received=project_id,
                )
            existing = session.get(StatisticalResultRow, result_id)
            if existing is not None:
                if existing.bundle_hash != bundle_hash:
                    raise ConflictError(
                        "statistical_result_conflict",
                        "相同 result_id 已登记不同内容的统计结果。",
                        result_id=result_id,
                    )
                return self._statistical_result_view(existing)
            row = StatisticalResultRow(
                result_id=result_id,
                project_id=project_id,
                run_id=run_id,
                design_revision_id=design_revision_id,
                mode=mode,
                non_scientific=non_scientific,
                non_scientific_reason=non_scientific_reason,
                bundle_hash=bundle_hash,
                manifest_json=canonical_json(manifest),
                report_markdown=report_markdown,
                report_json=report_json,
                version=1,
                created_at=now,
            )
            session.add(row)
            session.flush()
            session.add(
                RuntimeEventRow(
                    trace_id=current_trace_id(),
                    project_id=project_id,
                    run_id=run_id,
                    event_type="StatisticalResultRegistered",
                    severity="info",
                    payload_json=canonical_json(
                        redact_event_payload(
                            {
                                "actor": actor,
                                "result_id": result_id,
                                "design_revision_id": design_revision_id,
                                "mode": mode,
                                "bundle_hash": bundle_hash,
                                "synthetic": non_scientific,
                            }
                        )
                    ),
                )
            )
            return self._statistical_result_view(row)

    def list_statistical_results(
        self, *, project_id: str, run_id: str | None = None
    ) -> list[StatisticalResultView]:
        with self.database.session_factory() as session:
            if session.get(ProjectRow, project_id) is None:
                raise NotFoundError("project", project_id)
            query = select(StatisticalResultRow).where(
                StatisticalResultRow.project_id == project_id
            )
            if run_id is not None:
                query = query.where(StatisticalResultRow.run_id == run_id)
            rows = session.scalars(
                query.order_by(
                    StatisticalResultRow.created_at.desc(), StatisticalResultRow.result_id
                )
            ).all()
            return [self._statistical_result_view(row) for row in rows]

    def get_statistical_result(self, result_id: str) -> StatisticalResultDetailView:
        with self.database.session_factory() as session:
            row = session.get(StatisticalResultRow, result_id)
            if row is None:
                raise NotFoundError("statistical_result", result_id)
            return self._statistical_result_detail(row)

    def append_event(
        self,
        *,
        project_id: str | None,
        run_id: str | None,
        event_type: str,
        severity: str,
        payload: dict[str, object],
    ) -> None:
        with self._write_session() as session:
            session.add(
                RuntimeEventRow(
                    trace_id=current_trace_id(),
                    project_id=project_id,
                    run_id=run_id,
                    event_type=event_type,
                    severity=severity,
                    payload_json=canonical_json(redact_event_payload(payload)),
                )
            )

    def list_events(
        self,
        run_id: str | None = None,
        after_event_id: int = 0,
        *,
        project_id: str | None = None,
    ) -> list[RuntimeEventView]:
        with self.database.session_factory() as session:
            query = select(RuntimeEventRow).where(RuntimeEventRow.event_id > after_event_id)
            if run_id is not None:
                query = query.where(RuntimeEventRow.run_id == run_id)
            if project_id is not None:
                query = query.where(RuntimeEventRow.project_id == project_id)
            rows = session.scalars(query.order_by(RuntimeEventRow.event_id)).all()
            return [
                RuntimeEventView(
                    event_id=row.event_id,
                    trace_id=row.trace_id,
                    project_id=row.project_id,
                    run_id=row.run_id,
                    event_type=row.event_type,
                    severity=row.severity,
                    payload=_load(row.payload_json, {}),
                    created_at=_as_utc(row.created_at),
                )
                for row in rows
            ]

    # -- model profiles and Agent results ----------------------------------
    def create_model_profile(self, profile: ModelProfileInput) -> ModelProfileView:
        with self._write_session() as session:
            if session.get(ModelProfileRow, profile.id) is not None:
                raise ConflictError(
                    "model_profile_exists",
                    "同名模型配置已存在; 模型配置是不可变资源。",
                    profile_id=profile.id,
                )
            row = ModelProfileRow(
                profile_id=profile.id,
                profile_json=canonical_json(profile.model_dump(mode="json")),
                version=1,
            )
            session.add(row)
            session.flush()
            return self._model_profile(row)

    def get_model_profile(self, profile_id: str) -> ModelProfileView:
        with self.database.session_factory() as session:
            row = session.get(ModelProfileRow, profile_id)
            if row is None:
                raise NotFoundError("model_profile", profile_id)
            return self._model_profile(row)

    def list_model_profiles(self) -> list[ModelProfileView]:
        with self.database.session_factory() as session:
            rows = session.scalars(
                select(ModelProfileRow).order_by(ModelProfileRow.profile_id)
            ).all()
            return [self._model_profile(row) for row in rows]

    def delete_model_profile(self, profile_id: str) -> None:
        with self._write_session() as session:
            row = session.get(ModelProfileRow, profile_id)
            if row is None:
                raise NotFoundError("model_profile", profile_id)
            session.delete(row)

    def create_agent_task(
        self,
        *,
        project_id: str,
        expected_project_version: int,
        task_type: str,
        result: GatewayResult,
    ) -> AgentTaskView:
        with self._write_session() as session:
            project = session.get(ProjectRow, project_id)
            if project is None:
                raise NotFoundError("project", project_id)
            self._check_version("project", project.version, expected_project_version)
            row = AgentTaskRow(
                task_id=_id(),
                project_id=project_id,
                state="succeeded",
                task_type=task_type,
                context_hash=result.context_hash,
                result_json=canonical_json(result.model_dump(mode="json")),
            )
            session.add(row)
            session.flush()
            return self._agent_task(row)

    def get_agent_task(self, task_id: str) -> AgentTaskView:
        with self.database.session_factory() as session:
            row = session.get(AgentTaskRow, task_id)
            if row is None:
                raise NotFoundError("agent_task", task_id)
            return self._agent_task(row)

    @staticmethod
    def _model_profile(row: ModelProfileRow) -> ModelProfileView:
        return ModelProfileView(
            profile=ModelProfileInput.model_validate(_load(row.profile_json, {})),
            version=row.version,
            created_at=_as_utc(row.created_at),
        )

    @staticmethod
    def _agent_task(row: AgentTaskRow) -> AgentTaskView:
        return AgentTaskView(
            task_id=row.task_id,
            project_id=row.project_id,
            state=row.state,
            result=GatewayResult.model_validate(_load(row.result_json, {})),
            created_at=_as_utc(row.created_at),
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
