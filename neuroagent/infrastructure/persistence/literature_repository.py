"""SQLite adapter for the independently versioned Literature capability."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy.orm import Session

from neuroagent.infrastructure.persistence.database import Database
from neuroagent.infrastructure.persistence.repository_mixins.literature import LiteratureMixin
from neuroagent.infrastructure.persistence.unit_of_work import SqliteUnitOfWork


class SqliteLiteratureRepository(LiteratureMixin):
    """Persist literature through its own adapter and shared SQLite Unit of Work."""

    def __init__(self, database: Database) -> None:
        self.database = database
        self.unit_of_work = SqliteUnitOfWork(database)

    @contextmanager
    def _write_session(self) -> Iterator[Session]:
        with self.unit_of_work.write_session() as session:
            yield session
