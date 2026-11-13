"""SQLAlchemy repository implementing immutable revisions and atomic job claims."""

from __future__ import annotations

from neuroagent.infrastructure.persistence.repository_mappers import RepositoryMapperMixin
from neuroagent.infrastructure.persistence.repository_mixins import (
    ArtifactCatalogMixin,
    ConversationMixin,
    ExternalAnalysisImportMixin,
    IdempotencyMixin,
    JobExecutionMixin,
    LiteratureMixin,
    ModelAgentMixin,
    PlanApprovalMixin,
    ProjectDatasetMixin,
    QcReviewMixin,
    RunMixin,
    RuntimeEventMixin,
    WorkspaceAssistantMixin,
)
from neuroagent.infrastructure.persistence.repository_mixins._base import (
    _validate_artifact_relative_path as _validate_artifact_relative_path,
)
from neuroagent.infrastructure.persistence.sqlite_adapter import SqliteAdapterMixin


class SqliteRepository(
    SqliteAdapterMixin,
    RepositoryMapperMixin,
    IdempotencyMixin,
    ProjectDatasetMixin,
    PlanApprovalMixin,
    RunMixin,
    QcReviewMixin,
    ArtifactCatalogMixin,
    JobExecutionMixin,
    ExternalAnalysisImportMixin,
    LiteratureMixin,
    ModelAgentMixin,
    ConversationMixin,
    WorkspaceAssistantMixin,
    RuntimeEventMixin,
):
    pass
