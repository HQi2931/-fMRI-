"""SQLite persistence adapter composed specifically for the Worker host."""

from neuroagent.infrastructure.persistence.database import Database
from neuroagent.infrastructure.persistence.repository_mappers import RepositoryMapperMixin
from neuroagent.infrastructure.persistence.repository_mixins import (
    ArtifactCatalogMixin,
    ExternalAnalysisImportMixin,
    JobExecutionMixin,
    PlanApprovalMixin,
    ProjectDatasetMixin,
    QcReviewMixin,
    RunMixin,
    RuntimeEventMixin,
)
from neuroagent.infrastructure.persistence.sqlite_adapter import SqliteAdapterMixin
from neuroagent.infrastructure.persistence.unit_of_work import SqliteUnitOfWork


class SqliteWorkerRepository(
    SqliteAdapterMixin,
    RepositoryMapperMixin,
    ProjectDatasetMixin,
    PlanApprovalMixin,
    RunMixin,
    QcReviewMixin,
    ArtifactCatalogMixin,
    JobExecutionMixin,
    ExternalAnalysisImportMixin,
    RuntimeEventMixin,
):
    """Queue and executor catalog records with atomic Artifact/QC finalization."""

    def __init__(self, database: Database, *, unit_of_work: SqliteUnitOfWork) -> None:
        super().__init__(database, unit_of_work=unit_of_work)
