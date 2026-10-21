"""Shared SQLite adapter mechanics used by capability repositories."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy.orm import Session

from neuroagent.application.errors import ConflictError
from neuroagent.infrastructure.persistence.database import Database
from neuroagent.infrastructure.persistence.unit_of_work import SqliteUnitOfWork


class SqliteAdapterMixin:
    """Bind persistence mixins to a database and a shared transaction boundary."""

    database: Database
    unit_of_work: SqliteUnitOfWork

    def __init__(
        self,
        database: Database,
        *,
        unit_of_work: SqliteUnitOfWork | None = None,
    ) -> None:
        self.database = database
        self.unit_of_work = unit_of_work or SqliteUnitOfWork(database)

    @contextmanager
    def atomic(self) -> Iterator[None]:
        with self.unit_of_work.atomic():
            yield

    @contextmanager
    def read_snapshot(self) -> Iterator[None]:
        with self.unit_of_work.read_snapshot():
            yield

    @contextmanager
    def _write_session(self) -> Iterator[Session]:
        with self.unit_of_work.write_session() as session:
            yield session

    @staticmethod
    def _check_version(resource: str, actual: int, expected: int) -> None:
        if actual != expected:
            raise ConflictError(
                "revision_conflict",
                f"{resource} 版本已变化, 请刷新后重试。",
                expected=expected,
                actual=actual,
            )
