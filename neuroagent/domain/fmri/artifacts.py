"""Typed fMRI artifacts and processing-lineage contracts.

The domain package never opens image files.  It reasons about immutable metadata
produced by dataset inspection and registered artifact provenance.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator


class FrozenModel(BaseModel):
    """Strict, immutable base model used by scientific contracts."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class ArtifactKind(StrEnum):
    FUNCTIONAL_TIMESERIES = "functional_timeseries"
    BRAIN_MASK = "brain_mask"
    ALFF_MAP = "alff_map"
    FALFF_MAP = "falff_map"
    REHO_MAP = "reho_map"
    STATISTICAL_MAP = "statistical_map"
    CORRECTED_MAP = "corrected_map"
    QC_REPORT = "qc_report"


class MetricScaling(StrEnum):
    RAW = "raw"
    GLOBAL_MEAN = "global_mean"
    Z_SCORE = "z_score"


class FrequencyBand(FrozenModel):
    low_hz: float = Field(ge=0)
    high_hz: float = Field(gt=0)

    @model_validator(mode="after")
    def ordered(self) -> FrequencyBand:
        if self.low_hz >= self.high_hz:
            raise ValueError("frequency band must satisfy low_hz < high_hz")
        return self


class ArtifactLineage(FrozenModel):
    """Minimum provenance needed to decide whether an artifact can be reused."""

    artifact_id: str = Field(min_length=1)
    kind: ArtifactKind
    subject_id: str | None = None
    session_id: str | None = None
    condition: str | None = None
    metric_scaling: MetricScaling | None = None
    # Fail closed: a lineage is untrusted until an executor-side header check
    # records immutable evidence.  Clients and compilers may describe expected
    # metadata, but they must not obtain a reusable ``verified`` artifact merely
    # by omitting this field.
    metadata_verified: bool = False
    tr_seconds: float | None = Field(default=None, gt=0)
    volume_count: int | None = Field(default=None, gt=1)
    metadata_evidence_hash: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    subject_manifest_hash: str = Field(min_length=64, max_length=64)
    space: str = Field(min_length=1)
    grid_signature: str = Field(min_length=1)
    voxel_size_mm: tuple[float, float, float]
    mask_artifact_id: str | None
    mask_grid_signature: str | None
    temporally_filtered: bool
    frequency_band: FrequencyBand | None
    spatially_smoothed: bool
    smoothing_fwhm_mm: tuple[float, float, float] | None
    scrubbed: bool
    producer_step_hash: str = Field(min_length=64, max_length=64)

    @model_validator(mode="after")
    def lineage_is_consistent(self) -> ArtifactLineage:
        metadata_values = (
            self.tr_seconds,
            self.volume_count,
            self.metadata_evidence_hash,
        )
        if self.metadata_verified:
            if self.metadata_evidence_hash is None:
                raise ValueError("verified metadata requires an executor-produced evidence hash")
        elif any(value is not None for value in metadata_values):
            raise ValueError(
                "unverified lineage must not carry trusted TR, volume count, or evidence"
            )

        temporal_kinds = {
            ArtifactKind.FUNCTIONAL_TIMESERIES,
            ArtifactKind.ALFF_MAP,
            ArtifactKind.FALFF_MAP,
            ArtifactKind.REHO_MAP,
        }
        if (
            self.metadata_verified
            and self.kind in temporal_kinds
            and (self.tr_seconds is None or self.volume_count is None)
        ):
            raise ValueError("verified functional and metric lineage requires TR and volume count")
        if (self.tr_seconds is None) != (self.volume_count is None):
            raise ValueError("TR and volume count must be bound together")
        if self.temporally_filtered != (self.frequency_band is not None):
            raise ValueError("temporally_filtered and frequency_band must describe the same state")
        if self.spatially_smoothed != (self.smoothing_fwhm_mm is not None):
            raise ValueError(
                "spatially_smoothed and smoothing_fwhm_mm must describe the same state"
            )
        if any(value <= 0 for value in self.voxel_size_mm):
            raise ValueError("voxel dimensions must be positive")
        if self.smoothing_fwhm_mm is not None and any(
            value <= 0 for value in self.smoothing_fwhm_mm
        ):
            raise ValueError("smoothing FWHM values must be positive")
        if self.mask_artifact_id is None and self.mask_grid_signature is not None:
            raise ValueError("mask_grid_signature requires mask_artifact_id")
        metric_kinds = {
            ArtifactKind.ALFF_MAP,
            ArtifactKind.FALFF_MAP,
            ArtifactKind.REHO_MAP,
        }
        if self.kind in metric_kinds:
            if not self.subject_id:
                raise ValueError("subject-level metric artifacts require subject_id")
            if self.metric_scaling is None:
                raise ValueError("metric artifacts require metric_scaling")
            if not self.metadata_verified:
                raise ValueError("metric artifacts require verified image metadata")
            if not self.mask_matches_grid:
                raise ValueError("metric artifacts require a matching typed mask")
        elif self.metric_scaling is not None:
            raise ValueError("metric_scaling is only valid for subject-level metric artifacts")
        return self

    @property
    def mask_matches_grid(self) -> bool:
        return self.mask_artifact_id is not None and self.mask_grid_signature == self.grid_signature

    def is_compatible_with_mask(self, mask_artifact_id: str) -> bool:
        return self.mask_artifact_id == mask_artifact_id and self.mask_matches_grid


class MetricArtifactExpectation(FrozenModel):
    artifact_kind: ArtifactKind
    scaling: MetricScaling
    primary_endpoint: bool
    expected_relative_pattern: str = Field(min_length=1)
