from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

from neuroagent.infrastructure.persistence.database import Database
from neuroagent.infrastructure.persistence.repository import SqliteRepository


def test_concurrent_plan_revision_allocation_is_serialized(tmp_path) -> None:
    database = Database(f"sqlite:///{(tmp_path / 'concurrent.db').as_posix()}")
    database.initialize()
    repository = SqliteRepository(database)
    project = repository.create_project("concurrency", [str(tmp_path)], str(tmp_path / "work"))
    worker_count = 8
    barrier = Barrier(worker_count)

    def create(index: int) -> int:
        barrier.wait()
        result = repository.create_plan(
            project_id=project.project_id,
            expected_project_version=project.version,
            plan={"index": index},
            plan_hash=f"{index:064x}",
            manifest_hash="a" * 64,
            environment_hash="e" * 64,
            supersedes_id=None,
        )
        return result.revision

    try:
        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            revisions = list(executor.map(create, range(worker_count)))
    finally:
        database.dispose()

    assert sorted(revisions) == list(range(1, worker_count + 1))
