"""SQLite adapter for immutable QC reviews and approvals."""

from neuroagent.infrastructure.persistence.database import Database
from neuroagent.infrastructure.persistence.repository_mappers import RepositoryMapperMixin
from neuroagent.infrastructure.persistence.repository_mixins import QcReviewMixin
from neuroagent.infrastructure.persistence.sqlite_adapter import SqliteAdapterMixin
from neuroagent.infrastructure.persistence.unit_of_work import SqliteUnitOfWork


class SqliteQcRepository(SqliteAdapterMixin, RepositoryMapperMixin, QcReviewMixin):
    """Persist QC evidence while sharing run transactions with sibling adapters."""

    def __init__(self, database: Database, *, unit_of_work: SqliteUnitOfWork) -> None:
        super().__init__(database, unit_of_work=unit_of_work)
