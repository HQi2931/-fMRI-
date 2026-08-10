from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect


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
    }.issubset(tables)


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
