"""Shared persistence state, helpers, and abstract contracts for repository domain mixins."""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from pathlib import PurePosixPath, PureWindowsPath
from typing import Any
from uuid import uuid4

from sqlalchemy.orm import Session

from neuroagent.application.contracts import (
    ApprovalView,
    ArtifactView,
    DatasetSplitView,
    DatasetView,
    DemographicsRevisionView,
    ManifestRevisionView,
    PlanRevisionView,
    ProjectView,
    QcReviewView,
    RunView,
    StatisticalResultDetailView,
    StatisticalResultView,
)
from neuroagent.application.errors import InputValidationError
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


class RepositoryBaseMixin(ABC):
    """Declares shared persistence state and helpers every domain mixin reads via ``self``.

    Concrete bodies live on :class:`SqliteRepository` (transaction helpers and
    response conversions) or on the domain mixin owning the helper
    (``_register_artifacts_in_session`` on ``ArtifactEventMixin``).  The
    abstract contracts below exist so mypy strict can type-check every
    ``self.xxx`` access from within a single mixin's MRO.
    """

    database: Database
    _transaction_session: ContextVar[Session | None]

    @contextmanager
    @abstractmethod
    def _immediate_write_transaction(self) -> Iterator[Session]: ...

    @contextmanager
    @abstractmethod
    def atomic(self) -> Iterator[None]: ...

    @contextmanager
    @abstractmethod
    def _write_session(self) -> Iterator[Session]: ...

    @staticmethod
    @abstractmethod
    def _check_version(resource: str, actual: int, expected: int) -> None: ...

    @staticmethod
    @abstractmethod
    def _project(row: ProjectRow) -> ProjectView: ...

    @staticmethod
    @abstractmethod
    def _dataset(row: DatasetRow) -> DatasetView: ...

    @staticmethod
    @abstractmethod
    def _manifest(row: ManifestRevisionRow) -> ManifestRevisionView: ...

    @staticmethod
    @abstractmethod
    def _demographics(row: DemographicsRevisionRow) -> DemographicsRevisionView: ...

    @staticmethod
    @abstractmethod
    def _split(row: DatasetSplitRevisionRow) -> DatasetSplitView: ...

    @staticmethod
    @abstractmethod
    def _plan(row: PlanRevisionRow) -> PlanRevisionView: ...

    @staticmethod
    @abstractmethod
    def _approval(row: ApprovalRow) -> ApprovalView: ...

    @staticmethod
    @abstractmethod
    def _run(row: WorkflowRunRow) -> RunView: ...

    @staticmethod
    @abstractmethod
    def _qc_review(row: QcReviewRow, approval: QcApprovalRow | None = None) -> QcReviewView: ...

    @staticmethod
    @abstractmethod
    def _artifact(row: ArtifactRow) -> ArtifactView: ...

    @staticmethod
    @abstractmethod
    def _statistical_result_view(row: StatisticalResultRow) -> StatisticalResultView: ...

    @staticmethod
    @abstractmethod
    def _statistical_result_detail(row: StatisticalResultRow) -> StatisticalResultDetailView: ...

    @abstractmethod
    def _register_artifacts_in_session(
        self,
        session: Session,
        *,
        project_id: str,
        run: WorkflowRunRow,
        artifacts: tuple[dict[str, Any], ...],
    ) -> list[ArtifactRow]: ...
