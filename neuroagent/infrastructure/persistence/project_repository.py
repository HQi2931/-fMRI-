"""SQLite adapter for project, dataset, and manifest persistence."""

from neuroagent.infrastructure.persistence.database import Database
from neuroagent.infrastructure.persistence.repository_mappers import RepositoryMapperMixin
from neuroagent.infrastructure.persistence.repository_mixins import (
    ProjectDatasetMixin,
)
from neuroagent.infrastructure.persistence.sqlite_adapter import SqliteAdapterMixin
from neuroagent.infrastructure.persistence.unit_of_work import SqliteUnitOfWork


class SqliteProjectRepository(
    SqliteAdapterMixin,
    RepositoryMapperMixin,
    ProjectDatasetMixin,
):
    """Project capability adapter sharing the application's SQLite Unit of Work.

    Project use cases receive only ProjectRepositoryPort. Idempotency and event
    stores are separate adapters sharing this Unit of Work.
    """

    def __init__(self, database: Database, *, unit_of_work: SqliteUnitOfWork) -> None:
        super().__init__(database, unit_of_work=unit_of_work)
