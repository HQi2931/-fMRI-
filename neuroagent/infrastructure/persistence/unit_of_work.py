"""SQLite transaction boundary for one repository adapter instance."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

from sqlalchemy import text
from sqlalchemy.orm import Session

from neuroagent.infrastructure.persistence.database import Database


class SqliteUnitOfWork:
    """Own writer locking and let repository operations join an active unit."""

    def __init__(self, database: Database) -> None:
        self.database = database
        self._transaction_session: ContextVar[Session | None] = ContextVar(
            f"repository_transaction_{id(self)}",
            default=None,
        )

    def current_session(self) -> Session | None:
        """Return the transaction session only when this execution is in a unit."""

        return self._transaction_session.get()

    @contextmanager
    def _immediate_write_transaction(self) -> Iterator[Session]:
        """Acquire SQLite's writer lock before a read-modify-write operation."""

        with self.database.session_factory() as session:
            session.execute(text("BEGIN IMMEDIATE"))
            try:
                yield session
            except BaseException:
                session.rollback()
                raise
            else:
                session.commit()

    @contextmanager
    def atomic(self) -> Iterator[None]:
        """Join repository writes to one transaction, nesting safely."""

        active = self._transaction_session.get()
        if active is not None:
            yield
            return
        with self._immediate_write_transaction() as session:
            token = self._transaction_session.set(session)
            try:
                yield
            finally:
                self._transaction_session.reset(token)

    @contextmanager
    def read_snapshot(self) -> Iterator[None]:
        """Share one deferred SQLite read snapshot across repository adapters."""

        active = self._transaction_session.get()
        if active is not None:
            yield
            return
        with self.database.session_factory() as session:
            # A plain BEGIN is deferred; it does not reserve SQLite's writer
            # lock. The first SELECT establishes the snapshot shared by all
            # adapters using this unit of work.
            session.connection().exec_driver_sql("BEGIN")
            token = self._transaction_session.set(session)
            try:
                yield
            finally:
                self._transaction_session.reset(token)
                session.rollback()

    @contextmanager
    def write_session(self) -> Iterator[Session]:
        active = self._transaction_session.get()
        if active is not None:
            yield active
            return
        with self._immediate_write_transaction() as session:
            yield session
