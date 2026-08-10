"""SQLAlchemy engine and session lifecycle."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any
from uuid import uuid4

from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, create_engine, event, text
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from neuroagent.infrastructure.persistence.models import Base


class Database:
    def __init__(self, url: str) -> None:
        self.url = url
        self._database_path: Path | None = None
        self._runtime_marker: Path | None = None
        if url.startswith("sqlite:///") and not url.endswith(":memory:"):
            path_text = url.removeprefix("sqlite:///")
            self._database_path = Path(path_text).expanduser().resolve()
            self._database_path.parent.mkdir(parents=True, exist_ok=True)
        options: dict[str, object] = {"connect_args": {"check_same_thread": False}}
        if url.endswith(":memory:"):
            options["poolclass"] = StaticPool
        self.engine: Engine = create_engine(url, **options)
        if url.startswith("sqlite"):
            event.listen(self.engine, "connect", self._configure_sqlite)
        self.session_factory = sessionmaker(
            bind=self.engine, class_=Session, expire_on_commit=False
        )

    @staticmethod
    def _configure_sqlite(dbapi_connection: Any, _connection_record: Any) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.close()

    def initialize(self) -> None:
        if self.url.endswith(":memory:"):
            Base.metadata.create_all(self.engine)
            return
        config = Config()
        migrations = Path(__file__).resolve().parents[1] / "migrations"
        config.set_main_option("script_location", str(migrations))
        config.set_main_option("sqlalchemy.url", self.url.replace("%", "%%"))
        # API and Worker may start together against a brand-new local file.
        # Acquiring SQLite's writer lock before Alembic inspects the schema
        # serializes that first migration and keeps all DDL on this connection.
        with self.engine.connect() as connection:
            connection.exec_driver_sql("BEGIN IMMEDIATE")
            try:
                config.attributes["connection"] = connection
                command.upgrade(config, "head")
            except BaseException:
                connection.rollback()
                raise
            else:
                connection.commit()

    def acquire_runtime_lease(self) -> None:
        """Register this API/Worker process before it opens the SQLite database.

        The restore script creates the adjacent sentinel first. Creating our
        marker before checking that sentinel closes the startup/restore race:
        either restore observes this process or this process refuses startup.
        """

        if self._database_path is None or self._runtime_marker is not None:
            return
        marker_dir = Path(f"{self._database_path}.runtime-users")
        marker_dir.mkdir(parents=True, exist_ok=True)
        marker = marker_dir / f"{os.getpid()}-{uuid4()}.json"
        marker.write_text(
            json.dumps({"pid": os.getpid(), "database_file": self._database_path.name}),
            encoding="utf-8",
        )
        restore_sentinel = Path(f"{self._database_path}.restore.lock")
        if restore_sentinel.exists():
            marker.unlink(missing_ok=True)
            raise RuntimeError("database restore is in progress; runtime startup refused")
        self._runtime_marker = marker

    def release_runtime_lease(self) -> None:
        marker = self._runtime_marker
        self._runtime_marker = None
        if marker is not None:
            marker.unlink(missing_ok=True)

    def ping(self) -> None:
        """Check database liveness without leaking SQL into the application layer."""

        with self.session_factory() as session:
            session.execute(text("SELECT 1"))

    def dispose(self) -> None:
        self.release_runtime_lease()
        self.engine.dispose()
