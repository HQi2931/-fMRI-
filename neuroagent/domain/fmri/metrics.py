"""Scientific parameter models and hard validation rules for rs-fMRI metrics."""

from __future__ import annotations

from enum import StrEnum
from math import isclose

from pydantic import Field, field_validator, model_validator

from neuroagent.domain.fmri.artifacts import (
    ArtifactLineage,
    FrequencyBand,
    FrozenModel,
    MetricScaling,
)


class MetricKind(StrEnum):
    ALFF = "alff"
    FALFF = "falff"
    REHO = "reho"


class ParameterSource(StrEnum):
    USER = "user"
    STUDY_PROTOCOL = "study_protocol"
    DATASET_METADATA = "dataset_metadata"
    REVIEWED_PRESET = "reviewed_preset"


class TemporalFilterTiming(StrEnum):
    DISABLED = "disabled"
    BEFORE_NORMALIZE = "before_normalize"
    AFTER_NORMALIZE = "after_normalize"


class SmoothingTiming(StrEnum):
    DISABLED = "disabled"
    ON_FUNCTIONAL_DATA = "on_functional_data"
    ON_RESULTS = "on_results"


class ParameterProvenance(FrozenModel):
    name: str = Field(min_length=1)
    source: ParameterSource
    evidence_ref: str = Field(min_length=1)


class AlffFalffParameters(FrozenModel):
    """Explicit ALFF/fALFF choices; no study-specific numeric defaults."""

    tr_seconds: float = Field(gt=0)
    frequency_band: FrequencyBand
    requested_metrics: tuple[MetricKind, ...]
    requested_scalings: tuple[MetricScaling, ...]
    mask_artifact_id: str = Field(min_length=1)
    filter_timing: TemporalFilterTiming
    result_smoothing: bool
    result_smoothing_fwhm_mm: tuple[float, float, float] | None
    provenance: tuple[ParameterProvenance, ...]

    @field_validator("mask_artifact_id", mode="before")
    @classmethod
    def mask_is_mandatory(cls, value: object) -> object:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("all ALFF/fALFF outputs require a mask (typed brain mask)")
        return value

    @model_validator(mode="after")
    def validate_parameters(self) -> AlffFalffParameters:
        allowed = {MetricKind.ALFF, MetricKind.FALFF}
        if not self.requested_metrics or not set(self.requested_metrics).issubset(allowed):
            raise ValueError("ALFF/fALFF request must select alff and/or falff")
        if len(set(self.requested_metrics)) != len(self.requested_metrics):
            raise ValueError("requested_metrics must not contain duplicates")
        if not self.requested_scalings:
            raise ValueError("at least one output scaling must be selected")
        nyquist = 1.0 / (2.0 * self.tr_seconds)
        if self.frequency_band.high_hz > nyquist:
            raise ValueError(
                f"frequency high_hz exceeds Nyquist ({nyquist:.9g} Hz for the supplied TR)"
            )
        if self.result_smoothing != (self.result_smoothing_fwhm_mm is not None):
            raise ValueError("result smoothing choice and FWHM must be specified together")
        _require_provenance(
            self.provenance,
            {
                "tr_seconds",
                "frequency_band",
                "requested_metrics",
                "requested_scalings",
                "filter_timing",
                "result_smoothing",
            },
        )
        return self


class RehoParameters(FrozenModel):
    """Explicit ReHo choices mapped to DPABI V8.2 semantics."""

    tr_seconds: float = Field(gt=0)
    temporal_filter_band: FrequencyBand | None
    temporal_filter_add_mean_back: bool | None
    cluster_voxels: int
    mask_artifact_id: str = Field(min_length=1)
    requested_scalings: tuple[MetricScaling, ...]
    smooth_reho: bool
    smooth_reho_fwhm_mm: tuple[float, float, float] | None
    global_result_smoothing: bool
    global_result_smoothing_fwhm_mm: tuple[float, float, float] | None
    provenance: tuple[ParameterProvenance, ...]

    @field_validator("mask_artifact_id", mode="before")
    @classmethod
    def mask_is_mandatory(cls, value: object) -> object:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("all ReHo outputs require a mask (typed brain mask)")
        return value

    @model_validator(mode="after")
    def validate_parameters(self) -> RehoParameters:
        if self.cluster_voxels not in {7, 19, 27}:
            raise ValueError("ReHo cluster_voxels must be one of 7, 19 or 27")
        if self.temporal_filter_band is not None:
            nyquist = 1.0 / (2.0 * self.tr_seconds)
            if self.temporal_filter_band.high_hz > nyquist:
                raise ValueError(f"temporal filter high_hz exceeds Nyquist ({nyquist:.9g} Hz)")
        if (self.temporal_filter_band is not None) != (
            self.temporal_filter_add_mean_back is not None
        ):
            raise ValueError(
                "ReHo temporal filter band and add-mean-back choice must be specified together"
            )
        if not self.requested_scalings:
            raise ValueError("at least one output scaling must be selected")
        if self.smooth_reho != (self.smooth_reho_fwhm_mm is not None):
            raise ValueError("SmoothReHo choice and FWHM must be specified together")
        if self.global_result_smoothing != (self.global_result_smoothing_fwhm_mm is not None):
            raise ValueError("global result smoothing choice and FWHM must be specified together")
        if self.smooth_reho and self.global_result_smoothing:
            raise ValueError("SmoothReHo and global OnResults smoothing would smooth ReHo twice")
        _require_provenance(
            self.provenance,
            {
                "tr_seconds",
                "temporal_filter_band",
                "temporal_filter_add_mean_back",
                "cluster_voxels",
                "requested_scalings",
                "smooth_reho",
                "global_result_smoothing",
            },
        )
        return self


def validate_alff_falff_input(
    artifact: ArtifactLineage, parameters: AlffFalffParameters
) -> tuple[str, ...]:
    """Return blocking codes without mutating or inspecting the image."""

    issues: list[str] = []
    issues.extend(
        _validate_temporal_metadata(
            artifact,
            parameters.tr_seconds,
            (parameters.frequency_band,),
        )
    )
    if MetricKind.FALFF in parameters.requested_metrics and artifact.temporally_filtered:
        issues.append("FALFF_INPUT_ALREADY_FILTERED")
    if artifact.spatially_smoothed:
        issues.append("ALFF_INPUT_SPATIALLY_SMOOTHED")
    if not artifact.is_compatible_with_mask(parameters.mask_artifact_id):
        issues.append("MASK_GRID_MISMATCH")
    if parameters.filter_timing is TemporalFilterTiming.BEFORE_NORMALIZE:
        issues.append("ALFF_FILTER_TIMING_BEFORE_NORMALIZE")
    return tuple(issues)


def validate_reho_input(artifact: ArtifactLineage, parameters: RehoParameters) -> tuple[str, ...]:
    issues: list[str] = []
    issues.extend(
        _validate_temporal_metadata(
            artifact,
            parameters.tr_seconds,
            (parameters.temporal_filter_band,),
        )
    )
    if artifact.spatially_smoothed:
        issues.append("REHO_INPUT_SPATIALLY_SMOOTHED")
    if not artifact.is_compatible_with_mask(parameters.mask_artifact_id):
        issues.append("MASK_GRID_MISMATCH")
    # The reviewed ReHo DAG always owns the optional temporal-filter step.  A
    # pre-filtered checkpoint would otherwise be accepted here and then passed
    # through ``prepare_reho_timeseries`` again, making the declared lineage
    # ambiguous and risking duplicate filtering.  Fail closed even when the
    # existing band happens to equal the requested band.
    if artifact.temporally_filtered:
        issues.append("REHO_INPUT_ALREADY_FILTERED")
    return tuple(issues)


def validate_frequency_resolution(
    *, tr_seconds: float, volume_count: int, bands: tuple[FrequencyBand | None, ...]
) -> tuple[str, ...]:
    """Validate bands against the retained samples, not the scanner schedule.

    ``volume_count`` is the actual number of volumes available to the metric
    after dummy-volume removal and any CUT scrubbing.  The conservative Fourier
    resolution is therefore ``1 / (TR * N)``.  Zero remains valid as an
    explicit low-pass lower bound, but a positive cutoff below that resolution
    and a band ending below that resolution cannot be represented by the data.
    """

    resolution_hz = 1.0 / (tr_seconds * volume_count)
    for band in bands:
        if band is None:
            continue
        if 0 < band.low_hz < resolution_hz or band.high_hz < resolution_hz:
            return ("FREQUENCY_BAND_BELOW_EFFECTIVE_RESOLUTION",)
    return ()


def _validate_temporal_metadata(
    artifact: ArtifactLineage,
    tr_seconds: float,
    bands: tuple[FrequencyBand | None, ...],
) -> tuple[str, ...]:
    issues: list[str] = []
    if not artifact.metadata_verified or artifact.metadata_evidence_hash is None:
        issues.append("INPUT_METADATA_UNVERIFIED")
        return tuple(issues)
    if artifact.tr_seconds is None or artifact.volume_count is None:
        issues.append("EFFECTIVE_VOLUME_COUNT_UNKNOWN")
        return tuple(issues)
    if not isclose(artifact.tr_seconds, tr_seconds, rel_tol=1e-9, abs_tol=1e-9):
        issues.append("TR_LINEAGE_MISMATCH")
    issues.extend(
        validate_frequency_resolution(
            tr_seconds=artifact.tr_seconds,
            volume_count=artifact.volume_count,
            bands=bands,
        )
    )
    return tuple(issues)


def _require_provenance(provenance: tuple[ParameterProvenance, ...], required: set[str]) -> None:
    names = {item.name for item in provenance}
    missing = sorted(required - names)
    if missing:
        raise ValueError(f"scientific parameter provenance missing for: {missing}")
    if len(names) != len(provenance):
        raise ValueError("parameter provenance names must be unique")
