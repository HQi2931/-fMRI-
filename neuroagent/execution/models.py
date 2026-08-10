"""Structured execution contracts; no free command or script text is accepted."""

from __future__ import annotations

import math
from enum import StrEnum
from pathlib import Path, PurePosixPath

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from neuroagent.domain.fmri.statistics import Tail
from neuroagent.skills.models import stable_hash
from neuroagent.tools.dpabi_v82 import CorrectionCall, DpabiCfgProjection, StatisticsCall


class FrozenExecutionModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class MatlabJobKind(StrEnum):
    DPARSFA_PREPROCESSING = "dparsfa_preprocessing"
    DPABI_STATISTICS = "dpabi_statistics"


class ArtifactPathBinding(FrozenExecutionModel):
    artifact_id: str = Field(min_length=1)
    relative_path: str = Field(min_length=1)
    read_only: bool

    @field_validator("relative_path")
    @classmethod
    def safe_relative_path(cls, value: str) -> str:
        normalized = PurePosixPath(value.replace("\\", "/"))
        if normalized.is_absolute() or ".." in normalized.parts:
            raise ValueError("artifact bindings must remain inside the run directory")
        return normalized.as_posix()


class ExpectedArtifact(FrozenExecutionModel):
    artifact_type: str = Field(min_length=1)
    relative_pattern: str = Field(min_length=1)
    required: bool

    @field_validator("relative_pattern")
    @classmethod
    def safe_pattern(cls, value: str) -> str:
        normalized = PurePosixPath(value.replace("\\", "/"))
        if normalized.is_absolute() or ".." in normalized.parts:
            raise ValueError("expected artifact patterns must remain inside the run directory")
        return normalized.as_posix()


class VerifiedArtifact(FrozenExecutionModel):
    artifact_type: str = Field(min_length=1)
    relative_path: str = Field(min_length=1)
    size_bytes: int = Field(gt=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("relative_path")
    @classmethod
    def safe_relative_path(cls, value: str) -> str:
        normalized = PurePosixPath(value.replace("\\", "/"))
        if normalized.is_absolute() or ".." in normalized.parts:
            raise ValueError("verified artifact paths must remain inside the run directory")
        return normalized.as_posix()


class PreprocessingJobPayload(FrozenExecutionModel):
    base_cfg_artifact_id: str = Field(min_length=1)
    staging_relative_path: str = Field(min_length=1)
    metric_projection: DpabiCfgProjection
    subject_ids: tuple[str, ...]
    base_cfg_allowed_fields: tuple[str, ...]

    @field_validator("staging_relative_path")
    @classmethod
    def staging_is_relative(cls, value: str) -> str:
        normalized = PurePosixPath(value.replace("\\", "/"))
        if normalized.is_absolute() or ".." in normalized.parts:
            raise ValueError("staging path must remain inside the isolated run directory")
        return normalized.as_posix()

    @model_validator(mode="after")
    def base_cfg_fields_are_explicit_and_minimal(self) -> PreprocessingJobPayload:
        supported = {"Realign"}
        fields = set(self.base_cfg_allowed_fields)
        if len(fields) != len(self.base_cfg_allowed_fields):
            raise ValueError("base Cfg allowed fields must be unique")
        unsupported = sorted(fields - supported)
        if unsupported:
            raise ValueError(f"unsupported base Cfg scientific fields: {unsupported}")
        realign_enabled = bool(self.metric_projection.cfg.get("IsRealign", 0))
        if realign_enabled != ("Realign" in fields):
            raise ValueError(
                "base Cfg Realign field must be allowed exactly when realignment is enabled"
            )
        return self


class StatisticsJobPayload(FrozenExecutionModel):
    statistics: StatisticsCall
    correction: CorrectionCall | None

    @model_validator(mode="after")
    def correction_matches_design(self) -> StatisticsJobPayload:
        if self.correction is None:
            return self
        correction_mask = self.correction.parameters.get("mask_artifact_id")
        if correction_mask != self.statistics.mask_artifact_id:
            raise ValueError("test and correction must use the same frozen mask")
        if self.correction.parameters.get("statistic_type") != "T":
            raise ValueError("registered statistical calls currently produce T maps only")
        correction_df = self.correction.parameters.get("df1")
        if not isinstance(correction_df, (int, float)) or not math.isclose(
            float(correction_df), float(self.statistics.residual_df), rel_tol=0.0, abs_tol=1e-9
        ):
            raise ValueError("correction df1 must equal the statistical design residual DF")
        if self.correction.parameters.get("df2") is not None:
            raise ValueError("T-map correction must not declare df2")
        two_sided = self.statistics.tail is Tail.TWO_SIDED
        if self.correction.function.value == "y_FDR_Image" and not two_sided:
            raise ValueError("DPABI V8.2 y_FDR_Image uses two-sided T/Z/R p values")
        if (
            self.correction.function.value == "y_GRF_Threshold"
            and bool(self.correction.parameters["two_tailed"]) != two_sided
        ):
            raise ValueError("GRF two_tailed must match the statistical design tail")
        return self


class MatlabJobSpec(FrozenExecutionModel):
    job_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
    run_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
    kind: MatlabJobKind
    plan_hash: str = Field(min_length=64, max_length=64)
    approval_record_id: str = Field(min_length=1)
    input_manifest_hash: str = Field(min_length=64, max_length=64)
    timeout_seconds: int = Field(gt=0, le=604800)
    artifact_bindings: tuple[ArtifactPathBinding, ...]
    expected_artifacts: tuple[ExpectedArtifact, ...] = Field(min_length=1)
    payload: PreprocessingJobPayload | StatisticsJobPayload

    @model_validator(mode="after")
    def kind_matches_payload(self) -> MatlabJobSpec:
        if self.kind is MatlabJobKind.DPARSFA_PREPROCESSING and not isinstance(
            self.payload, PreprocessingJobPayload
        ):
            raise ValueError("preprocessing job requires PreprocessingJobPayload")
        if self.kind is MatlabJobKind.DPABI_STATISTICS and not isinstance(
            self.payload, StatisticsJobPayload
        ):
            raise ValueError("statistics job requires StatisticsJobPayload")
        artifact_ids = [binding.artifact_id for binding in self.artifact_bindings]
        if len(artifact_ids) != len(set(artifact_ids)):
            raise ValueError("artifact bindings must be unique")
        if any(not binding.read_only for binding in self.artifact_bindings):
            raise ValueError("all source artifact bindings must be read-only")
        if isinstance(self.payload, PreprocessingJobPayload):
            bindings = set(artifact_ids)
            if self.payload.base_cfg_artifact_id not in bindings:
                raise ValueError("base DPARSFA Cfg artifact is not bound")
        return self

    @property
    def job_hash(self) -> str:
        return stable_hash(self.model_dump(mode="json"))


class MatlabEnvironment(FrozenExecutionModel):
    matlab_executable: Path
    matlab_root: Path
    spm_path: Path
    dpabi_path: Path
    matlab_version: str
    spm_version: str
    dpabi_version: str


class RenderedJob(FrozenExecutionModel):
    run_directory: Path
    entry_script: Path
    command: tuple[str, ...]
    job_hash: str = Field(min_length=64, max_length=64)
    generated_files: tuple[str, ...]


class MatlabJobStatus(StrEnum):
    DRY_RUN = "dry_run"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"


class MatlabJobResult(FrozenExecutionModel):
    status: MatlabJobStatus
    job_hash: str = Field(min_length=64, max_length=64)
    exit_code: int | None
    stdout: str
    stderr: str
    rendered: RenderedJob
    registered_artifacts: tuple[VerifiedArtifact, ...]
