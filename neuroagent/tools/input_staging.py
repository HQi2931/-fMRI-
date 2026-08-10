"""Read-only input classification and controlled staging-copy planning."""

from __future__ import annotations

import hashlib
import os
import re
import shutil
from enum import StrEnum
from pathlib import Path, PurePosixPath

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from neuroagent.skills.models import stable_hash


class FrozenInputModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class InputFormat(StrEnum):
    DICOM = "dicom"
    BIDS_NIFTI = "bids_nifti"
    NIFTI = "nifti"
    DPABI_READY = "dpabi_ready"
    UNKNOWN = "unknown"


class InputClassification(FrozenInputModel):
    source_artifact_id: str = Field(min_length=1)
    input_format: InputFormat
    file_count: int = Field(ge=0)
    dicom_count: int = Field(ge=0)
    nifti_count: int = Field(ge=0)
    json_count: int = Field(ge=0)
    evidence_relative_paths: tuple[str, ...]
    blocking_issues: tuple[str, ...]


class ReadOnlyInputClassifier:
    """Inspect names and small file headers without changing source metadata."""

    def classify(
        self,
        source_artifact_id: str,
        source_root: Path,
        *,
        scan_file_limit: int,
    ) -> InputClassification:
        if scan_file_limit <= 0:
            raise ValueError("scan_file_limit must be positive")
        root_was_symlink = source_root.is_symlink()
        root = source_root.resolve(strict=True)
        if not root.is_dir():
            raise ValueError("source root must be a directory")
        relative_files: list[str] = []
        dicom: list[str] = []
        nifti: list[str] = []
        json_files: list[str] = []
        symlinks: list[str] = []
        for directory, directory_names, file_names in os.walk(root, followlinks=False):
            directory_path = Path(directory)
            kept_directories: list[str] = []
            for name in sorted(directory_names):
                candidate = directory_path / name
                relative = candidate.relative_to(root).as_posix()
                if candidate.is_symlink():
                    symlinks.append(relative)
                else:
                    kept_directories.append(name)
            directory_names[:] = kept_directories
            for name in sorted(file_names):
                candidate = directory_path / name
                relative = candidate.relative_to(root).as_posix()
                if candidate.is_symlink():
                    symlinks.append(relative)
                    continue
                relative_files.append(relative)
                if len(relative_files) > scan_file_limit:
                    return InputClassification(
                        source_artifact_id=source_artifact_id,
                        input_format=InputFormat.UNKNOWN,
                        file_count=len(relative_files),
                        dicom_count=len(dicom),
                        nifti_count=len(nifti),
                        json_count=len(json_files),
                        evidence_relative_paths=tuple(relative_files[:scan_file_limit]),
                        blocking_issues=("SCAN_FILE_LIMIT_EXCEEDED",),
                    )
                lowered = name.lower()
                if lowered.endswith((".nii", ".nii.gz")):
                    nifti.append(relative)
                elif lowered.endswith(".json"):
                    json_files.append(relative)
                elif lowered.endswith((".dcm", ".ima")) or _has_dicom_signature(candidate):
                    dicom.append(relative)

        issues: list[str] = []
        if root_was_symlink:
            issues.append("SOURCE_ROOT_IS_SYMLINK")
        if symlinks:
            issues.append("SOURCE_CONTAINS_SYMLINKS")
        file_set = set(relative_files)
        has_bids_description = "dataset_description.json" in file_set
        has_bids_bold = any(_is_bids_bold(path) for path in nifti)
        has_dpabi_layout = any(_is_dpabi_ready_path(path) for path in nifti)
        if has_bids_description and has_bids_bold:
            detected = InputFormat.BIDS_NIFTI
        elif has_dpabi_layout:
            detected = InputFormat.DPABI_READY
        elif dicom and not nifti:
            detected = InputFormat.DICOM
        elif nifti and not dicom:
            detected = InputFormat.NIFTI
        elif dicom and nifti:
            detected = InputFormat.UNKNOWN
            issues.append("MIXED_DICOM_NIFTI_INPUT")
        else:
            detected = InputFormat.UNKNOWN
            issues.append("UNSUPPORTED_OR_EMPTY_INPUT")
        evidence = tuple(
            sorted(
                {
                    *dicom,
                    *nifti,
                    *(path for path in json_files if path == "dataset_description.json"),
                }
            )
        )
        return InputClassification(
            source_artifact_id=source_artifact_id,
            input_format=detected,
            file_count=len(relative_files),
            dicom_count=len(dicom),
            nifti_count=len(nifti),
            json_count=len(json_files),
            evidence_relative_paths=evidence,
            blocking_issues=tuple(issues),
        )


class ManifestFile(FrozenInputModel):
    artifact_id: str = Field(min_length=1)
    relative_path: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(ge=0)

    @field_validator("relative_path")
    @classmethod
    def safe_relative_path(cls, value: str) -> str:
        return _safe_relative(value, "manifest source path")


class StagingCopyOperation(FrozenInputModel):
    source_artifact_id: str = Field(min_length=1)
    source_relative_path: str = Field(min_length=1)
    destination_relative_path: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(ge=0)

    @field_validator("source_relative_path", "destination_relative_path")
    @classmethod
    def safe_paths(cls, value: str) -> str:
        return _safe_relative(value, "staging path")


class StagingCopyPlan(FrozenInputModel):
    source_artifact_id: str = Field(min_length=1)
    input_manifest_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    input_format: InputFormat
    source_read_only: bool
    operations: tuple[StagingCopyOperation, ...]
    plan_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class StagingCopyResult(FrozenInputModel):
    plan_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    copied_relative_paths: tuple[str, ...]
    verified_source_hashes: tuple[str, ...]


class StagingCopyTool:
    def plan(
        self,
        classification: InputClassification,
        *,
        input_manifest_hash: str,
        files: tuple[ManifestFile, ...],
    ) -> StagingCopyPlan:
        if classification.input_format is InputFormat.UNKNOWN or classification.blocking_issues:
            raise ValueError("only an unblocked recognized input can be staged")
        if not files:
            raise ValueError("staging requires a non-empty frozen file manifest")
        relative_paths = [item.relative_path for item in files]
        artifact_ids = [item.artifact_id for item in files]
        if len(relative_paths) != len(set(relative_paths)):
            raise ValueError("manifest file paths must be unique")
        if len(artifact_ids) != len(set(artifact_ids)):
            raise ValueError("manifest Artifact IDs must be unique")
        format_directory = {
            InputFormat.DICOM: "dicom",
            InputFormat.BIDS_NIFTI: "bids",
            InputFormat.NIFTI: "nifti",
            InputFormat.DPABI_READY: "dpabi",
        }[classification.input_format]
        operations = tuple(
            StagingCopyOperation(
                source_artifact_id=item.artifact_id,
                source_relative_path=item.relative_path,
                destination_relative_path=(
                    PurePosixPath("staging") / format_directory / item.relative_path
                ).as_posix(),
                sha256=item.sha256,
                size_bytes=item.size_bytes,
            )
            for item in sorted(files, key=lambda value: value.relative_path)
        )
        body = {
            "source_artifact_id": classification.source_artifact_id,
            "input_manifest_hash": input_manifest_hash,
            "input_format": classification.input_format.value,
            "source_read_only": True,
            "operations": [item.model_dump(mode="json") for item in operations],
        }
        return StagingCopyPlan(
            **body,
            plan_hash=stable_hash(body),
        )

    def execute(
        self,
        plan: StagingCopyPlan,
        *,
        source_root: Path,
        run_root: Path,
    ) -> StagingCopyResult:
        if source_root.is_symlink():
            raise ValueError("staging source root must not be a symlink")
        source = source_root.resolve(strict=True)
        destination_root = run_root.resolve(strict=True)
        copied: list[str] = []
        verified: list[str] = []
        for operation in plan.operations:
            unresolved_source = source / operation.source_relative_path
            if _contains_symlink(unresolved_source, source):
                raise ValueError("staging source must not traverse symlinks")
            source_path = unresolved_source.resolve(strict=True)
            _assert_within(source_path, source)
            if source_path.is_symlink() or not source_path.is_file():
                raise ValueError("staging source must be a regular non-symlink file")
            stat_before = source_path.stat()
            if stat_before.st_size != operation.size_bytes:
                raise ValueError(f"source size changed: {operation.source_relative_path}")
            digest = _sha256(source_path)
            if digest != operation.sha256:
                raise ValueError(f"source hash changed: {operation.source_relative_path}")
            destination = (destination_root / operation.destination_relative_path).resolve()
            _assert_within(destination, destination_root)
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.exists():
                if destination.is_file() and _sha256(destination) == digest:
                    copied.append(operation.destination_relative_path)
                    verified.append(digest)
                    continue
                raise ValueError(f"refusing to overwrite staged file: {destination}")
            temporary = destination.with_name(f".{destination.name}.{plan.plan_hash[:12]}.partial")
            if temporary.exists():
                raise ValueError(f"staging temporary path already exists: {temporary}")
            try:
                shutil.copy2(source_path, temporary)
                if _sha256(temporary) != digest:
                    raise ValueError(f"staged hash mismatch: {operation.destination_relative_path}")
                temporary.rename(destination)
            finally:
                if temporary.exists():
                    temporary.unlink()
            stat_after = source_path.stat()
            if (
                stat_before.st_size,
                stat_before.st_mtime_ns,
                stat_before.st_ctime_ns,
            ) != (stat_after.st_size, stat_after.st_mtime_ns, stat_after.st_ctime_ns):
                raise ValueError(f"source metadata changed while staging: {source_path}")
            copied.append(operation.destination_relative_path)
            verified.append(digest)
        return StagingCopyResult(
            plan_hash=plan.plan_hash,
            copied_relative_paths=tuple(copied),
            verified_source_hashes=tuple(verified),
        )


class DpabiBidsConverterCfgProjection(FrozenInputModel):
    subject_ids: tuple[str, ...]
    functional_session_number: int = Field(gt=0)
    convert_functional_dicom: bool
    convert_t1_dicom: bool
    convert_to_bids: bool
    deface_t1: bool
    tr_seconds: float | None = Field(gt=0)
    expected_time_points: int | None = Field(gt=0)

    def as_dpabi_cfg(self) -> dict[str, object]:
        return {
            "SubjectID": list(self.subject_ids),
            "FunctionalSessionNumber": self.functional_session_number,
            "IsNeedConvertFunDCM2IMG": int(self.convert_functional_dicom),
            "IsNeedConvertT1DCM2IMG": int(self.convert_t1_dicom),
            "IsConvert2BIDS": int(self.convert_to_bids),
            "IsDeface": int(self.deface_t1),
            "TR": self.tr_seconds or 0,
            "TimePoints": self.expected_time_points or 0,
            "StartingDirName": "FunRaw",
        }


class DpabiBidsConverterJobPlan(FrozenInputModel):
    function: str = Field(pattern=r"^DPABI_BIDS_Converter_run$")
    source_artifact_id: str = Field(min_length=1)
    base_cfg_artifact_id: str = Field(min_length=1)
    working_relative_path: str
    subject_ids: tuple[str, ...]
    functional_session_number: int = Field(gt=0)
    convert_functional_dicom: bool
    convert_t1_dicom: bool
    convert_to_bids: bool
    deface_t1: bool
    tr_seconds: float | None = Field(gt=0)
    expected_time_points: int | None = Field(gt=0)
    cfg_projection: DpabiBidsConverterCfgProjection
    plan_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("working_relative_path")
    @classmethod
    def safe_working_path(cls, value: str) -> str:
        return _safe_relative(value, "converter working path")

    @model_validator(mode="after")
    def projection_matches_plan(self) -> DpabiBidsConverterJobPlan:
        projection = self.cfg_projection
        expected = (
            self.subject_ids,
            self.functional_session_number,
            self.convert_functional_dicom,
            self.convert_t1_dicom,
            self.convert_to_bids,
            self.deface_t1,
            self.tr_seconds,
            self.expected_time_points,
        )
        actual = (
            projection.subject_ids,
            projection.functional_session_number,
            projection.convert_functional_dicom,
            projection.convert_t1_dicom,
            projection.convert_to_bids,
            projection.deface_t1,
            projection.tr_seconds,
            projection.expected_time_points,
        )
        if expected != actual:
            raise ValueError("converter Cfg projection does not match the frozen plan")
        return self


class DpabiBidsConverterPlanTool:
    def plan(
        self,
        classification: InputClassification,
        *,
        base_cfg_artifact_id: str,
        subject_ids: tuple[str, ...],
        functional_session_number: int,
        convert_t1_dicom: bool,
        convert_to_bids: bool,
        deface_t1: bool,
        tr_seconds: float | None,
        expected_time_points: int | None,
    ) -> DpabiBidsConverterJobPlan:
        if classification.input_format is not InputFormat.DICOM:
            raise ValueError("DPABI BIDS conversion plans require classified DICOM input")
        if classification.blocking_issues:
            raise ValueError("blocked DICOM input cannot produce a conversion plan")
        if not subject_ids or len(set(subject_ids)) != len(subject_ids):
            raise ValueError("conversion subject IDs must be non-empty and unique")
        if any(not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", item) for item in subject_ids):
            raise ValueError("conversion subject IDs contain unsafe characters")
        if functional_session_number <= 0:
            raise ValueError("functional_session_number must be positive")
        if deface_t1 and not convert_t1_dicom:
            raise ValueError("T1 defacing requires T1 DICOM conversion")
        projection = DpabiBidsConverterCfgProjection(
            subject_ids=subject_ids,
            functional_session_number=functional_session_number,
            convert_functional_dicom=True,
            convert_t1_dicom=convert_t1_dicom,
            convert_to_bids=convert_to_bids,
            deface_t1=deface_t1,
            tr_seconds=tr_seconds,
            expected_time_points=expected_time_points,
        )
        body = {
            "function": "DPABI_BIDS_Converter_run",
            "source_artifact_id": classification.source_artifact_id,
            "base_cfg_artifact_id": base_cfg_artifact_id,
            "working_relative_path": "staging/dicom",
            "subject_ids": subject_ids,
            "functional_session_number": functional_session_number,
            "convert_functional_dicom": True,
            "convert_t1_dicom": convert_t1_dicom,
            "convert_to_bids": convert_to_bids,
            "deface_t1": deface_t1,
            "tr_seconds": tr_seconds,
            "expected_time_points": expected_time_points,
            "cfg_projection": projection.model_dump(mode="json"),
        }
        return DpabiBidsConverterJobPlan(
            function="DPABI_BIDS_Converter_run",
            source_artifact_id=classification.source_artifact_id,
            base_cfg_artifact_id=base_cfg_artifact_id,
            working_relative_path="staging/dicom",
            subject_ids=subject_ids,
            functional_session_number=functional_session_number,
            convert_functional_dicom=True,
            convert_t1_dicom=convert_t1_dicom,
            convert_to_bids=convert_to_bids,
            deface_t1=deface_t1,
            tr_seconds=tr_seconds,
            expected_time_points=expected_time_points,
            cfg_projection=projection,
            plan_hash=stable_hash(body),
        )


def _safe_relative(value: str, label: str) -> str:
    normalized = PurePosixPath(value.replace("\\", "/"))
    if normalized.is_absolute() or ".." in normalized.parts or ":" in value:
        raise ValueError(f"{label} must remain relative")
    if normalized.as_posix() in {"", "."}:
        raise ValueError(f"{label} must name a file or directory")
    return normalized.as_posix()


def _has_dicom_signature(path: Path) -> bool:
    try:
        if path.stat().st_size < 132:
            return False
        with path.open("rb") as stream:
            stream.seek(128)
            return stream.read(4) == b"DICM"
    except OSError:
        return False


def _is_bids_bold(relative_path: str) -> bool:
    parts = PurePosixPath(relative_path).parts
    return (
        len(parts) >= 3
        and parts[0].startswith("sub-")
        and "func" in parts
        and relative_path.lower().endswith(("_bold.nii", "_bold.nii.gz"))
    )


def _is_dpabi_ready_path(relative_path: str) -> bool:
    parts = PurePosixPath(relative_path).parts
    return bool(parts and re.fullmatch(r"(?:S\d+_)?FunImg[A-Z]*", parts[0]))


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _assert_within(candidate: Path, root: Path) -> None:
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"path escapes allowed root: {candidate}") from exc


def _contains_symlink(candidate: Path, root: Path) -> bool:
    current = candidate
    while current != root:
        if current.is_symlink():
            return True
        if root not in current.parents:
            return True
        current = current.parent
    return root.is_symlink()
