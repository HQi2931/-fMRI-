from __future__ import annotations

import pytest
from pydantic import ValidationError

from neuroagent.domain.fmri import (
    AlffFalffParameters,
    FrequencyBand,
    GlobalSignalRegressor,
    MetricKind,
    MetricScaling,
    NormalizationMode,
    NormalizationParameters,
    NormalizationTiming,
    NuisanceRegressionParameters,
    NuisanceTiming,
    RealignmentParameters,
    RehoParameters,
    ScrubbingParameters,
    SliceTimingParameters,
    SmoothingParameters,
    TemporalFilteringParameters,
    TemporalFilterTiming,
    TissueRegressor,
)
from neuroagent.tools import DpabiMetricRequest, DpabiPreprocessingRequest, DpabiV82Adapter
from tests.science.conftest import (
    ALFF_PROVENANCE,
    REHO_PROVENANCE,
    preprocessing_parameters,
    provenance,
)


@pytest.mark.parametrize(
    ("model", "values", "message"),
    [
        (
            SliceTimingParameters,
            {"enabled": False, "slice_count": 1, "slice_order": None, "reference_slice": None},
            "disabled slice timing",
        ),
        (
            SliceTimingParameters,
            {"enabled": True, "slice_count": None, "slice_order": None, "reference_slice": None},
            "requires count",
        ),
        (
            SliceTimingParameters,
            {"enabled": True, "slice_count": 3, "slice_order": (1, 2), "reference_slice": 1},
            "length",
        ),
        (
            SliceTimingParameters,
            {"enabled": True, "slice_count": 3, "slice_order": (1, 2, 3), "reference_slice": 4},
            "occur",
        ),
        (
            RealignmentParameters,
            {"enabled": True, "options_source": None},
            "options source",
        ),
        (
            TissueRegressor,
            {
                "enabled": False,
                "mask_source": "spm",
                "mask_threshold": None,
                "method": None,
                "compcor_components": None,
            },
            "disabled tissue",
        ),
        (
            TissueRegressor,
            {
                "enabled": True,
                "mask_source": None,
                "mask_threshold": None,
                "method": None,
                "compcor_components": None,
            },
            "requires mask",
        ),
        (
            TissueRegressor,
            {
                "enabled": True,
                "mask_source": "spm",
                "mask_threshold": 0.9,
                "method": "compcor",
                "compcor_components": None,
            },
            "component count",
        ),
        (
            TissueRegressor,
            {
                "enabled": True,
                "mask_source": "spm",
                "mask_threshold": 0.9,
                "method": "mean",
                "compcor_components": 2,
            },
            "must not declare",
        ),
        (
            GlobalSignalRegressor,
            {"enabled": True, "mask_source": None, "method": None},
            "requires mask",
        ),
        (
            GlobalSignalRegressor,
            {"enabled": False, "mask_source": "spm", "method": "mean"},
            "must not carry",
        ),
        (
            NuisanceRegressionParameters,
            {
                "enabled": False,
                "timing": "after_realign",
                "polynomial_trend": None,
                "head_motion_model": None,
                "head_motion_scrubbing": None,
                "white_matter": None,
                "csf": None,
                "global_signal": None,
                "warp_masks_to_individual_space": None,
                "add_mean_back": None,
            },
            "disabled nuisance",
        ),
        (
            NuisanceRegressionParameters,
            {
                "enabled": True,
                "timing": "after_realign",
                "polynomial_trend": 0,
                "head_motion_model": 0,
                "head_motion_scrubbing": None,
                "white_matter": None,
                "csf": None,
                "global_signal": None,
                "warp_masks_to_individual_space": False,
                "add_mean_back": True,
            },
            "every declared choice",
        ),
        (
            NormalizationParameters,
            {
                "mode": 0,
                "timing": "on_results",
                "bounding_box_mm": None,
                "voxel_size_mm": None,
                "structural_artifact_id": None,
                "affine_regularization": None,
            },
            "disabled normalization",
        ),
        (
            NormalizationParameters,
            {
                "mode": 1,
                "timing": None,
                "bounding_box_mm": None,
                "voxel_size_mm": None,
                "structural_artifact_id": None,
                "affine_regularization": None,
            },
            "requires timing",
        ),
        (
            NormalizationParameters,
            {
                "mode": 1,
                "timing": "on_results",
                "bounding_box_mm": ((-1.0, -1.0, -1.0), (1.0, 1.0, 1.0)),
                "voxel_size_mm": (1.0, 0.0, 1.0),
                "structural_artifact_id": None,
                "affine_regularization": None,
            },
            "voxel sizes",
        ),
        (
            NormalizationParameters,
            {
                "mode": 1,
                "timing": "on_results",
                "bounding_box_mm": ((1.0, -1.0, -1.0), (1.0, 1.0, 1.0)),
                "voxel_size_mm": (1.0, 1.0, 1.0),
                "structural_artifact_id": None,
                "affine_regularization": None,
            },
            "lower values",
        ),
        (
            NormalizationParameters,
            {
                "mode": 2,
                "timing": "on_results",
                "bounding_box_mm": ((-1.0, -1.0, -1.0), (1.0, 1.0, 1.0)),
                "voxel_size_mm": (1.0, 1.0, 1.0),
                "structural_artifact_id": None,
                "affine_regularization": None,
            },
            "requires a structural",
        ),
        (
            NormalizationParameters,
            {
                "mode": 1,
                "timing": "on_results",
                "bounding_box_mm": ((-1.0, -1.0, -1.0), (1.0, 1.0, 1.0)),
                "voxel_size_mm": (1.0, 1.0, 1.0),
                "structural_artifact_id": "t1",
                "affine_regularization": "mni",
            },
            "must not declare",
        ),
        (
            TemporalFilteringParameters,
            {
                "timing": "disabled",
                "frequency_band": {"low_hz": 0.01, "high_hz": 0.08},
                "add_mean_back": True,
            },
            "disabled temporal",
        ),
        (
            TemporalFilteringParameters,
            {"timing": "after_normalize", "frequency_band": None, "add_mean_back": None},
            "requires a band",
        ),
        (
            ScrubbingParameters,
            {"enabled": True, "timing": None, "censoring": None, "method": None},
            "requires timing",
        ),
        (
            ScrubbingParameters,
            {
                "enabled": False,
                "timing": "after_preprocessing",
                "censoring": None,
                "method": None,
            },
            "must not carry",
        ),
        (
            SmoothingParameters,
            {"timing": "disabled", "method": 1, "fwhm_mm": None},
            "disabled smoothing",
        ),
        (
            SmoothingParameters,
            {"timing": "on_results", "method": None, "fwhm_mm": None},
            "requires method",
        ),
        (
            SmoothingParameters,
            {"timing": "on_results", "method": 1, "fwhm_mm": (1.0, 0.0, 1.0)},
            "must be positive",
        ),
    ],
)
def test_preprocessing_component_models_reject_inconsistent_states(
    model: type[object], values: dict[str, object], message: str
) -> None:
    with pytest.raises(ValidationError, match=message):
        model.model_validate(values)  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    ("mutator", "message"),
    [
        (
            lambda values: values["temporal_filter"]["frequency_band"].update(high_hz=0.3),
            "Nyquist",
        ),
        (lambda values: values.update(detrend=True), "duplicates"),
        (
            lambda values: values["normalization"].update(timing="on_results"),
            "requires normalization on functional",
        ),
        (
            lambda values: values["nuisance"].update(warp_masks_to_individual_space=True),
            "only valid for AfterRealign",
        ),
        (
            lambda values: values["normalization"].update(
                mode=1,
                structural_artifact_id=None,
                affine_regularization=None,
            ),
            "Segment tissue masks require",
        ),
        (
            lambda values: values["smoothing"].update(
                timing="on_results", method=2, fwhm_mm=(4.0, 4.0, 4.0)
            ),
            "DARTEL smoothing requires",
        ),
    ],
)
def test_complete_preprocessing_rejects_cross_step_conflicts(mutator: object, message: str) -> None:
    values = preprocessing_parameters().model_dump(mode="python")
    mutator(values)  # type: ignore[operator]
    with pytest.raises(ValidationError, match=message):
        type(preprocessing_parameters()).model_validate(values)


def _disabled_parameters():
    values = preprocessing_parameters().model_dump(mode="python")
    values.update(
        slice_timing={
            "enabled": False,
            "slice_count": None,
            "slice_order": None,
            "reference_slice": None,
        },
        realignment={"enabled": False, "options_source": None},
        nuisance={
            "enabled": False,
            "timing": None,
            "polynomial_trend": None,
            "head_motion_model": None,
            "head_motion_scrubbing": None,
            "white_matter": None,
            "csf": None,
            "global_signal": None,
            "warp_masks_to_individual_space": None,
            "add_mean_back": None,
        },
        normalization={
            "mode": 0,
            "timing": None,
            "bounding_box_mm": None,
            "voxel_size_mm": None,
            "structural_artifact_id": None,
            "affine_regularization": None,
        },
        temporal_filter={"timing": "disabled", "frequency_band": None, "add_mean_back": None},
        scrubbing={"enabled": False, "timing": None, "censoring": None, "method": None},
    )
    return type(preprocessing_parameters()).model_validate(values)


def _preprocessing_request(parameters=None) -> DpabiPreprocessingRequest:
    return DpabiPreprocessingRequest(
        subject_ids=("sub-01", "sub-02"),
        functional_session_number=1,
        starting_dir_name="FunImg",
        input_manifest_hash="a" * 64,
        parameters=parameters or preprocessing_parameters(),
    )


def _matching_reho(*, frequency_band: FrequencyBand | None = None) -> DpabiMetricRequest:
    reho = RehoParameters(
        tr_seconds=2.0,
        temporal_filter_band=frequency_band or FrequencyBand(low_hz=0.01, high_hz=0.08),
        temporal_filter_add_mean_back=True,
        cluster_voxels=19,
        mask_artifact_id="mask",
        requested_scalings=(MetricScaling.RAW,),
        smooth_reho=False,
        smooth_reho_fwhm_mm=None,
        global_result_smoothing=False,
        global_result_smoothing_fwhm_mm=None,
        provenance=provenance(REHO_PROVENANCE),
    )
    return DpabiMetricRequest(
        subject_ids=("sub-01", "sub-02"),
        functional_session_number=1,
        starting_dir_name="FunImg",
        input_manifest_hash="a" * 64,
        mask_relative_path="input/mask.nii",
        alff_falff=None,
        reho=reho,
    )


def test_adapter_maps_explicitly_disabled_preprocessing() -> None:
    cfg = (
        DpabiV82Adapter()
        .project_preprocessing_cfg(_preprocessing_request(_disabled_parameters()))
        .cfg
    )
    assert cfg["IsSliceTiming"] == 0
    assert "SliceTiming" not in cfg
    assert cfg["IsRealign"] == 0
    assert cfg["IsCovremove"] == 0
    assert cfg["IsNormalize"] == 0
    assert cfg["IsFilter"] == 0
    assert cfg["IsScrubbing"] == 0
    assert cfg["IsSmooth"] == 0


def test_adapter_pipeline_merges_only_matching_frozen_inputs_and_filter_plan() -> None:
    adapter = DpabiV82Adapter()
    preprocessing = _preprocessing_request()
    metric = _matching_reho()
    projection = adapter.project_pipeline_cfg(preprocessing, metric)
    assert projection.cfg["IsCalReHo"] == 1
    assert projection.cfg["IsNormalize"] == 2

    with pytest.raises(ValueError, match="frozen inputs"):
        adapter.project_pipeline_cfg(
            preprocessing,
            metric.model_copy(update={"input_manifest_hash": "b" * 64}),
        )
    with pytest.raises(ValueError, match=r"Filter\.AHighPass"):
        adapter.project_pipeline_cfg(
            preprocessing,
            _matching_reho(frequency_band=FrequencyBand(low_hz=0.02, high_hz=0.08)),
        )

    values = preprocessing_parameters().model_dump(mode="python")
    values["smoothing"] = {
        "timing": "on_results",
        "method": 1,
        "fwhm_mm": (4.0, 4.0, 4.0),
    }
    smoothed = type(preprocessing_parameters()).model_validate(values)
    with pytest.raises(ValueError, match="smoothing must be disabled"):
        adapter.project_pipeline_cfg(_preprocessing_request(smoothed), metric)


def test_adapter_maps_disabled_tissue_and_global_signal_regressors() -> None:
    values = preprocessing_parameters().model_dump(mode="python")
    values["nuisance"].update(
        timing=NuisanceTiming.AFTER_REALIGN,
        white_matter={
            "enabled": False,
            "mask_source": None,
            "mask_threshold": None,
            "method": None,
            "compcor_components": None,
        },
        csf={
            "enabled": False,
            "mask_source": None,
            "mask_threshold": None,
            "method": None,
            "compcor_components": None,
        },
        global_signal={"enabled": False, "mask_source": None, "method": None},
        head_motion_scrubbing=None,
    )
    parameters = type(preprocessing_parameters()).model_validate(values)
    covremove = (
        DpabiV82Adapter()
        .project_preprocessing_cfg(_preprocessing_request(parameters))
        .cfg["Covremove"]
    )
    assert covremove["WM"] == {"IsRemove": 0}
    assert covremove["CSF"] == {"IsRemove": 0}
    assert covremove["WholeBrain"] == {"IsRemove": 0, "IsBothWithWithoutGSR": 0}
    assert "HeadMotionScrubbingRegressors" not in covremove


def test_dpabi_v82_compcor_uses_one_shared_csf_component_field() -> None:
    values = preprocessing_parameters().model_dump(mode="python")
    values["nuisance"]["white_matter"].update(method="compcor", compcor_components=5)
    parameters = type(preprocessing_parameters()).model_validate(values)
    covremove = (
        DpabiV82Adapter()
        .project_preprocessing_cfg(_preprocessing_request(parameters))
        .cfg["Covremove"]
    )
    assert "CompCorPCNum" not in covremove["WM"]
    assert covremove["CSF"]["CompCorPCNum"] == 5

    values["nuisance"]["white_matter"]["compcor_components"] = 3
    with pytest.raises(ValidationError, match="one shared CompCor"):
        type(preprocessing_parameters()).model_validate(values)


def test_epi_normalization_does_not_emit_segmentation_cfg() -> None:
    values = _disabled_parameters().model_dump(mode="python")
    values["normalization"] = NormalizationParameters(
        mode=NormalizationMode.EPI_TEMPLATE,
        timing=NormalizationTiming.ON_RESULTS,
        bounding_box_mm=((-90.0, -126.0, -72.0), (90.0, 90.0, 108.0)),
        voxel_size_mm=(3.0, 3.0, 3.0),
        structural_artifact_id=None,
        affine_regularization=None,
    ).model_dump(mode="python")
    parameters = type(preprocessing_parameters()).model_validate(values)
    cfg = DpabiV82Adapter().project_preprocessing_cfg(_preprocessing_request(parameters)).cfg
    assert cfg["IsNormalize"] == 1
    assert cfg["Normalize"]["Timing"] == "OnResults"
    assert "Segment" not in cfg


def test_metric_isfilter_conflict_is_rejected_before_pipeline_merge() -> None:
    alff = AlffFalffParameters(
        tr_seconds=2.0,
        frequency_band=FrequencyBand(low_hz=0.01, high_hz=0.08),
        requested_metrics=(MetricKind.ALFF,),
        requested_scalings=(MetricScaling.RAW,),
        mask_artifact_id="mask",
        filter_timing=TemporalFilterTiming.DISABLED,
        result_smoothing=False,
        result_smoothing_fwhm_mm=None,
        provenance=provenance(ALFF_PROVENANCE),
    )
    metric = DpabiMetricRequest(
        subject_ids=("sub-01", "sub-02"),
        functional_session_number=1,
        starting_dir_name="FunImg",
        input_manifest_hash="a" * 64,
        mask_relative_path="input/mask.nii",
        alff_falff=alff,
        reho=None,
    )
    with pytest.raises(ValueError, match="disagree on IsFilter"):
        DpabiV82Adapter().project_pipeline_cfg(_preprocessing_request(), metric)


def test_dpabi_requests_reject_duplicate_subjects_and_unresolved_masks() -> None:
    metric_values = _matching_reho().model_dump(mode="python")
    metric_values["subject_ids"] = ()
    with pytest.raises(ValidationError, match="non-empty and unique"):
        DpabiMetricRequest.model_validate(metric_values)

    metric_values = _matching_reho().model_dump(mode="python")
    metric_values["subject_ids"] = ("sub-01", "sub-01")
    with pytest.raises(ValidationError, match="non-empty and unique"):
        DpabiMetricRequest.model_validate(metric_values)

    metric_values = _matching_reho().model_dump(mode="python")
    metric_values["mask_relative_path"] = None
    with pytest.raises(ValidationError, match="valid string"):
        DpabiMetricRequest.model_validate(metric_values)

    alff = AlffFalffParameters(
        tr_seconds=2.0,
        frequency_band=FrequencyBand(low_hz=0.01, high_hz=0.08),
        requested_metrics=(MetricKind.ALFF,),
        requested_scalings=(MetricScaling.RAW,),
        mask_artifact_id="mask",
        filter_timing=TemporalFilterTiming.DISABLED,
        result_smoothing=False,
        result_smoothing_fwhm_mm=None,
        provenance=provenance(ALFF_PROVENANCE),
    )
    metric_values.update(reho=None, alff_falff=alff, subject_ids=("sub-01", "sub-02"))
    with pytest.raises(ValidationError, match="valid string"):
        DpabiMetricRequest.model_validate(metric_values)

    preprocessing_values = _preprocessing_request().model_dump(mode="python")
    preprocessing_values["subject_ids"] = ()
    with pytest.raises(ValidationError, match="non-empty and unique"):
        DpabiPreprocessingRequest.model_validate(preprocessing_values)
    preprocessing_values["subject_ids"] = ("../subject",)
    with pytest.raises(ValidationError, match="unsafe"):
        DpabiPreprocessingRequest.model_validate(preprocessing_values)


def test_combined_metric_request_rejects_fwhm_mismatch_and_double_reho_smoothing() -> None:
    alff = AlffFalffParameters(
        tr_seconds=2.0,
        frequency_band=FrequencyBand(low_hz=0.01, high_hz=0.08),
        requested_metrics=(MetricKind.ALFF,),
        requested_scalings=(MetricScaling.RAW,),
        mask_artifact_id="mask",
        filter_timing=TemporalFilterTiming.AFTER_NORMALIZE,
        result_smoothing=True,
        result_smoothing_fwhm_mm=(4.0, 4.0, 4.0),
        provenance=provenance(ALFF_PROVENANCE),
    )
    reho = _matching_reho().reho
    assert reho is not None
    reho = reho.model_copy(
        update={
            "global_result_smoothing": True,
            "global_result_smoothing_fwhm_mm": (6.0, 6.0, 6.0),
        }
    )
    base = _matching_reho().model_dump(mode="python")
    base.update(alff_falff=alff, reho=reho)
    with pytest.raises(ValidationError, match="one global result-smoothing FWHM"):
        DpabiMetricRequest.model_validate(base)

    base["reho"] = reho.model_copy(
        update={
            "mask_artifact_id": "mask",
            "smooth_reho": True,
            "smooth_reho_fwhm_mm": (3.0, 3.0, 3.0),
            "global_result_smoothing_fwhm_mm": (4.0, 4.0, 4.0),
        }
    )
    base["mask_relative_path"] = "input/mask.nii"
    with pytest.raises(ValidationError, match="smooth ReHo twice"):
        DpabiMetricRequest.model_validate(base)


def test_pipeline_without_metrics_returns_common_projection() -> None:
    adapter = DpabiV82Adapter()
    request = _preprocessing_request()
    assert adapter.project_pipeline_cfg(request, None) == adapter.project_preprocessing_cfg(request)
