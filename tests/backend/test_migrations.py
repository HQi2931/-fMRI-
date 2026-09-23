from __future__ import annotations

import logging
import subprocess
import sys
import textwrap
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect


def test_migrations_do_not_disable_application_loggers(tmp_path: Path) -> None:
    """Running migrations in-process must not switch off neuroagent logging.

    ``fileConfig`` defaults to ``disable_existing_loggers=True``, which silenced
    every already-imported ``neuroagent.*`` logger as soon as Alembic ran.  The
    API's unhandled-error log and the worker's executor traceback both went
    missing in production while every test still passed.
    """

    application_logger = logging.getLogger("neuroagent.workflow.worker")
    assert application_logger.disabled is False

    database_path = tmp_path / "logging.sqlite"
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", f"sqlite:///{database_path.as_posix()}")
    command.upgrade(config, "head")

    assert application_logger.disabled is False, "migrations disabled application logging"
    assert application_logger.isEnabledFor(logging.ERROR), (
        "migrations raised the effective level of application logging"
    )


def test_initial_alembic_migration_creates_metadata_schema(tmp_path: Path) -> None:
    database_path = tmp_path / "migration.sqlite"
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", f"sqlite:///{database_path.as_posix()}")
    command.upgrade(config, "head")
    engine = create_engine(f"sqlite:///{database_path.as_posix()}")
    try:
        tables = set(inspect(engine).get_table_names())
    finally:
        engine.dispose()
    assert {
        "alembic_version",
        "projects",
        "datasets",
        "manifest_revisions",
        "plan_revisions",
        "workflow_runs",
        "jobs",
        "runtime_events",
        "model_profiles",
        "agent_tasks",
        "qc_review_revisions",
        "qc_approval_records",
        "statistical_results",
        "conversations",
        "conversation_messages",
        "conversation_tool_calls",
        "papers",
        "paper_sections",
        "literature_chunks",
    }.issubset(tables)


def test_agent_tool_loop_migration_adds_audit_columns_and_round_trips(tmp_path: Path) -> None:
    """0014 must add the loop columns, and its downgrade must be reversible."""

    database_path = tmp_path / "tool-loop.sqlite"
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", f"sqlite:///{database_path.as_posix()}")
    command.upgrade(config, "head")

    engine = create_engine(f"sqlite:///{database_path.as_posix()}")
    try:
        columns = {
            column["name"] for column in inspect(engine).get_columns("conversation_tool_calls")
        }
    finally:
        engine.dispose()

    assert {"step_index", "risk_tier", "provider_call_id", "duration_ms"}.issubset(columns)

    # Downgrade must not be lossy for the columns that already existed, and the
    # upgrade must be re-runnable (a broken batch alter leaves SQLite mid-way).
    command.downgrade(config, "0013_optional_qc")
    engine = create_engine(f"sqlite:///{database_path.as_posix()}")
    try:
        downgraded = {
            column["name"] for column in inspect(engine).get_columns("conversation_tool_calls")
        }
    finally:
        engine.dispose()
    assert "step_index" not in downgraded
    assert {"tool_call_id", "tool_name", "input_json", "output_json"}.issubset(downgraded)

    command.upgrade(config, "head")
    engine = create_engine(f"sqlite:///{database_path.as_posix()}")
    try:
        restored = {
            column["name"] for column in inspect(engine).get_columns("conversation_tool_calls")
        }
    finally:
        engine.dispose()
    assert {"step_index", "risk_tier", "provider_call_id", "duration_ms"}.issubset(restored)


def test_two_processes_can_initialize_the_same_new_sqlite_database(tmp_path: Path) -> None:
    database_path = tmp_path / "shared.sqlite"
    ready_dir = tmp_path / "ready"
    ready_dir.mkdir()
    repository_root = Path(__file__).resolve().parents[2]
    script = textwrap.dedent(
        """
        import sys
        import time
        from pathlib import Path

        from neuroagent.infrastructure.persistence.database import Database

        database_url, marker_text = sys.argv[1:]
        marker = Path(marker_text)
        marker.write_text("ready", encoding="utf-8")
        deadline = time.monotonic() + 10
        while len(list(marker.parent.glob("process-*"))) < 2:
            if time.monotonic() >= deadline:
                raise TimeoutError("peer process did not become ready")
            time.sleep(0.01)
        database = Database(database_url)
        try:
            database.initialize()
        finally:
            database.dispose()
        """
    )
    url = f"sqlite:///{database_path.as_posix()}"
    processes = [
        subprocess.Popen(
            [sys.executable, "-c", script, url, str(ready_dir / f"process-{index}")],
            cwd=repository_root,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for index in range(2)
    ]
    try:
        results = [process.communicate(timeout=30) for process in processes]
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)

    for process, (stdout, stderr) in zip(processes, results, strict=True):
        assert process.returncode == 0, f"stdout={stdout}\nstderr={stderr}"

    engine = create_engine(url)
    try:
        assert "alembic_version" in inspect(engine).get_table_names()
    finally:
        engine.dispose()
