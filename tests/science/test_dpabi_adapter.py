from __future__ import annotations

import pytest
from pydantic import ValidationError

from neuroagent.domain.fmri import (
    AlffFalffParameters,
    AnalysisImage,
    FdrCorrection,
    FrequencyBand,
    GrfCorrection,
    MetricKind,
    MetricScaling,
    MissingValuePolicy,
    RehoParameters,
    StatisticalDesignRevision,
    StatisticalMapType,
    StatisticalTest,
    Tail,
    TemporalFilterTiming,
)
from neuroagent.tools import DpabiMetricRequest, DpabiV82Adapter
from tests.science.conftest import ALFF_PROVENANCE, REHO_PROVENANCE, provenance


def test_dpabi_v82_metric_field_mapping_snapshot() -> None:
    alff = AlffFalffParameters(
        tr_seconds=2.0,
        frequency_band=FrequencyBand(low_hz=0.01, high_hz=0.08),
        requested_metrics=(MetricKind.FALFF,),
        requested_scalings=(MetricScaling.Z_SCORE,),
        mask_artifact_id="mask-001",
        filter_timing=TemporalFilterTiming.AFTER_NORMALIZE,
        result_smoothing=True,
        result_smoothing_fwhm_mm=(6.0, 6.0, 6.0),
        provenance=provenance(ALFF_PROVENANCE),
    )
    reho = RehoParameters(
        tr_seconds=2.0,
        temporal_filter_band=FrequencyBand(low_hz=0.01, high_hz=0.08),
        temporal_filter_add_mean_back=True,
        cluster_voxels=19,
        mask_artifact_id="mask-001",
        requested_scalings=(MetricScaling.Z_SCORE,),
        smooth_reho=False,
        smooth_reho_fwhm_mm=None,
        global_result_smoothing=True,
        global_result_smoothing_fwhm_mm=(6.0, 6.0, 6.0),
        provenance=provenance(REHO_PROVENANCE),
    )
    projection = DpabiV82Adapter().project_metric_cfg(
        DpabiMetricRequest(
            subject_ids=("sub-01", "sub-02"),
            functional_session_number=1,
            starting_dir_name="FunImgARCW",
            input_manifest_hash="a" * 64,
            mask_relative_path="input/mask.nii",
            alff_falff=alff,
            reho=reho,
        )
    )
    assert projection.cfg == {
        "SubjectID": ["sub-01", "sub-02"],
        "SubjectNum": 2,
        "FunctionalSessionNumber": 1,
        "StartingDirName": "FunImgARCW",
        "IsCalALFF": 1,
        "IsCalReHo": 1,
        "TR": 2.0,
        "MaskFile": "input/mask.nii",
        "CalALFF": {
            "AHighPass_LowCutoff": 0.01,
            "ALowPass_HighCutoff": 0.08,
        },
        "CalReHo": {"ClusterNVoxel": 19, "SmoothReHo": 0},
        "IsFilter": 1,
        "Filter": {
            "Timing": "AfterNormalize",
            "AHighPass_LowCutoff": 0.01,
            "ALowPass_HighCutoff": 0.08,
            "AAddMeanBack": 1,
        },
        "IsSmooth": 1,
        "Smooth": {"Timing": "OnResults", "FWHM": [6.0, 6.0, 6.0]},
    }


def test_statistics_and_fdr_map_to_confirmed_functions() -> None:
    design = StatisticalDesignRevision(
        revision_id="stats-1",
        test=StatisticalTest.INDEPENDENT_TWO_SAMPLE_T,
        subject_order=("sub-01", "sub-02", "sub-03", "sub-04"),
        images=(
            AnalysisImage(subject_id="sub-01", artifact_id="a1", group="case", condition=None),
            AnalysisImage(subject_id="sub-02", artifact_id="a2", group="control", condition=None),
            AnalysisImage(subject_id="sub-03", artifact_id="a3", group="case", condition=None),
            AnalysisImage(subject_id="sub-04", artifact_id="a4", group="control", condition=None),
        ),
        group_order=("case", "control"),
        condition_order=(),
        covariates=(),
        contrast=(1.0, 0.0),
        one_sample_baseline=None,
        mask_artifact_id="mask-001",
        tail=Tail.TWO_SIDED,
        missing_value_policy=MissingValuePolicy.ERROR,
        qc_review_revision_id="qc-1",
        qc_review_hash="q" * 64,
    )
    adapter = DpabiV82Adapter()
    call = adapter.project_statistics(design)
    assert call.function.value == "y_TTest2_Image"
    assert call.dependent_artifact_groups == (("a1", "a3"), ("a2", "a4"))
    correction = adapter.project_correction(
        FdrCorrection(
            method="fdr",
            q_threshold=0.05,
            mask_artifact_id="mask-001",
            statistic_type=StatisticalMapType.T,
            df1=2,
            df2=None,
        )
    )
    assert correction.function.value == "y_FDR_Image"
    assert correction.parameters["q_threshold"] == 0.05


def test_dpabi_request_rejects_unsafe_paths_subjects_and_inconsistent_smoothing() -> None:
    alff_values = {
        "tr_seconds": 2.0,
        "frequency_band": {"low_hz": 0.01, "high_hz": 0.08},
        "requested_metrics": ("falff",),
        "requested_scalings": ("raw",),
        "mask_artifact_id": "mask",
        "filter_timing": "after_normalize",
        "result_smoothing": False,
        "result_smoothing_fwhm_mm": None,
        "provenance": provenance(ALFF_PROVENANCE),
    }
    reho_values = {
        "tr_seconds": 2.0,
        "temporal_filter_band": None,
        "temporal_filter_add_mean_back": None,
        "cluster_voxels": 7,
        "mask_artifact_id": "mask",
        "requested_scalings": ("raw",),
        "smooth_reho": False,
        "smooth_reho_fwhm_mm": None,
        "global_result_smoothing": True,
        "global_result_smoothing_fwhm_mm": (4.0, 4.0, 4.0),
        "provenance": provenance(REHO_PROVENANCE),
    }
    base = {
        "subject_ids": ("sub-01",),
        "functional_session_number": 1,
        "starting_dir_name": "FunImg",
        "input_manifest_hash": "a" * 64,
        "mask_relative_path": "input/mask.nii",
        "alff_falff": alff_values,
        "reho": reho_values,
    }
    with pytest.raises(ValidationError, match="consistent"):
        DpabiMetricRequest.model_validate(base)
    with pytest.raises(ValidationError, match="staging"):
        DpabiMetricRequest.model_validate(
            {**base, "reho": None, "mask_relative_path": "../../raw/mask.nii"}
        )
    with pytest.raises(ValidationError, match="unsafe"):
        DpabiMetricRequest.model_validate(
            {**base, "reho": None, "subject_ids": ("sub-01\nsub-02",)}
        )
    with pytest.raises(ValidationError, match="at least one"):
        DpabiMetricRequest.model_validate({**base, "alff_falff": None, "reho": None})


def test_adapter_rejects_incompatible_filter_order_and_mixed_tr() -> None:
    alff = AlffFalffParameters(
        tr_seconds=2.0,
        frequency_band=FrequencyBand(low_hz=0.01, high_hz=0.08),
        requested_metrics=(MetricKind.FALFF,),
        requested_scalings=(MetricScaling.RAW,),
        mask_artifact_id="mask",
        filter_timing=TemporalFilterTiming.BEFORE_NORMALIZE,
        result_smoothing=False,
        result_smoothing_fwhm_mm=None,
        provenance=provenance(ALFF_PROVENANCE),
    )
    adapter = DpabiV82Adapter()
    with pytest.raises(ValueError, match="incompatible with standard fALFF"):
        adapter.project_metric_cfg(
            DpabiMetricRequest(
                subject_ids=("sub-01",),
                functional_session_number=1,
                starting_dir_name="FunImg",
                input_manifest_hash="a" * 64,
                mask_relative_path="input/mask.nii",
                alff_falff=alff,
                reho=None,
            )
        )

    reho = RehoParameters(
        tr_seconds=3.0,
        temporal_filter_band=FrequencyBand(low_hz=0.01, high_hz=0.08),
        temporal_filter_add_mean_back=True,
        cluster_voxels=27,
        mask_artifact_id="mask",
        requested_scalings=(MetricScaling.RAW,),
        smooth_reho=False,
        smooth_reho_fwhm_mm=None,
        global_result_smoothing=False,
        global_result_smoothing_fwhm_mm=None,
        provenance=provenance(REHO_PROVENANCE),
    )
    alff_values = alff.model_copy(update={"filter_timing": TemporalFilterTiming.AFTER_NORMALIZE})
    with pytest.raises(ValueError, match="one explicit TR"):
        adapter.project_metric_cfg(
            DpabiMetricRequest(
                subject_ids=("sub-01",),
                functional_session_number=1,
                starting_dir_name="FunImg",
                input_manifest_hash="a" * 64,
                mask_relative_path="input/mask.nii",
                alff_falff=alff_values,
                reho=reho,
            )
        )


def test_alff_only_and_smooth_reho_cfg_branches() -> None:
    alff = AlffFalffParameters(
        tr_seconds=2.0,
        frequency_band=FrequencyBand(low_hz=0.01, high_hz=0.08),
        requested_metrics=(MetricKind.ALFF,),
        requested_scalings=(MetricScaling.RAW,),
        mask_artifact_id="mask",
        filter_timing=TemporalFilterTiming.DISABLED,
        result_smoothing=True,
        result_smoothing_fwhm_mm=(5.0, 5.0, 5.0),
        provenance=provenance(ALFF_PROVENANCE),
    )
    adapter = DpabiV82Adapter()
    alff_cfg = adapter.project_metric_cfg(
        DpabiMetricRequest(
            subject_ids=("sub-01",),
            functional_session_number=1,
            starting_dir_name="FunImg",
            input_manifest_hash="a" * 64,
            mask_relative_path="input/mask.nii",
            alff_falff=alff,
            reho=None,
        )
    ).cfg
    assert alff_cfg["Smooth"] == {"Timing": "OnResults", "FWHM": [5.0, 5.0, 5.0]}
    assert alff_cfg["IsFilter"] == 0
    assert alff_cfg["MaskFile"] == "input/mask.nii"

    reho = RehoParameters(
        tr_seconds=2.0,
        temporal_filter_band=None,
        temporal_filter_add_mean_back=None,
        cluster_voxels=7,
        mask_artifact_id="mask",
        requested_scalings=(MetricScaling.RAW,),
        smooth_reho=True,
        smooth_reho_fwhm_mm=(4.0, 4.0, 4.0),
        global_result_smoothing=False,
        global_result_smoothing_fwhm_mm=None,
        provenance=provenance(REHO_PROVENANCE),
    )
    reho_cfg = adapter.project_metric_cfg(
        DpabiMetricRequest(
            subject_ids=("sub-01",),
            functional_session_number=1,
            starting_dir_name="FunImg",
            input_manifest_hash="a" * 64,
            mask_relative_path="input/mask.nii",
            alff_falff=None,
            reho=reho,
        )
    ).cfg
    assert reho_cfg["IsFilter"] == 0
    assert reho_cfg["IsSmooth"] == 0
    assert reho_cfg["Smooth"]["FWHM"] == [4.0, 4.0, 4.0]


def test_adapter_maps_paired_group_analysis_and_grf() -> None:
    subjects = ("s1", "s2")
    paired = StatisticalDesignRevision(
        revision_id="paired",
        test=StatisticalTest.PAIRED_T,
        subject_order=subjects,
        images=tuple(
            AnalysisImage(
                subject_id=subject,
                artifact_id=f"{condition}-{subject}",
                group=None,
                condition=condition,
            )
            for condition in ("pre", "post")
            for subject in subjects
        ),
        group_order=(),
        condition_order=("pre", "post"),
        covariates=(),
        contrast=(1.0, 0.0, 0.0),
        one_sample_baseline=None,
        mask_artifact_id="mask",
        tail=Tail.TWO_SIDED,
        missing_value_policy=MissingValuePolicy.ERROR,
        qc_review_revision_id="qc",
        qc_review_hash="q" * 64,
    )
    adapter = DpabiV82Adapter()
    paired_call = adapter.project_statistics(paired)
    assert paired_call.function.value == "y_TTestPaired_Image"
    assert paired_call.dependent_artifact_groups[0] == ("pre-s1", "pre-s2")

    regression_values = paired.model_dump()
    regression_values.update(
        test="regression",
        subject_order=("s1", "s2", "s3"),
        images=(
            {"subject_id": "s1", "artifact_id": "a1", "group": None, "condition": None},
            {"subject_id": "s2", "artifact_id": "a2", "group": None, "condition": None},
            {"subject_id": "s3", "artifact_id": "a3", "group": None, "condition": None},
        ),
        group_order=(),
        condition_order=(),
        covariates=(
            {
                "name": "age",
                "values": (
                    {"subject_id": "s1", "value": 20.0},
                    {"subject_id": "s2", "value": 30.0},
                    {"subject_id": "s3", "value": 40.0},
                ),
                "centering": "none",
            },
        ),
        contrast=(0.0, 1.0),
    )
    regression = StatisticalDesignRevision.model_validate(regression_values)
    assert adapter.project_statistics(regression).function.value == "y_GroupAnalysis_Image"

    grf = adapter.project_correction(
        GrfCorrection(
            method="grf",
            voxel_p_threshold=0.001,
            cluster_p_threshold=0.05,
            two_tailed=True,
            mask_artifact_id="mask",
            statistic_type=StatisticalMapType.T,
            df1=10,
            df2=None,
            smoothness_mode="provided_dlh",
            smoothness_dlh=0.01,
        )
    )
    assert grf.function.value == "y_GRF_Threshold"
    assert grf.parameters["smoothness_dlh"] == 0.01


def test_f_and_grf_corrections_require_second_df() -> None:
    with pytest.raises(ValidationError, match="df2"):
        FdrCorrection(
            method="fdr",
            q_threshold=0.05,
            mask_artifact_id="mask",
            statistic_type=StatisticalMapType.F,
            df1=2,
            df2=None,
        )
    with pytest.raises(ValidationError, match="df2"):
        GrfCorrection(
            method="grf",
            voxel_p_threshold=0.001,
            cluster_p_threshold=0.05,
            two_tailed=True,
            mask_artifact_id="mask",
            statistic_type=StatisticalMapType.F,
            df1=2,
            df2=None,
            smoothness_mode="dpabi_header_or_estimate",
            smoothness_dlh=None,
        )
