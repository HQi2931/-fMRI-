from __future__ import annotations

import gzip
import os
import struct
from pathlib import Path

import pytest

from neuroagent.application.hashing import content_hash
from neuroagent.infrastructure.filesystem import dataset_inspector
from neuroagent.infrastructure.filesystem.dataset_inspector import DatasetInspector
from neuroagent.infrastructure.filesystem.path_policy import PathPolicy
from neuroagent.infrastructure.matlab_executor import MatlabJobExecutor
from neuroagent.observability.work_timings import capture_work_timings

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
    assert "metadata_revision" not in frozen
    with capture_work_timings() as timings:
        measured = inspector.inspect(dataset)
    assert content_hash(frozen) == content_hash(measured)
    assert timings.snapshot()[0]["stage"] == "hashing"
    assert timings.snapshot()[0]["calls"] == 5
    assert "work_timings" not in measured
    MatlabJobExecutor._verify_manifest_file_hashes(frozen, dataset)

    # A same-size sidecar edit cannot be detected by file size alone.
    sidecar = dataset / "dataset_description.json"
    original = sidecar.read_bytes()
    sidecar.write_bytes(original.replace(b"synthetic", b"different"))
    assert sidecar.stat().st_size == len(original)
    with pytest.raises(ValueError, match="file hash changed"):
        MatlabJobExecutor._verify_manifest_file_hashes(frozen, dataset)
    assert content_hash(frozen) != content_hash(inspector.inspect(dataset))


def test_planning_revision_tracks_metadata_and_inventory_changes(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    dataset = make_bids_dataset(source)
    inspector = DatasetInspector(PathPolicy([source], tmp_path / "work"))

    def revision() -> str:
        report = inspector.inspect(dataset, report_only=True)
        assert report["file_hashes"] is None
        value = report["metadata_revision"]
        assert isinstance(value, str) and len(value) == 64
        return value

    original = revision()
    assert revision() == original
    sidecar = dataset / "dataset_description.json"
    stat = sidecar.stat()
    sidecar.write_bytes(sidecar.read_bytes().replace(b"synthetic", b"different"))
    os.utime(sidecar, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))
    changed = revision()
    assert changed != original
    extra = dataset / "notes.txt"
    extra.write_text("synthetic note", encoding="utf-8")
    added = revision()
    assert added != changed
    extra.rename(dataset / "renamed.txt")
    renamed = revision()
    assert renamed != added
    (dataset / "renamed.txt").unlink()
    assert revision() == changed
    (dataset / "empty-directory").mkdir()
    assert revision() != changed


def test_deep_compressed_inputs_keep_fast_inventory_and_frozen_integrity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source"
    functional = source / "FunImg" / "Sub_001" / "visit" / "acquisition"
    anatomical = source / "T1Img" / "Sub_001" / "visit" / "acquisition"
    header = struct.pack("<I", 348) + bytes(348)
    for directory in (functional, anatomical):
        directory.mkdir(parents=True)
        (directory / "image.nii.gz").write_bytes(gzip.compress(header, mtime=0))
    inspector = DatasetInspector(PathPolicy([source], tmp_path / "work"))
    original_hash = dataset_inspector.sha256_file
    hashed: list[Path] = []

    def observed_hash(path: Path) -> str:
        hashed.append(path)
        return original_hash(path)

    monkeypatch.setattr(dataset_inspector, "sha256_file", observed_hash)
    report = inspector.inspect(source, report_only=True, selected_input_stage="FunImg")
    assert not hashed
    assert report["profile"]["subject_count"] == 1
    assert len(report["file_sizes"]) == 2
    frozen = inspector.inspect(source, selected_input_stage="FunImg")
    assert len(hashed) == 2
    MatlabJobExecutor._verify_manifest_file_hashes(frozen, source)

    # Alter a valid gzip metadata byte without changing file size or voxel data.
    image = functional / "image.nii.gz"
    stat = image.stat()
    changed = bytearray(image.read_bytes())
    changed[4] = 1
    image.write_bytes(changed)
    os.utime(image, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))
    assert gzip.decompress(changed) == header
    assert image.stat().st_size == stat.st_size
    new_report = inspector.inspect(source, report_only=True, selected_input_stage="FunImg")
    assert len(hashed) == 2
    assert new_report["metadata_revision"] != report["metadata_revision"]
    with pytest.raises(ValueError, match="file hash changed"):
        MatlabJobExecutor._verify_manifest_file_hashes(frozen, source)
