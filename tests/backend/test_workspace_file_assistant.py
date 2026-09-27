from __future__ import annotations

from pathlib import Path

import pytest

from neuroagent.application.errors import InputValidationError
from neuroagent.infrastructure.filesystem.workspace_files import WorkspaceFilePolicy


def test_workspace_policy_reads_structured_formats_with_locators(tmp_path: Path) -> None:
    (tmp_path / "config.json").write_text('{\n  "server": {"port": 8000}\n}\n', encoding="utf-8")
    (tmp_path / "events.jsonl").write_text(
        '{"event":"start"}\n{"event":"stop"}\n', encoding="utf-8"
    )
    (tmp_path / "table.csv").write_text("name,value\nalpha,1\n", encoding="utf-8")
    policy = WorkspaceFilePolicy(tmp_path)

    config = policy.read("config.json")
    assert config.format == "json"
    assert any(item["json_pointer"] == "/server" for item in config.locators)
    assert policy.read("events.jsonl").locators[1]["record"] == 2
    assert policy.read("table.csv").locators[1]["columns"] == {"name": 1, "value": 2}


def test_workspace_policy_rejects_escape_binary_and_hard_exclusion(tmp_path: Path) -> None:
    (tmp_path / "safe.txt").write_text("token=secret-value\n", encoding="utf-8")
    (tmp_path / "database.db").write_bytes(b"SQLite format 3")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config.txt").write_text("hidden", encoding="utf-8")
    policy = WorkspaceFilePolicy(tmp_path)

    with pytest.raises(InputValidationError):
        policy.read("../safe.txt")
    with pytest.raises(InputValidationError):
        policy.read("database.db")
    with pytest.raises(InputValidationError):
        policy.read(".git/config.txt")
    assert "[redacted-secret]" in policy.read("safe.txt").content


def test_workspace_policy_builds_non_overlapping_atomic_patch(tmp_path: Path) -> None:
    target = tmp_path / "source.py"
    target.write_text("one\ntwo\nthree\nfour\n", encoding="utf-8")
    policy = WorkspaceFilePolicy(tmp_path)
    digest = policy.read("source.py").sha256
    proposal = policy.build_patch(
        [
            {
                "path": "source.py",
                "expected_sha256": digest,
                "start_line": 1,
                "end_line": 1,
                "replacement": "ONE\n",
            },
            {
                "path": "source.py",
                "expected_sha256": digest,
                "start_line": 4,
                "end_line": 4,
                "replacement": "FOUR\n",
            },
        ]
    )
    assert proposal["proposal_hash"]
    policy.apply_patch(proposal["edits"], tmp_path / "backups")
    assert target.read_text(encoding="utf-8") == "ONE\ntwo\nthree\nFOUR\n"
