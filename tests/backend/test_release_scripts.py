from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def test_backup_manifest_marks_sensitive_metadata_and_excludes_images(tmp_path: Path) -> None:
    source = tmp_path / "source.db"
    destination = tmp_path / "backup" / "neuroagent.db"
    with sqlite3.connect(source) as connection:
        connection.execute(
            "CREATE TABLE demographics (subject_id TEXT, source_path TEXT, mapped_value TEXT)"
        )
        connection.execute(
            "INSERT INTO demographics VALUES (?, ?, ?)",
            ("sub-sensitive", r"D:\\research\\participants.tsv", "case"),
        )

    completed = subprocess.run(
        [
            sys.executable,
            str(REPOSITORY_ROOT / "scripts" / "backup-runtime.py"),
            str(source),
            str(destination),
        ],
        cwd=REPOSITORY_ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )

    assert completed.returncode == 0, completed.stderr
    manifest = json.loads(destination.with_suffix(".json").read_text(encoding="utf-8"))
    assert manifest["includes_research_data"] is True
    assert manifest["contains_sensitive_research_metadata"] is True
    assert manifest["contains_demographics_mappings"] is True
    assert manifest["contains_absolute_source_paths"] is True
    assert manifest["includes_raw_or_derived_imaging_files"] is False
    assert "Protect this backup as sensitive research data" in manifest["handling_notice"]
    assert manifest["sha256"] == hashlib.sha256(destination.read_bytes()).hexdigest()


def _prepare_restore_fixture(tmp_path: Path) -> tuple[Path, Path, Path]:
    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir()
    restore_script = scripts_dir / "restore.ps1"
    shutil.copyfile(REPOSITORY_ROOT / "scripts" / "restore.ps1", restore_script)
    backup_dir = tmp_path / "backups" / "test-backup"
    backup_dir.mkdir(parents=True)
    database = backup_dir / "neuroagent.db"
    database.write_bytes(b"verified metadata backup")
    (backup_dir / "neuroagent.json").write_text(
        json.dumps({"sha256": hashlib.sha256(database.read_bytes()).hexdigest()}),
        encoding="utf-8",
    )
    runtime_dir = tmp_path / "tmp" / "local"
    runtime_dir.mkdir(parents=True)
    return restore_script, backup_dir, runtime_dir


def _windows_process_started_at(pid: int) -> str:
    completed = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-Command",
            f"(Get-Process -Id {pid}).StartTime.ToUniversalTime().ToString('o')",
        ],
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    )
    return completed.stdout.strip()


@pytest.mark.skipif(os.name != "nt", reason="release automation targets Windows PowerShell")
@pytest.mark.parametrize("service_name", ["api", "worker"])
def test_restore_refuses_each_matching_live_service_state(
    tmp_path: Path, service_name: str
) -> None:
    restore_script, backup_dir, runtime_dir = _prepare_restore_fixture(tmp_path)
    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        (runtime_dir / f"{service_name}.json").write_text(
            json.dumps(
                {
                    "pid": process.pid,
                    "started_at": _windows_process_started_at(process.pid),
                    "service": service_name,
                }
            ),
            encoding="utf-8",
        )
        completed = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(restore_script),
                "-BackupDirectory",
                str(backup_dir),
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
    finally:
        process.terminate()
        process.wait(timeout=10)

    combined = completed.stdout + completed.stderr
    assert completed.returncode != 0
    assert f"{service_name} is still running" in combined
    assert not (tmp_path / "work" / "neuroagent.db").exists()


@pytest.mark.skipif(os.name != "nt", reason="release automation targets Windows PowerShell")
def test_restore_ignores_stale_pid_state_when_start_time_does_not_match(tmp_path: Path) -> None:
    restore_script, backup_dir, runtime_dir = _prepare_restore_fixture(tmp_path)
    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        (runtime_dir / "api.json").write_text(
            json.dumps(
                {
                    "pid": process.pid,
                    "started_at": "2000-01-01T00:00:00Z",
                    "service": "api",
                }
            ),
            encoding="utf-8",
        )
        completed = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(restore_script),
                "-BackupDirectory",
                str(backup_dir),
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
    finally:
        process.terminate()
        process.wait(timeout=10)

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert (tmp_path / "work" / "neuroagent.db").read_bytes() == b"verified metadata backup"


@pytest.mark.skipif(os.name != "nt", reason="release automation targets Windows PowerShell")
def test_restore_refuses_live_database_runtime_marker(tmp_path: Path) -> None:
    restore_script, backup_dir, _runtime_dir = _prepare_restore_fixture(tmp_path)
    marker_dir = tmp_path / "work" / "neuroagent.db.runtime-users"
    marker_dir.mkdir(parents=True)
    marker = marker_dir / f"{os.getpid()}-test.json"
    marker.write_text(json.dumps({"pid": os.getpid()}), encoding="utf-8")

    completed = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(restore_script),
            "-BackupDirectory",
            str(backup_dir),
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )

    combined = completed.stdout + completed.stderr
    assert completed.returncode != 0
    assert "database is in use" in combined
    assert not (tmp_path / "work" / "neuroagent.db").exists()
    assert not (tmp_path / "work" / "neuroagent.db.restore.lock").exists()


@pytest.mark.skipif(os.name != "nt", reason="release automation targets Windows PowerShell")
def test_restore_removes_stale_database_runtime_marker(tmp_path: Path) -> None:
    restore_script, backup_dir, _runtime_dir = _prepare_restore_fixture(tmp_path)
    marker_dir = tmp_path / "work" / "neuroagent.db.runtime-users"
    marker_dir.mkdir(parents=True)
    marker = marker_dir / "2147483646-test.json"
    marker.write_text(json.dumps({"pid": 2147483646}), encoding="utf-8")

    completed = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(restore_script),
            "-BackupDirectory",
            str(backup_dir),
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert (tmp_path / "work" / "neuroagent.db").read_bytes() == b"verified metadata backup"
    assert not marker.exists()
    assert not (tmp_path / "work" / "neuroagent.db.restore.lock").exists()


@pytest.mark.skipif(os.name != "nt", reason="release automation targets Windows PowerShell")
def test_restore_refuses_another_live_restore_sentinel(tmp_path: Path) -> None:
    restore_script, backup_dir, _runtime_dir = _prepare_restore_fixture(tmp_path)
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    sentinel = work_dir / "neuroagent.db.restore.lock"
    sentinel.write_text(
        json.dumps({"pid": os.getpid(), "owner_token": "another-restore"}),
        encoding="utf-8",
    )

    completed = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(restore_script),
            "-BackupDirectory",
            str(backup_dir),
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )

    combined = completed.stdout + completed.stderr
    assert completed.returncode != 0
    assert "Another database restore is already in progress" in combined
    assert sentinel.exists()
    assert not (work_dir / "neuroagent.db").exists()


@pytest.mark.skipif(os.name != "nt", reason="release automation targets Windows PowerShell")
def test_restore_reclaims_stale_restore_sentinel(tmp_path: Path) -> None:
    restore_script, backup_dir, _runtime_dir = _prepare_restore_fixture(tmp_path)
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    sentinel = work_dir / "neuroagent.db.restore.lock"
    sentinel.write_text(
        json.dumps({"pid": 2147483646, "owner_token": "stale-restore"}),
        encoding="utf-8",
    )

    completed = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(restore_script),
            "-BackupDirectory",
            str(backup_dir),
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert (work_dir / "neuroagent.db").read_bytes() == b"verified metadata backup"
    assert not sentinel.exists()


@pytest.mark.skipif(os.name != "nt", reason="release automation targets Windows PowerShell")
def test_restore_requires_target_to_match_configured_database(tmp_path: Path) -> None:
    restore_script, backup_dir, _runtime_dir = _prepare_restore_fixture(tmp_path)
    (tmp_path / ".env").write_text(
        "RSFMRI_DATABASE_URL=sqlite:///./work/study-metadata.db\n",
        encoding="utf-8",
    )
    environment = os.environ.copy()
    environment.pop("RSFMRI_DATABASE_URL", None)

    mismatch = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(restore_script),
            "-BackupDirectory",
            str(backup_dir),
        ],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert mismatch.returncode != 0
    assert "does not match RSFMRI_DATABASE_URL" in mismatch.stdout + mismatch.stderr
    assert not (tmp_path / "work" / "neuroagent.db").exists()

    matched = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(restore_script),
            "-BackupDirectory",
            str(backup_dir),
            "-DatabasePath",
            "work/study-metadata.db",
        ],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert matched.returncode == 0, matched.stdout + matched.stderr
    assert (tmp_path / "work" / "study-metadata.db").read_bytes() == b"verified metadata backup"


def test_backup_and_restore_validate_the_configured_database_identity() -> None:
    for script_name in ("backup.ps1", "restore.ps1"):
        script = (REPOSITORY_ROOT / "scripts" / script_name).read_text(encoding="utf-8")
        assert "Get-ConfiguredDatabaseUrl" in script
        assert "Resolve-ConfiguredDatabasePath" in script
        assert "does not match RSFMRI_DATABASE_URL" in script


@pytest.mark.skipif(os.name != "nt", reason="release automation targets Windows PowerShell")
def test_configure_github_stops_when_repository_edit_fails(tmp_path: Path) -> None:
    fake_gh = tmp_path / "gh.cmd"
    fake_gh.write_text(
        '@echo off\r\nif "%1"=="auth" exit /b 0\r\nif "%1"=="repo" exit /b 23\r\nexit /b 0\r\n',
        encoding="ascii",
    )
    environment = os.environ.copy()
    environment["PATH"] = f"{tmp_path}{os.pathsep}{environment['PATH']}"

    completed = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(REPOSITORY_ROOT / "scripts" / "configure-github.ps1"),
        ],
        cwd=REPOSITORY_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )

    combined = completed.stdout + completed.stderr
    assert completed.returncode != 0
    assert "exit code 23" in combined
    assert "GitHub repository settings configured" not in combined


def test_phase_close_checks_each_post_push_github_operation() -> None:
    script = (REPOSITORY_ROOT / "scripts" / "phase-close.ps1").read_text(encoding="utf-8")

    for description in (
        "Create draft pull request",
        "Publish agent-review status",
        "Wait for pull-request checks",
        "Mark pull request ready",
        "Enable automatic squash merge",
    ):
        assert f"Invoke-NativeChecked '{description}'" in script


@pytest.mark.skipif(os.name != "nt", reason="release automation targets Windows PowerShell")
def test_reviewed_tree_verifier_rejects_a_changed_candidate(tmp_path: Path) -> None:
    verifier = REPOSITORY_ROOT / "scripts" / "verify-reviewed-tree.ps1"
    expected_tree = "a" * 40
    review = tmp_path / "review.md"
    review.write_text(
        f"# Review\n\ndecision: pass\nreviewed-tree: {expected_tree}\n",
        encoding="utf-8",
    )

    accepted = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(verifier),
            "-ReviewPath",
            str(review),
            "-ActualTree",
            expected_tree,
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert accepted.returncode == 0, accepted.stdout + accepted.stderr

    changed = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(verifier),
            "-ReviewPath",
            str(review),
            "-ActualTree",
            "b" * 40,
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert changed.returncode != 0
    assert "differs from reviewed tree" in changed.stdout + changed.stderr


def test_phase_close_binds_review_to_candidate_tree_and_checks_staging() -> None:
    script = (REPOSITORY_ROOT / "scripts" / "phase-close.ps1").read_text(encoding="utf-8")

    assert "reviewed-tree:" in script
    assert "verify-reviewed-tree.ps1" in script
    assert "Stage approved candidate content" in script
    assert "Restage approved candidate content" in script
    assert "Stage review attestation" in script
    assert "git add -- $contentPathspecs" in script
    assert "git add -- $reviewPath" in script


def test_local_lifecycle_waits_for_health_and_stops_descendant_processes() -> None:
    start_script = (REPOSITORY_ROOT / "scripts" / "start-local.ps1").read_text(encoding="utf-8")
    stop_script = (REPOSITORY_ROOT / "scripts" / "stop-local.ps1").read_text(encoding="utf-8")
    diagnose_script = (REPOSITORY_ROOT / "scripts" / "diagnose.ps1").read_text(encoding="utf-8")

    assert "neuroagent.api.main" in start_script
    assert "neuroagent.workflow.worker_main" in start_script
    assert "/api/v1/health" in start_script
    assert "API did not become healthy" in start_script
    for script in (start_script, diagnose_script):
        assert "Settings.from_env()" in script
        assert "$apiBaseUri/api/v1/health" in script
        assert "http://127.0.0.1:8000/api/v1/health" not in script
    for script in (start_script, stop_script):
        assert "Get-CimInstance Win32_Process" in script
        assert "Stop-LocalProcessTree" in script
