"""Explicit scientific choices for DPABI resting-state preprocessing.

The models in this module deliberately have no study-specific defaults.  A
disabled operation is still an explicit choice, and every enabled operation
must carry the parameters that DPABI V8.2 will consume.
"""

from __future__ import annotations

from enum import IntEnum, StrEnum

from pydantic import Field, model_validator

from neuroagent.domain.fmri.artifacts import FrequencyBand, FrozenModel
from neuroagent.domain.fmri.metrics import (
    ParameterProvenance,
    SmoothingTiming,
    TemporalFilterTiming,
    _require_provenance,
)


class RealignmentOptionsSource(StrEnum):
    DPABI_V82_JOBMAT = "dpabi_v82_jobmat"


class NuisanceTiming(StrEnum):
    AFTER_REALIGN = "after_realign"
    AFTER_NORMALIZE = "after_normalize"


class HeadMotionModel(IntEnum):
    NONE = 0
    SIX_PARAMETERS = 1
    TWELVE_PARAMETERS = 2
    SIX_PARAMETERS_WITH_SQUARES = 3
    FRISTON_24 = 4


class TissueMaskSource(StrEnum):
    SPM = "spm"
    SEGMENT = "segment"


class TissueRegressorMethod(StrEnum):
    MEAN = "mean"
    COMPCOR = "compcor"


class GlobalSignalMaskSource(StrEnum):
    SPM = "spm"
    AUTO_MASK = "auto_mask"


class GlobalSignalMethod(StrEnum):
    MEAN = "mean"


class FdType(StrEnum):
    POWER = "fd_power"
    JENKINSON = "fd_jenkinson"


class ScrubbingMethod(StrEnum):
    CUT = "cut"
    NEAREST = "nearest"
    LINEAR = "linear"
    SPLINE = "spline"
    PCHIP = "pchip"


class ScrubbingTiming(StrEnum):
    AFTER_PREPROCESSING = "after_preprocessing"


class NormalizationMode(IntEnum):
    DISABLED = 0
    EPI_TEMPLATE = 1
    T1_SEGMENT = 2
    DARTEL = 3


class NormalizationTiming(StrEnum):
    ON_FUNCTIONAL_DATA = "on_functional_data"
    ON_RESULTS = "on_results"


class AffineRegularization(StrEnum):
    MNI = "mni"
    EASTERN = "eastern"


class SmoothingMethod(IntEnum):
    SPM = 1
    DARTEL = 2


class SliceTimingParameters(FrozenModel):
    enabled: bool
    slice_count: int | None = Field(ge=1)
    slice_order: tuple[int, ...] | None
    reference_slice: int | None = Field(ge=1)

    @model_validator(mode="after")
    def explicit_enabled_state(self) -> SliceTimingParameters:
        values = (self.slice_count, self.slice_order, self.reference_slice)
        if not self.enabled:
            if any(value is not None for value in values):
                raise ValueError("disabled slice timing must not carry active parameters")
            return self
        if any(value is None for value in values):
            raise ValueError("enabled slice timing requires count, order and reference slice")
        assert self.slice_count is not None
        assert self.slice_order is not None
        assert self.reference_slice is not None
        if len(self.slice_order) != self.slice_count:
            raise ValueError("slice_order length must equal slice_count")
        if set(self.slice_order) != set(range(1, self.slice_count + 1)):
            raise ValueError("slice_order must be a permutation of 1..slice_count")
        if self.reference_slice not in self.slice_order:
            raise ValueError("reference_slice must occur in slice_order")
        return self


class RealignmentParameters(FrozenModel):
    enabled: bool
    options_source: RealignmentOptionsSource | None

    @model_validator(mode="after")
    def approved_options_are_explicit(self) -> RealignmentParameters:
        if self.enabled != (self.options_source is not None):
            raise ValueError("realignment requires the explicit DPABI V8.2 Jobmat options source")
        return self


class MotionCensoringParameters(FrozenModel):
    fd_type: FdType
    fd_threshold_mm: float = Field(gt=0)
    previous_points: int = Field(ge=0)
    later_points: int = Field(ge=0)


class TissueRegressor(FrozenModel):
    enabled: bool
    mask_source: TissueMaskSource | None
    mask_threshold: float | None = Field(gt=0, le=1)
    method: TissueRegressorMethod | None
    compcor_components: int | None = Field(gt=0)

    @model_validator(mode="after")
    def settings_match_enabled_state(self) -> TissueRegressor:
        settings = (self.mask_source, self.mask_threshold, self.method)
        if not self.enabled:
            if any(value is not None for value in (*settings, self.compcor_components)):
                raise ValueError("disabled tissue regressor must not carry active parameters")
            return self
        if any(value is None for value in settings):
            raise ValueError("enabled tissue regressor requires mask, threshold and method")
        if self.method is TissueRegressorMethod.COMPCOR:
            if self.compcor_components is None:
                raise ValueError("CompCor requires an explicit component count")
        elif self.compcor_components is not None:
            raise ValueError("mean tissue regression must not declare CompCor components")
        return self


class GlobalSignalRegressor(FrozenModel):
    enabled: bool
    mask_source: GlobalSignalMaskSource | None
    method: GlobalSignalMethod | None

    @model_validator(mode="after")
    def settings_match_enabled_state(self) -> GlobalSignalRegressor:
        if self.enabled:
            if self.mask_source is None or self.method is None:
                raise ValueError("enabled global-signal regression requires mask and method")
        elif self.mask_source is not None or self.method is not None:
            raise ValueError("disabled global-signal regression must not carry parameters")
        return self


class NuisanceRegressionParameters(FrozenModel):
    enabled: bool
    timing: NuisanceTiming | None
    polynomial_trend: int | None = Field(ge=-1)
    head_motion_model: HeadMotionModel | None
    head_motion_scrubbing: MotionCensoringParameters | None
    white_matter: TissueRegressor | None
    csf: TissueRegressor | None
    global_signal: GlobalSignalRegressor | None
    warp_masks_to_individual_space: bool | None
    add_mean_back: bool | None

    @model_validator(mode="after")
    def settings_match_enabled_state(self) -> NuisanceRegressionParameters:
        settings = (
            self.timing,
            self.polynomial_trend,
            self.head_motion_model,
            self.head_motion_scrubbing,
            self.white_matter,
            self.csf,
            self.global_signal,
            self.warp_masks_to_individual_space,
            self.add_mean_back,
        )
        if not self.enabled:
            if any(value is not None for value in settings):
                raise ValueError("disabled nuisance regression must not carry active parameters")
            return self
        required = (
            self.timing,
            self.polynomial_trend,
            self.head_motion_model,
            self.white_matter,
            self.csf,
            self.global_signal,
            self.warp_masks_to_individual_space,
            self.add_mean_back,
        )
        if any(value is None for value in required):
            raise ValueError("enabled nuisance regression requires every declared choice")
        return self


class NormalizationParameters(FrozenModel):
    mode: NormalizationMode
    timing: NormalizationTiming | None
    bounding_box_mm: tuple[tuple[float, float, float], tuple[float, float, float]] | None
    voxel_size_mm: tuple[float, float, float] | None
    structural_artifact_id: str | None
    affine_regularization: AffineRegularization | None

    @model_validator(mode="after")
    def settings_match_mode(self) -> NormalizationParameters:
        details = (
            self.timing,
            self.bounding_box_mm,
            self.voxel_size_mm,
            self.structural_artifact_id,
            self.affine_regularization,
        )
        if self.mode is NormalizationMode.DISABLED:
            if any(value is not None for value in details):
                raise ValueError("disabled normalization must not carry active parameters")
            return self
        if self.timing is None or self.bounding_box_mm is None or self.voxel_size_mm is None:
            raise ValueError("enabled normalization requires timing, bounding box and voxel size")
        if any(value <= 0 for value in self.voxel_size_mm):
            raise ValueError("normalization voxel sizes must be positive")
        lower, upper = self.bounding_box_mm
        if any(low >= high for low, high in zip(lower, upper, strict=True)):
            raise ValueError("normalization bounding-box lower values must precede upper values")
        structural = self.mode in {NormalizationMode.T1_SEGMENT, NormalizationMode.DARTEL}
        if structural and (not self.structural_artifact_id or self.affine_regularization is None):
            raise ValueError(
                "T1-based normalization requires a structural Artifact and affine regularization"
            )
        if not structural and (
            self.structural_artifact_id is not None or self.affine_regularization is not None
        ):
            raise ValueError("EPI normalization must not declare T1 segmentation parameters")
        return self


class TemporalFilteringParameters(FrozenModel):
    timing: TemporalFilterTiming
    frequency_band: FrequencyBand | None
    add_mean_back: bool | None

    @model_validator(mode="after")
    def settings_match_timing(self) -> TemporalFilteringParameters:
        disabled = self.timing is TemporalFilterTiming.DISABLED
        if disabled and (self.frequency_band is not None or self.add_mean_back is not None):
            raise ValueError("disabled temporal filtering must not carry active parameters")
        if not disabled and (self.frequency_band is None or self.add_mean_back is None):
            raise ValueError("enabled temporal filtering requires a band and add-mean-back choice")
        return self


class ScrubbingParameters(FrozenModel):
    enabled: bool
    timing: ScrubbingTiming | None
    censoring: MotionCensoringParameters | None
    method: ScrubbingMethod | None

    @model_validator(mode="after")
    def settings_match_enabled_state(self) -> ScrubbingParameters:
        if self.enabled:
            if self.timing is None or self.censoring is None or self.method is None:
                raise ValueError("enabled scrubbing requires timing, censoring and method")
        elif self.timing is not None or self.censoring is not None or self.method is not None:
            raise ValueError("disabled scrubbing must not carry active parameters")
        return self


class SmoothingParameters(FrozenModel):
    timing: SmoothingTiming
    method: SmoothingMethod | None
    fwhm_mm: tuple[float, float, float] | None

    @model_validator(mode="after")
    def settings_match_timing(self) -> SmoothingParameters:
        disabled = self.timing is SmoothingTiming.DISABLED
        if disabled and (self.method is not None or self.fwhm_mm is not None):
            raise ValueError("disabled smoothing must not carry active parameters")
        if not disabled and (self.method is None or self.fwhm_mm is None):
            raise ValueError("enabled smoothing requires method and FWHM")
        if self.fwhm_mm is not None and any(value <= 0 for value in self.fwhm_mm):
            raise ValueError("smoothing FWHM values must be positive")
        return self


class PreprocessingParameters(FrozenModel):
    """A complete, provenance-backed common preprocessing decision."""

    tr_seconds: float = Field(gt=0)
    expected_time_points: int | None = Field(gt=0)
    dummy_scans: int = Field(ge=0)
    slice_timing: SliceTimingParameters
    realignment: RealignmentParameters
    nuisance: NuisanceRegressionParameters
    normalization: NormalizationParameters
    detrend: bool
    temporal_filter: TemporalFilteringParameters
    scrubbing: ScrubbingParameters
    smoothing: SmoothingParameters
    provenance: tuple[ParameterProvenance, ...]

    @model_validator(mode="after")
    def validate_pipeline_choices(self) -> PreprocessingParameters:
        if self.expected_time_points is not None and self.dummy_scans >= self.expected_time_points:
            raise ValueError("dummy_scans must be smaller than expected_time_points")
        if self.temporal_filter.frequency_band is not None:
            nyquist = 1.0 / (2.0 * self.tr_seconds)
            if self.temporal_filter.frequency_band.high_hz > nyquist:
                raise ValueError(f"temporal filter high_hz exceeds Nyquist ({nyquist:.9g} Hz)")
        motion_required = self.scrubbing.enabled or (
            self.nuisance.enabled
            and (
                self.nuisance.head_motion_model is not HeadMotionModel.NONE
                or self.nuisance.head_motion_scrubbing is not None
            )
        )
        if motion_required and not self.realignment.enabled:
            raise ValueError("head-motion regression or scrubbing requires realignment")
        if (
            self.detrend
            and self.nuisance.enabled
            and self.nuisance.polynomial_trend is not None
            and self.nuisance.polynomial_trend >= 1
        ):
            raise ValueError("separate detrending duplicates nuisance polynomial detrending")
        if (
            self.nuisance.enabled
            and self.nuisance.timing is NuisanceTiming.AFTER_NORMALIZE
            and (
                self.normalization.mode is NormalizationMode.DISABLED
                or self.normalization.timing is not NormalizationTiming.ON_FUNCTIONAL_DATA
            )
        ):
            raise ValueError(
                "AfterNormalize nuisance regression requires normalization on functional data"
            )
        if (
            self.nuisance.enabled
            and self.nuisance.warp_masks_to_individual_space
            and self.nuisance.timing is not NuisanceTiming.AFTER_REALIGN
        ):
            raise ValueError(
                "individual-space mask warping is only valid for AfterRealign nuisance"
            )
        segment_masks = self.nuisance.enabled and any(
            regressor is not None
            and regressor.enabled
            and regressor.mask_source is TissueMaskSource.SEGMENT
            for regressor in (self.nuisance.white_matter, self.nuisance.csf)
        )
        if segment_masks and self.normalization.mode not in {
            NormalizationMode.T1_SEGMENT,
            NormalizationMode.DARTEL,
        }:
            raise ValueError("Segment tissue masks require T1 segmentation or DARTEL")
        if self.nuisance.enabled:
            compcor_counts = {
                regressor.compcor_components
                for regressor in (self.nuisance.white_matter, self.nuisance.csf)
                if regressor is not None
                and regressor.enabled
                and regressor.method is TissueRegressorMethod.COMPCOR
            }
            if len(compcor_counts) > 1:
                raise ValueError(
                    "DPABI V8.2 uses one shared CompCor component count for WM and CSF"
                )
        if (
            self.smoothing.method is SmoothingMethod.DARTEL
            and self.normalization.mode is not NormalizationMode.DARTEL
        ):
            raise ValueError("DARTEL smoothing requires DARTEL normalization")
        _require_provenance(
            self.provenance,
            {
                "tr_seconds",
                "expected_time_points",
                "dummy_scans",
                "slice_timing",
                "realignment",
                "nuisance",
                "normalization",
                "detrend",
                "temporal_filter",
                "scrubbing",
                "smoothing",
            },
        )
        return self
