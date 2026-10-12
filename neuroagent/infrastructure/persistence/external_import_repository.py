"""SQLite adapter for externally produced analysis imports."""

from neuroagent.infrastructure.persistence.database import Database
from neuroagent.infrastructure.persistence.repository_mappers import RepositoryMapperMixin
from neuroagent.infrastructure.persistence.repository_mixins import (
    ExternalAnalysisImportMixin,
)
from neuroagent.infrastructure.persistence.sqlite_adapter import SqliteAdapterMixin
from neuroagent.infrastructure.persistence.unit_of_work import SqliteUnitOfWork


class SqliteExternalImportRepository(
    SqliteAdapterMixin,
    RepositoryMapperMixin,
    ExternalAnalysisImportMixin,
):
    """External import persistence adapter with project queries injected above."""

    def __init__(self, database: Database, *, unit_of_work: SqliteUnitOfWork) -> None:
        super().__init__(database, unit_of_work=unit_of_work)
