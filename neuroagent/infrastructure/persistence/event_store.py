"""SQLite adapter for append-only runtime events."""

from neuroagent.infrastructure.persistence.database import Database
from neuroagent.infrastructure.persistence.repository_mappers import RepositoryMapperMixin
from neuroagent.infrastructure.persistence.repository_mixins import RuntimeEventMixin
from neuroagent.infrastructure.persistence.sqlite_adapter import SqliteAdapterMixin
from neuroagent.infrastructure.persistence.unit_of_work import SqliteUnitOfWork


class SqliteEventStore(SqliteAdapterMixin, RepositoryMapperMixin, RuntimeEventMixin):
    """Event store sharing the application's SQLite Unit of Work."""

    def __init__(self, database: Database, *, unit_of_work: SqliteUnitOfWork) -> None:
        super().__init__(database, unit_of_work=unit_of_work)
