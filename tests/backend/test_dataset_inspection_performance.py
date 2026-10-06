from __future__ import annotations

from pathlib import Path

import pytest

from neuroagent.application.hashing import content_hash
from neuroagent.infrastructure.filesystem import dataset_inspector
from neuroagent.infrastructure.filesystem.dataset_inspector import DatasetInspector
from neuroagent.infrastructure.filesystem.path_policy import PathPolicy
from neuroagent.infrastructure.matlab_executor import MatlabJobExecutor

from .conftest import make_bids_dataset


def test_report_discovery_does_not_hash_image_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    dataset = make_bids_dataset(source)
    inspector = DatasetInspector(PathPolicy([source], tmp_path / "work"))

    def unexpected_hash(path: Path) -> str:
        pytest.fail(f"Report discovery hashed a file: {path.name}")

    monkeypatch.setattr(dataset_inspector, "sha256_file", unexpected_hash)
    clock = iter([10.0, 10.002, 10.015])
    monkeypatch.setattr(dataset_inspector, "perf_counter", lambda: next(clock))
    report = inspector.inspect(dataset, report_only=True)

    assert report["profile"]["subject_count"] == 2
    assert len(report["file_sizes"]) == 5
    assert report["file_hashes"] is None
    assert report["inspection_timing_ms"] == 15.0
    assert report["inventory_ms"] == 2.0
    assert report["hashing_ms"] == 0.0
    with pytest.raises(ValueError, match="missing file hash evidence"):
        MatlabJobExecutor._verify_manifest_file_hashes(report, dataset)


def test_frozen_scan_keeps_complete_stable_hashes_and_worker_reverification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    dataset = make_bids_dataset(source)
    inspector = DatasetInspector(PathPolicy([source], tmp_path / "work"))
    original_hash = dataset_inspector.sha256_file
    hashed_files: list[Path] = []

    def observed_hash(path: Path) -> str:
        hashed_files.append(path)
        return original_hash(path)

    monkeypatch.setattr(dataset_inspector, "sha256_file", observed_hash)
    frozen = inspector.inspect(dataset)
    assert len(hashed_files) == 5
    assert set(frozen["file_hashes"]) == set(frozen["file_sizes"])
    assert all(len(value) == 64 for value in frozen["file_hashes"].values())
    assert "inspection_timing_ms" not in frozen
    assert content_hash(frozen) == content_hash(inspector.inspect(dataset))
    MatlabJobExecutor._verify_manifest_file_hashes(frozen, dataset)

    # A same-size sidecar edit cannot be detected by file size alone.
    sidecar = dataset / "dataset_description.json"
    original = sidecar.read_bytes()
    sidecar.write_bytes(original.replace(b"synthetic", b"different"))
    assert sidecar.stat().st_size == len(original)
    with pytest.raises(ValueError, match="file hash changed"):
        MatlabJobExecutor._verify_manifest_file_hashes(frozen, dataset)
    assert content_hash(frozen) != content_hash(inspector.inspect(dataset))
