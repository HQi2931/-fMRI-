"""Fail-closed statistical-result and artifact-evidence contracts."""

from __future__ import annotations

import math
from enum import StrEnum
from pathlib import PurePosixPath

from pydantic import Field, field_validator, model_validator

from neuroagent.domain.fmri.artifacts import FrozenModel
from neuroagent.domain.fmri.statistics import CorrectionSpec


class StatisticalResultMode(StrEnum):
    """Whether a result contains real executor evidence or a test fixture."""

    REAL = "real"
    SYNTHETIC_NON_SCIENTIFIC = "synthetic_non_scientific"


class StatisticalArtifactRole(StrEnum):
    """Roles required to reproduce and audit a statistical result."""

    DESIGN_MATRIX = "design_matrix"
    CONTRAST = "contrast"
    UNCORRECTED_STATISTICAL_MAP = "uncorrected_statistical_map"
    CORRECTED_STATISTICAL_MAP = "corrected_statistical_map"
    EFFECT_MAP = "effect_map"
    CLUSTER_TABLE = "cluster_table"
    EXECUTION_LOG = "execution_log"
    SOFTWARE_VERSION_EVIDENCE = "software_version_evidence"


def _canonical_artifact_type(role: StatisticalArtifactRole, mode: StatisticalResultMode) -> str:
    prefix = (
        "synthetic.non_scientific"
        if mode is StatisticalResultMode.SYNTHETIC_NON_SCIENTIFIC
        else "statistics"
    )
    return f"{prefix}.{role.value}"


class RegisteredArtifactMetadata(FrozenModel):
    """Immutable metadata for one registered statistical result artifact.

    A placeholder deliberately carries no checksum, size, or provenance hash so
    a synthetic fixture cannot be mistaken for executor-verified evidence.
    """

    artifact_id: str = Field(min_length=1)
    role: StatisticalArtifactRole
    artifact_type: str = Field(min_length=1)
    relative_path: str = Field(min_length=1)
    checksum_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    size_bytes: int | None = Field(default=None, ge=0)
    provenance_hash: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    placeholder: bool = False

    @field_validator("relative_path")
    @classmethod
    def relative_path_stays_in_run(cls, value: str) -> str:
        normalized = PurePosixPath(value.replace("\\", "/"))
        if (
            normalized.is_absolute()
            or ".." in normalized.parts
            or normalized.as_posix() == "."
            or (normalized.parts and normalized.parts[0].endswith(":"))
        ):
            raise ValueError("result artifact path must remain inside the run directory")
        return normalized.as_posix()

    @model_validator(mode="after")
    def evidence_matches_placeholder_state(self) -> RegisteredArtifactMetadata:
        evidence = (self.checksum_sha256, self.size_bytes, self.provenance_hash)
        if self.placeholder:
            if any(item is not None for item in evidence):
                raise ValueError("placeholder artifacts must not claim file-integrity evidence")
            return self
        if any(item is None for item in evidence):
            raise ValueError(
                "registered artifacts require checksum, positive size, and provenance hash"
            )
        if self.size_bytes is not None and self.size_bytes <= 0:
            raise ValueError("registered artifact size must be positive")
        return self


class ClusterRecord(FrozenModel):
    """A cluster observation without inferred anatomy or effect interpretation."""

    cluster_id: str = Field(min_length=1)
    source_map_artifact_id: str = Field(min_length=1)
    extent_voxels: int = Field(gt=0)
    peak_statistic: float
    peak_coordinate_mm: tuple[float, float, float]
    coordinate_space: str = Field(min_length=1)

    @field_validator("peak_statistic")
    @classmethod
    def peak_is_finite(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("cluster peak statistic must be finite")
        return value

    @field_validator("peak_coordinate_mm")
    @classmethod
    def coordinate_is_finite(cls, value: tuple[float, float, float]) -> tuple[float, float, float]:
        if not all(math.isfinite(item) for item in value):
            raise ValueError("cluster peak coordinates must be finite")
        return value


class StatisticalResultManifest(FrozenModel):
    """Complete, immutable manifest for real or explicitly synthetic results."""

    schema_version: str = Field(default="1.0", pattern=r"^1\.0$")
    result_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    design_revision_id: str = Field(min_length=1)
    mode: StatisticalResultMode
    non_scientific: bool
    non_scientific_reason: str | None = Field(default=None, min_length=1)
    correction: CorrectionSpec | None = None
    cluster_connectivity_definition: str = Field(min_length=1)
    artifacts: tuple[RegisteredArtifactMetadata, ...] = Field(min_length=1)
    clusters: tuple[ClusterRecord, ...] = ()

    @model_validator(mode="after")
    def result_is_complete_and_unambiguous(self) -> StatisticalResultManifest:
        artifact_ids = tuple(artifact.artifact_id for artifact in self.artifacts)
        if len(set(artifact_ids)) != len(artifact_ids):
            raise ValueError("statistical result artifact IDs must be unique")

        cluster_ids = tuple(cluster.cluster_id for cluster in self.clusters)
        if len(set(cluster_ids)) != len(cluster_ids):
            raise ValueError("cluster IDs must be unique")

        roles = {artifact.role for artifact in self.artifacts}
        required_roles = {
            StatisticalArtifactRole.DESIGN_MATRIX,
            StatisticalArtifactRole.CONTRAST,
            StatisticalArtifactRole.UNCORRECTED_STATISTICAL_MAP,
            StatisticalArtifactRole.EFFECT_MAP,
            StatisticalArtifactRole.CLUSTER_TABLE,
            StatisticalArtifactRole.EXECUTION_LOG,
            StatisticalArtifactRole.SOFTWARE_VERSION_EVIDENCE,
        }
        if self.correction is not None:
            required_roles.add(StatisticalArtifactRole.CORRECTED_STATISTICAL_MAP)
        elif StatisticalArtifactRole.CORRECTED_STATISTICAL_MAP in roles:
            raise ValueError("corrected map requires an explicit CorrectionSpec")

        missing = sorted(role.value for role in required_roles - roles)
        if missing:
            raise ValueError(f"statistical result is incomplete; missing artifact roles: {missing}")

        mismatched_types = tuple(
            (
                artifact.role.value,
                artifact.artifact_type,
                _canonical_artifact_type(artifact.role, self.mode),
            )
            for artifact in self.artifacts
            if artifact.artifact_type != _canonical_artifact_type(artifact.role, self.mode)
        )
        if mismatched_types:
            raise ValueError(
                "statistical artifact role/type mismatch; expected canonical artifact types: "
                f"{mismatched_types}"
            )

        synthetic = self.mode is StatisticalResultMode.SYNTHETIC_NON_SCIENTIFIC
        if synthetic:
            if not self.non_scientific or not self.non_scientific_reason:
                raise ValueError(
                    "synthetic results require an explicit non-scientific marker and reason"
                )
        else:
            if self.non_scientific or self.non_scientific_reason is not None:
                raise ValueError("real result mode must not carry synthetic-result markers")
            if any(artifact.placeholder for artifact in self.artifacts):
                raise ValueError("real result mode forbids placeholder artifacts")

        map_artifact_ids = {
            artifact.artifact_id
            for artifact in self.artifacts
            if artifact.role
            in {
                StatisticalArtifactRole.UNCORRECTED_STATISTICAL_MAP,
                StatisticalArtifactRole.CORRECTED_STATISTICAL_MAP,
            }
        }
        unknown_sources = sorted(
            {
                cluster.source_map_artifact_id
                for cluster in self.clusters
                if cluster.source_map_artifact_id not in map_artifact_ids
            }
        )
        if unknown_sources:
            raise ValueError(
                f"cluster records must reference registered statistical maps: {unknown_sources}"
            )
        return self

    def artifacts_for_role(
        self, role: StatisticalArtifactRole
    ) -> tuple[RegisteredArtifactMetadata, ...]:
        """Return artifacts in stable registration order for the requested role."""

        return tuple(artifact for artifact in self.artifacts if artifact.role is role)
