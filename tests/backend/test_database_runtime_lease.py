from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from neuroagent.infrastructure.persistence.database import Database


def test_database_runtime_lease_is_created_and_released(tmp_path: Path) -> None:
    database_path = tmp_path / "metadata.sqlite"
    database = Database(f"sqlite:///{database_path.as_posix()}")

    database.acquire_runtime_lease()

    markers = list(Path(f"{database_path}.runtime-users").glob("*.json"))
    assert len(markers) == 1
    assert json.loads(markers[0].read_text(encoding="utf-8"))["pid"] == os.getpid()

    database.dispose()
    assert not markers[0].exists()


def test_database_runtime_lease_refuses_active_restore_sentinel(tmp_path: Path) -> None:
    database_path = tmp_path / "metadata.sqlite"
    sentinel = Path(f"{database_path}.restore.lock")
    sentinel.write_text('{"pid":1,"owner_token":"restore"}', encoding="utf-8")
    database = Database(f"sqlite:///{database_path.as_posix()}")

    with pytest.raises(RuntimeError, match="restore is in progress"):
        database.acquire_runtime_lease()

    assert list(Path(f"{database_path}.runtime-users").glob("*.json")) == []
    database.dispose()
