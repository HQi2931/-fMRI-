"""SQLite adapter for workspace consent and patch records."""

from neuroagent.infrastructure.persistence.database import Database
from neuroagent.infrastructure.persistence.repository_mappers import RepositoryMapperMixin
from neuroagent.infrastructure.persistence.repository_mixins import WorkspaceAssistantMixin
from neuroagent.infrastructure.persistence.sqlite_adapter import SqliteAdapterMixin
from neuroagent.infrastructure.persistence.unit_of_work import SqliteUnitOfWork


class SqliteWorkspaceRepository(
    SqliteAdapterMixin,
    RepositoryMapperMixin,
    WorkspaceAssistantMixin,
):
    """Workspace persistence adapter; conversation and profile queries are injected separately."""

    def __init__(self, database: Database, *, unit_of_work: SqliteUnitOfWork) -> None:
        super().__init__(database, unit_of_work=unit_of_work)
