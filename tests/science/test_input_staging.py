from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from pydantic import ValidationError

from neuroagent.tools import (
    DpabiBidsConverterJobPlan,
    DpabiBidsConverterPlanTool,
    InputFormat,
    ManifestFile,
    ReadOnlyInputClassifier,
    StagingCopyOperation,
    StagingCopyTool,
)


def _write(path: Path, content: bytes = b"synthetic") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.mark.parametrize(
    ("layout", "expected"),
    [
        ("bids", InputFormat.BIDS_NIFTI),
        ("nifti", InputFormat.NIFTI),
        ("dpabi", InputFormat.DPABI_READY),
        ("dicom", InputFormat.DICOM),
    ],
)
def test_read_only_classifier_recognizes_supported_inputs(
    tmp_path: Path, layout: str, expected: InputFormat
) -> None:
    source = tmp_path / layout
    source.mkdir()
    if layout == "bids":
        _write(source / "dataset_description.json", b"{}")
        _write(source / "sub-01" / "func" / "sub-01_task-rest_bold.nii.gz")
    elif layout == "nifti":
        _write(source / "sub-01_rest.nii")
    elif layout == "dpabi":
        _write(source / "FunImg" / "sub-01" / "sub-01.nii")
    else:
        _write(source / "sub-01" / "image.dcm", b"\x00" * 128 + b"DICM" + b"data")
    before = {
        path: (path.stat().st_mtime_ns, _digest(path))
        for path in source.rglob("*")
        if path.is_file()
    }
    result = ReadOnlyInputClassifier().classify("dataset-source", source, scan_file_limit=100)
    after = {
        path: (path.stat().st_mtime_ns, _digest(path))
        for path in source.rglob("*")
        if path.is_file()
    }
    assert result.input_format is expected
    assert not result.blocking_issues
    assert before == after


def test_mixed_input_and_scan_limit_are_blocked(tmp_path: Path) -> None:
    source = tmp_path / "mixed"
    _write(source / "one.nii")
    _write(source / "two.dcm", b"\x00" * 128 + b"DICM")
    classifier = ReadOnlyInputClassifier()
    mixed = classifier.classify("source", source, scan_file_limit=10)
    assert mixed.input_format is InputFormat.UNKNOWN
    assert "MIXED_DICOM_NIFTI_INPUT" in mixed.blocking_issues
    limited = classifier.classify("source", source, scan_file_limit=1)
    assert limited.blocking_issues == ("SCAN_FILE_LIMIT_EXCEEDED",)


def test_staging_plan_is_deterministic_idempotent_and_source_read_only(tmp_path: Path) -> None:
    source = tmp_path / "source"
    image = _write(source / "sub-01_rest.nii", b"synthetic-nifti")
    classification = ReadOnlyInputClassifier().classify(
        "dataset-source", source, scan_file_limit=10
    )
    manifest_file = ManifestFile(
        artifact_id="file-001",
        relative_path="sub-01_rest.nii",
        sha256=_digest(image),
        size_bytes=image.stat().st_size,
    )
    tool = StagingCopyTool()
    first = tool.plan(
        classification,
        input_manifest_hash="a" * 64,
        files=(manifest_file,),
    )
    second = tool.plan(
        classification,
        input_manifest_hash="a" * 64,
        files=(manifest_file,),
    )
    assert first == second
    assert first.source_read_only is True
    before = (image.stat().st_mtime_ns, _digest(image))
    run_root = tmp_path / "run"
    run_root.mkdir()
    result = tool.execute(first, source_root=source, run_root=run_root)
    repeated = tool.execute(first, source_root=source, run_root=run_root)
    staged = run_root / "staging" / "nifti" / "sub-01_rest.nii"
    assert staged.read_bytes() == image.read_bytes()
    assert result == repeated
    assert before == (image.stat().st_mtime_ns, _digest(image))


def test_staging_rejects_path_traversal_and_changed_source(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="relative"):
        StagingCopyOperation(
            source_artifact_id="file",
            source_relative_path="../../raw.nii",
            destination_relative_path="staging/raw.nii",
            sha256="a" * 64,
            size_bytes=1,
        )
    source = tmp_path / "source"
    image = _write(source / "one.nii", b"first")
    classification = ReadOnlyInputClassifier().classify("source", source, scan_file_limit=10)
    plan = StagingCopyTool().plan(
        classification,
        input_manifest_hash="a" * 64,
        files=(
            ManifestFile(
                artifact_id="file",
                relative_path="one.nii",
                sha256=_digest(image),
                size_bytes=image.stat().st_size,
            ),
        ),
    )
    image.write_bytes(b"changed")
    run_root = tmp_path / "run"
    run_root.mkdir()
    with pytest.raises(ValueError, match=r"source size changed|source hash changed"):
        StagingCopyTool().execute(plan, source_root=source, run_root=run_root)


def test_classifier_rejects_invalid_roots_and_reports_empty_input(tmp_path: Path) -> None:
    classifier = ReadOnlyInputClassifier()
    source = tmp_path / "source"
    source.mkdir()
    with pytest.raises(ValueError, match="positive"):
        classifier.classify("source", source, scan_file_limit=0)

    regular_file = _write(tmp_path / "file.nii")
    with pytest.raises(ValueError, match="directory"):
        classifier.classify("source", regular_file, scan_file_limit=10)

    _write(source / "short-extensionless", b"too-short-for-dicom")
    result = classifier.classify("source", source, scan_file_limit=10)
    assert result.input_format is InputFormat.UNKNOWN
    assert result.blocking_issues == ("UNSUPPORTED_OR_EMPTY_INPUT",)


def test_staging_plan_requires_recognized_unique_nonempty_manifest(tmp_path: Path) -> None:
    source = tmp_path / "source"
    first = _write(source / "first.nii", b"first")
    second = _write(source / "second.nii", b"second")
    classification = ReadOnlyInputClassifier().classify("source", source, scan_file_limit=10)
    tool = StagingCopyTool()

    with pytest.raises(ValueError, match="unblocked recognized"):
        tool.plan(
            classification.model_copy(
                update={"input_format": InputFormat.UNKNOWN, "blocking_issues": ("blocked",)}
            ),
            input_manifest_hash="a" * 64,
            files=(),
        )
    with pytest.raises(ValueError, match="non-empty"):
        tool.plan(classification, input_manifest_hash="a" * 64, files=())

    one = ManifestFile(
        artifact_id="one", relative_path="first.nii", sha256=_digest(first), size_bytes=5
    )
    duplicate_path = one.model_copy(update={"artifact_id": "two"})
    with pytest.raises(ValueError, match="paths must be unique"):
        tool.plan(
            classification,
            input_manifest_hash="a" * 64,
            files=(one, duplicate_path),
        )
    duplicate_id = ManifestFile(
        artifact_id="one", relative_path="second.nii", sha256=_digest(second), size_bytes=6
    )
    with pytest.raises(ValueError, match="Artifact IDs must be unique"):
        tool.plan(
            classification,
            input_manifest_hash="a" * 64,
            files=(one, duplicate_id),
        )
    with pytest.raises(ValidationError, match="name a file"):
        ManifestFile(artifact_id="bad", relative_path=".", sha256="a" * 64, size_bytes=0)


def test_staging_rejects_same_size_source_change_existing_output_and_partial(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    image = _write(source / "one.nii", b"first")
    classification = ReadOnlyInputClassifier().classify("source", source, scan_file_limit=10)
    manifest = ManifestFile(
        artifact_id="file",
        relative_path="one.nii",
        sha256=_digest(image),
        size_bytes=image.stat().st_size,
    )
    tool = StagingCopyTool()
    plan = tool.plan(classification, input_manifest_hash="a" * 64, files=(manifest,))
    run_root = tmp_path / "run"
    run_root.mkdir()

    image.write_bytes(b"other")
    with pytest.raises(ValueError, match="source hash changed"):
        tool.execute(plan, source_root=source, run_root=run_root)
    image.write_bytes(b"first")

    destination = run_root / plan.operations[0].destination_relative_path
    destination.parent.mkdir(parents=True)
    destination.write_bytes(b"different")
    with pytest.raises(ValueError, match="refusing to overwrite"):
        tool.execute(plan, source_root=source, run_root=run_root)
    destination.unlink()

    partial = destination.with_name(f".{destination.name}.{plan.plan_hash[:12]}.partial")
    partial.write_bytes(b"partial")
    with pytest.raises(ValueError, match="temporary path already exists"):
        tool.execute(plan, source_root=source, run_root=run_root)


@pytest.mark.parametrize(
    ("updates", "message"),
    [
        ({"blocking_issues": ("SOURCE_CONTAINS_SYMLINKS",)}, "blocked DICOM"),
        ({"subject_ids": ()}, "non-empty and unique"),
        ({"subject_ids": ("sub-01", "sub-01")}, "non-empty and unique"),
        ({"subject_ids": ("../sub-01",)}, "unsafe characters"),
        ({"functional_session_number": 0}, "must be positive"),
        ({"convert_t1_dicom": False, "deface_t1": True}, "requires T1 DICOM"),
    ],
)
def test_converter_plan_rejects_blocked_or_unsafe_requests(
    tmp_path: Path, updates: dict[str, object], message: str
) -> None:
    source = tmp_path / "dicom"
    _write(source / "image.dcm", b"\x00" * 128 + b"DICM")
    classification = ReadOnlyInputClassifier().classify("source", source, scan_file_limit=10)
    if "blocking_issues" in updates:
        classification = classification.model_copy(
            update={"blocking_issues": updates.pop("blocking_issues")}
        )
    arguments: dict[str, object] = {
        "base_cfg_artifact_id": "converter-cfg",
        "subject_ids": ("sub-01",),
        "functional_session_number": 1,
        "convert_t1_dicom": True,
        "convert_to_bids": True,
        "deface_t1": False,
        "tr_seconds": 2.0,
        "expected_time_points": 240,
    }
    arguments.update(updates)
    with pytest.raises(ValueError, match=message):
        DpabiBidsConverterPlanTool().plan(classification, **arguments)  # type: ignore[arg-type]


def test_converter_plan_cfg_projection_is_bound_to_frozen_plan(tmp_path: Path) -> None:
    source = tmp_path / "dicom"
    _write(source / "image.dcm", b"\x00" * 128 + b"DICM")
    classification = ReadOnlyInputClassifier().classify("source", source, scan_file_limit=10)
    plan = DpabiBidsConverterPlanTool().plan(
        classification,
        base_cfg_artifact_id="converter-cfg",
        subject_ids=("sub-01",),
        functional_session_number=1,
        convert_t1_dicom=False,
        convert_to_bids=False,
        deface_t1=False,
        tr_seconds=None,
        expected_time_points=None,
    )
    values = plan.model_dump(mode="python")
    values["cfg_projection"]["subject_ids"] = ("sub-02",)
    with pytest.raises(ValidationError, match="does not match"):
        DpabiBidsConverterJobPlan.model_validate(values)


def test_dicom_only_produces_controlled_converter_plan(tmp_path: Path) -> None:
    source = tmp_path / "dicom"
    _write(source / "sub-01" / "image.ima", b"\x00" * 128 + b"DICM")
    classification = ReadOnlyInputClassifier().classify("dicom-source", source, scan_file_limit=10)
    plan = DpabiBidsConverterPlanTool().plan(
        classification,
        base_cfg_artifact_id="converter-cfg",
        subject_ids=("sub-01",),
        functional_session_number=1,
        convert_t1_dicom=True,
        convert_to_bids=True,
        deface_t1=True,
        tr_seconds=2.0,
        expected_time_points=240,
    )
    assert plan.function == "DPABI_BIDS_Converter_run"
    assert plan.working_relative_path == "staging/dicom"
    assert plan.cfg_projection.as_dpabi_cfg() == {
        "SubjectID": ["sub-01"],
        "FunctionalSessionNumber": 1,
        "IsNeedConvertFunDCM2IMG": 1,
        "IsNeedConvertT1DCM2IMG": 1,
        "IsConvert2BIDS": 1,
        "IsDeface": 1,
        "TR": 2.0,
        "TimePoints": 240,
        "StartingDirName": "FunRaw",
    }
    with pytest.raises(ValueError, match="classified DICOM"):
        DpabiBidsConverterPlanTool().plan(
            classification.model_copy(update={"input_format": InputFormat.NIFTI}),
            base_cfg_artifact_id="converter-cfg",
            subject_ids=("sub-01",),
            functional_session_number=1,
            convert_t1_dicom=False,
            convert_to_bids=True,
            deface_t1=False,
            tr_seconds=None,
            expected_time_points=None,
        )
