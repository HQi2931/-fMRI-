from __future__ import annotations

from collections.abc import Iterable

import pytest

from neuroagent.domain.fmri import (
    AffineRegularization,
    ArtifactKind,
    ArtifactLineage,
    FdType,
    FrequencyBand,
    GlobalSignalMaskSource,
    GlobalSignalMethod,
    GlobalSignalRegressor,
    HeadMotionModel,
    MetricScaling,
    MotionCensoringParameters,
    NormalizationMode,
    NormalizationParameters,
    NormalizationTiming,
    NuisanceRegressionParameters,
    NuisanceTiming,
    ParameterProvenance,
    ParameterSource,
    PreprocessingParameters,
    RealignmentOptionsSource,
    RealignmentParameters,
    ScrubbingMethod,
    ScrubbingParameters,
    ScrubbingTiming,
    SliceTimingParameters,
    SmoothingParameters,
    SmoothingTiming,
    TemporalFilteringParameters,
    TemporalFilterTiming,
    TissueMaskSource,
    TissueRegressor,
    TissueRegressorMethod,
)


@pytest.fixture
def base_lineage() -> ArtifactLineage:
    return ArtifactLineage(
        artifact_id="functional-001",
        kind=ArtifactKind.FUNCTIONAL_TIMESERIES,
        metadata_verified=True,
        tr_seconds=2.0,
        volume_count=230,
        metadata_evidence_hash="c" * 64,
        subject_manifest_hash="a" * 64,
        space="MNI152",
        grid_signature="grid-3mm",
        voxel_size_mm=(3.0, 3.0, 3.0),
        mask_artifact_id="mask-001",
        mask_grid_signature="grid-3mm",
        temporally_filtered=False,
        frequency_band=None,
        spatially_smoothed=False,
        smoothing_fwhm_mm=None,
        scrubbed=False,
        producer_step_hash="b" * 64,
    )


def provenance(names: Iterable[str]) -> tuple[ParameterProvenance, ...]:
    return tuple(
        ParameterProvenance(
            name=name,
            source=ParameterSource.STUDY_PROTOCOL,
            evidence_ref=f"protocol://{name}",
        )
        for name in names
    )


ALFF_PROVENANCE = (
    "tr_seconds",
    "frequency_band",
    "requested_metrics",
    "requested_scalings",
    "filter_timing",
    "result_smoothing",
)

REHO_PROVENANCE = (
    "tr_seconds",
    "temporal_filter_band",
    "temporal_filter_add_mean_back",
    "cluster_voxels",
    "requested_scalings",
    "smooth_reho",
    "global_result_smoothing",
)

PREPROCESSING_PROVENANCE = (
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
)


def preprocessing_parameters() -> PreprocessingParameters:
    return PreprocessingParameters(
        tr_seconds=2.0,
        expected_time_points=240,
        dummy_scans=10,
        slice_timing=SliceTimingParameters(
            enabled=True,
            slice_count=4,
            slice_order=(1, 3, 2, 4),
            reference_slice=2,
        ),
        realignment=RealignmentParameters(
            enabled=True,
            options_source=RealignmentOptionsSource.DPABI_V82_JOBMAT,
        ),
        nuisance=NuisanceRegressionParameters(
            enabled=True,
            timing=NuisanceTiming.AFTER_NORMALIZE,
            polynomial_trend=1,
            head_motion_model=HeadMotionModel.FRISTON_24,
            head_motion_scrubbing=MotionCensoringParameters(
                fd_type=FdType.POWER,
                fd_threshold_mm=0.5,
                previous_points=1,
                later_points=2,
            ),
            white_matter=TissueRegressor(
                enabled=True,
                mask_source=TissueMaskSource.SPM,
                mask_threshold=0.99,
                method=TissueRegressorMethod.MEAN,
                compcor_components=None,
            ),
            csf=TissueRegressor(
                enabled=True,
                mask_source=TissueMaskSource.SEGMENT,
                mask_threshold=0.95,
                method=TissueRegressorMethod.COMPCOR,
                compcor_components=5,
            ),
            global_signal=GlobalSignalRegressor(
                enabled=True,
                mask_source=GlobalSignalMaskSource.AUTO_MASK,
                method=GlobalSignalMethod.MEAN,
            ),
            warp_masks_to_individual_space=False,
            add_mean_back=True,
        ),
        normalization=NormalizationParameters(
            mode=NormalizationMode.T1_SEGMENT,
            timing=NormalizationTiming.ON_FUNCTIONAL_DATA,
            bounding_box_mm=((-90.0, -126.0, -72.0), (90.0, 90.0, 108.0)),
            voxel_size_mm=(3.0, 3.0, 3.0),
            structural_artifact_id="t1-001",
            affine_regularization=AffineRegularization.MNI,
        ),
        detrend=False,
        temporal_filter=TemporalFilteringParameters(
            timing=TemporalFilterTiming.AFTER_NORMALIZE,
            frequency_band=FrequencyBand(low_hz=0.01, high_hz=0.08),
            add_mean_back=True,
        ),
        scrubbing=ScrubbingParameters(
            enabled=True,
            timing=ScrubbingTiming.AFTER_PREPROCESSING,
            censoring=MotionCensoringParameters(
                fd_type=FdType.JENKINSON,
                fd_threshold_mm=0.2,
                previous_points=1,
                later_points=2,
            ),
            method=ScrubbingMethod.CUT,
        ),
        smoothing=SmoothingParameters(
            timing=SmoothingTiming.DISABLED,
            method=None,
            fwhm_mm=None,
        ),
        provenance=provenance(PREPROCESSING_PROVENANCE),
    )


RAW_SCALING = (MetricScaling.RAW,)
