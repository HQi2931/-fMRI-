"""SQLite adapter for immutable plan revisions and approvals."""

from neuroagent.infrastructure.persistence.database import Database
from neuroagent.infrastructure.persistence.repository_mappers import RepositoryMapperMixin
from neuroagent.infrastructure.persistence.repository_mixins import (
    PlanApprovalMixin,
)
from neuroagent.infrastructure.persistence.sqlite_adapter import SqliteAdapterMixin
from neuroagent.infrastructure.persistence.unit_of_work import SqliteUnitOfWork


class SqlitePlanRepository(
    SqliteAdapterMixin,
    RepositoryMapperMixin,
    PlanApprovalMixin,
):
    """Plan persistence adapter sharing the application transaction boundary."""

    def __init__(self, database: Database, *, unit_of_work: SqliteUnitOfWork) -> None:
        super().__init__(database, unit_of_work=unit_of_work)
