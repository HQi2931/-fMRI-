"""SQLite adapter for model profiles and Agent task results."""

from neuroagent.infrastructure.persistence.database import Database
from neuroagent.infrastructure.persistence.repository_mappers import RepositoryMapperMixin
from neuroagent.infrastructure.persistence.repository_mixins import (
    ModelAgentMixin,
)
from neuroagent.infrastructure.persistence.sqlite_adapter import SqliteAdapterMixin
from neuroagent.infrastructure.persistence.unit_of_work import SqliteUnitOfWork


class SqliteModelRepository(
    SqliteAdapterMixin,
    RepositoryMapperMixin,
    ModelAgentMixin,
):
    """Model profile persistence adapter sharing the application's Unit of Work."""

    def __init__(self, database: Database, *, unit_of_work: SqliteUnitOfWork) -> None:
        super().__init__(database, unit_of_work=unit_of_work)
