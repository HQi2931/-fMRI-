from __future__ import annotations

import pytest

from neuroagent.domain.fmri import (
    AlffFalffParameters,
    ArtifactLineage,
    FrequencyBand,
    MetricKind,
    MetricScaling,
    RehoParameters,
    TemporalFilterTiming,
)
from neuroagent.domain.fmri.preprocessing import PreprocessingParameters
from neuroagent.domain.fmri.skillpacks.builtin import build_builtin_registry
from neuroagent.skills import (
    EnvironmentSnapshot,
    SkillCompileError,
    SkillCompiler,
    SkillRequest,
    SkillResolver,
    SkillValidator,
)
from neuroagent.tools import build_default_tool_registry
from tests.science.conftest import (
    ALFF_PROVENANCE,
    REHO_PROVENANCE,
    preprocessing_parameters,
    provenance,
)


def environment() -> EnvironmentSnapshot:
    """The locally verified scientific stack, so resolution stays warning-free."""

    return EnvironmentSnapshot(
        matlab_version="R2024b",
        spm_version="SPM25",
        dpabi_version="V9.0_250415",
        adapter_version="1.0.0",
        environment_hash="e" * 64,
    )


def request(lineage: ArtifactLineage) -> SkillRequest:
    return SkillRequest(
        project_id="project-1",
        dataset_ref="dataset-1",
        input_manifest_hash="a" * 64,
        requested_metrics=(MetricKind.ALFF, MetricKind.FALFF, MetricKind.REHO),
        primary_outputs=("zALFF", "zfALFF", "zReHo"),
        input_artifact=lineage,
        alff_falff=AlffFalffParameters(
            tr_seconds=2.0,
            frequency_band=FrequencyBand(low_hz=0.01, high_hz=0.08),
            requested_metrics=(MetricKind.ALFF, MetricKind.FALFF),
            requested_scalings=(MetricScaling.Z_SCORE,),
            mask_artifact_id="mask-001",
            filter_timing=TemporalFilterTiming.AFTER_NORMALIZE,
            result_smoothing=False,
            result_smoothing_fwhm_mm=None,
            provenance=provenance(ALFF_PROVENANCE),
        ),
        reho=RehoParameters(
            tr_seconds=2.0,
            temporal_filter_band=FrequencyBand(low_hz=0.01, high_hz=0.08),
            temporal_filter_add_mean_back=True,
            cluster_voxels=27,
            mask_artifact_id="mask-001",
            requested_scalings=(MetricScaling.Z_SCORE,),
            smooth_reho=False,
            smooth_reho_fwhm_mm=None,
            global_result_smoothing=False,
            global_result_smoothing_fwhm_mm=None,
            provenance=provenance(REHO_PROVENANCE),
        ),
    )


def compile_plan(lineage: ArtifactLineage):  # type: ignore[no-untyped-def]
    resolver = SkillResolver(build_builtin_registry())
    resolution = resolver.resolve(request(lineage), environment())
    compiler = SkillCompiler(build_default_tool_registry(), SkillValidator())
    return compiler.compile(resolution)


def _skill_request_with(base_request: SkillRequest, **updates: object) -> SkillRequest:
    return SkillRequest.model_validate({**base_request.model_dump(mode="python"), **updates})


def test_combined_plan_has_typed_checkpoint_order_and_stable_hash(
    base_lineage: ArtifactLineage,
) -> None:
    first = compile_plan(base_lineage)
    second = compile_plan(base_lineage)
    assert first == second
    assert first.plan_hash == second.plan_hash
    step_ids = tuple(step.step_id for step in first.steps)
    assert step_ids.index("calculate_alff_falff") < step_ids.index("calculate_reho")
    prepare_reho = next(step for step in first.steps if step.step_id == "prepare_reho_timeseries")
    assert "calculate_alff_falff" in prepare_reho.needs
    assert "reho.temporal_filter_add_mean_back" in prepare_reho.parameter_names
    assert "preprocessing.scrubbing" in prepare_reho.parameter_names
    reho_step = next(step for step in first.steps if step.step_id == "calculate_reho")
    assert reho_step.consumes == ("timeseries.reho_ready.verified.unsmoothed",)
    assert "metric.alff" in first.artifact_expectations
    assert "metric.falff" in first.artifact_expectations
    assert first.approval_binding() == (first.plan_hash, "a" * 64, "e" * 64)


def test_blocking_issue_cannot_be_compiled(base_lineage: ArtifactLineage) -> None:
    smoothed = base_lineage.model_copy(
        update={"spatially_smoothed": True, "smoothing_fwhm_mm": (6.0, 6.0, 6.0)}
    )
    with pytest.raises(SkillCompileError, match="REHO_INPUT_SPATIALLY_SMOOTHED"):
        compile_plan(smoothed)


def test_pure_preprocessing_request_compiles_and_binds_base_cfg(
    base_lineage: ArtifactLineage,
) -> None:
    preprocessing_request = SkillRequest(
        project_id="project-1",
        dataset_ref="dataset-1",
        input_manifest_hash="a" * 64,
        requested_metrics=(),
        primary_outputs=("timeseries.preprocessed",),
        input_artifact=base_lineage,
        alff_falff=None,
        reho=None,
        request_preprocessing=True,
        preprocessing=preprocessing_parameters(),
        base_cfg_artifact_id="cfg-base-001",
    )
    resolution = SkillResolver(build_builtin_registry()).resolve(
        preprocessing_request, environment()
    )
    assert tuple(spec.skill_id for spec in resolution.selected_specs) == (
        "rsfmri.preprocess.common",
    )
    plan = SkillCompiler(build_default_tool_registry(), SkillValidator()).compile(resolution)
    assert plan.base_cfg_artifact_id == "cfg-base-001"
    assert plan.preprocessing_parameters_hash is not None
    assert dict(plan.resolved_parameters)["preprocessing"]["dummy_scans"] == 10
    assert dict(plan.resolved_parameters)["base_cfg_artifact_id"] == "cfg-base-001"


def test_preprocessing_and_metrics_are_connected_by_artifact_contract(
    base_lineage: ArtifactLineage,
) -> None:
    values = request(base_lineage).model_dump()
    preprocessing = preprocessing_parameters().model_dump(mode="json")
    preprocessing["scrubbing"]["method"] = "nearest"
    values.update(
        request_preprocessing=True,
        preprocessing=preprocessing,
        base_cfg_artifact_id="cfg-base-001",
    )
    requested = SkillRequest.model_validate(values)
    resolution = SkillResolver(build_builtin_registry()).resolve(requested, environment())
    assert tuple(spec.skill_id for spec in resolution.selected_specs) == (
        "rsfmri.preprocess.common",
        "rsfmri.pipeline.alff_reho_combined",
    )
    plan = SkillCompiler(build_default_tool_registry(), SkillValidator()).compile(resolution)
    alff = next(step for step in plan.steps if step.step_id == "calculate_alff_falff")
    prepare_reho = next(step for step in plan.steps if step.step_id == "prepare_reho_timeseries")
    assert "verify_preprocessed_metadata" in alff.needs
    assert "verify_preprocessed_metadata" in prepare_reho.needs
    verify = next(step for step in plan.steps if step.step_id == "verify_preprocessed_metadata")
    assert verify.needs == ("preprocess_common",)
    assert verify.produces == ("timeseries.verified.unfiltered.unsmoothed",)


def test_same_dag_reho_blocks_when_cut_scrubbing_hides_retained_volume_count(
    base_lineage: ArtifactLineage,
) -> None:
    values = request(base_lineage).model_dump()
    values.update(
        request_preprocessing=True,
        preprocessing=preprocessing_parameters().model_dump(mode="json"),
        base_cfg_artifact_id="cfg-base-001",
    )
    requested = SkillRequest.model_validate(values)
    resolution = SkillResolver(build_builtin_registry()).resolve(requested, environment())
    with pytest.raises(SkillCompileError, match="EFFECTIVE_VOLUME_COUNT_UNKNOWN"):
        SkillCompiler(build_default_tool_registry(), SkillValidator()).compile(resolution)


def test_same_dag_reho_rejects_before_normalize_prefiltering(
    base_lineage: ArtifactLineage,
) -> None:
    values = request(base_lineage).model_dump()
    preprocessing = preprocessing_parameters().model_dump(mode="json")
    preprocessing["temporal_filter"]["timing"] = TemporalFilterTiming.BEFORE_NORMALIZE
    preprocessing["scrubbing"]["method"] = "nearest"
    values.update(
        requested_metrics=(MetricKind.REHO,),
        primary_outputs=("zReHo",),
        alff_falff=None,
        request_preprocessing=True,
        preprocessing=preprocessing,
        base_cfg_artifact_id="cfg-base-001",
    )
    requested = SkillRequest.model_validate(values)
    resolution = SkillResolver(build_builtin_registry()).resolve(requested, environment())

    with pytest.raises(SkillCompileError, match="REHO_PREPROCESSING_PREFILTERED_INPUT"):
        SkillCompiler(build_default_tool_registry(), SkillValidator()).compile(resolution)


def test_result_normalization_metric_capability_gate_has_exact_scope(
    base_lineage: ArtifactLineage,
) -> None:
    base_request = request(base_lineage)
    preprocessing_values = preprocessing_parameters().model_dump(mode="json")
    preprocessing_values["scrubbing"]["method"] = "nearest"
    preprocessing_values["nuisance"]["timing"] = "after_realign"
    preprocessing_values["normalization"]["timing"] = "on_results"
    on_results = PreprocessingParameters.model_validate(preprocessing_values)
    assert base_request.alff_falff is not None
    single_alff = base_request.alff_falff.model_copy(
        update={"requested_metrics": (MetricKind.ALFF,)}
    )
    metric_request = _skill_request_with(
        base_request,
        requested_metrics=(MetricKind.ALFF,),
        primary_outputs=("zALFF",),
        alff_falff=single_alff,
        reho=None,
        request_preprocessing=True,
        preprocessing=on_results,
        base_cfg_artifact_id="cfg-base-001",
    )
    resolution = SkillResolver(build_builtin_registry()).resolve(metric_request, environment())

    report = SkillValidator().validate_resolution(resolution)
    issue = next(
        item
        for item in report.issues
        if item.code == "RESULT_NORMALIZATION_METRIC_MASK_SPACES_UNSUPPORTED"
    )
    assert issue.severity.value == "blocking"
    assert issue.path == "preprocessing.normalization.timing"
    assert issue.remediation is not None
    assert "不判断真实图像或掩膜空间" in issue.message


def test_result_normalization_metric_gate_does_not_expand_to_neighboring_paths(
    base_lineage: ArtifactLineage,
) -> None:
    base_request = request(base_lineage)
    validator = SkillValidator()

    disabled_values = preprocessing_parameters().model_dump(mode="json")
    disabled_values["normalization"] = {
        "mode": 0,
        "timing": None,
        "bounding_box_mm": None,
        "voxel_size_mm": None,
        "structural_artifact_id": None,
        "affine_regularization": None,
    }
    disabled_values["nuisance"] = {
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
    }
    disabled_preprocessing = PreprocessingParameters.model_validate(disabled_values)
    assert base_request.alff_falff is not None
    alff = base_request.alff_falff.model_copy(update={"requested_metrics": (MetricKind.ALFF,)})
    disabled_request = _skill_request_with(
        base_request,
        requested_metrics=(MetricKind.ALFF,),
        primary_outputs=("zALFF",),
        alff_falff=alff,
        reho=None,
        request_preprocessing=True,
        preprocessing=disabled_preprocessing,
        base_cfg_artifact_id="cfg-base-001",
    )
    disabled_resolution = SkillResolver(build_builtin_registry()).resolve(
        disabled_request, environment()
    )
    assert "RESULT_NORMALIZATION_METRIC_MASK_SPACES_UNSUPPORTED" not in {
        item.code for item in validator.validate_resolution(disabled_resolution).issues
    }

    preprocessing_values = preprocessing_parameters().model_dump(mode="json")
    preprocessing_values["scrubbing"]["method"] = "nearest"
    preprocessing_values["nuisance"]["timing"] = "after_realign"
    preprocessing_values["normalization"]["timing"] = "on_results"
    on_results = PreprocessingParameters.model_validate(preprocessing_values)
    preprocessing_only = _skill_request_with(
        base_request,
        requested_metrics=(),
        primary_outputs=("timeseries.preprocessed",),
        alff_falff=None,
        reho=None,
        request_preprocessing=True,
        preprocessing=on_results,
        base_cfg_artifact_id="cfg-base-001",
    )
    preprocessing_resolution = SkillResolver(build_builtin_registry()).resolve(
        preprocessing_only, environment()
    )
    assert "RESULT_NORMALIZATION_METRIC_MASK_SPACES_UNSUPPORTED" not in {
        item.code for item in validator.validate_resolution(preprocessing_resolution).issues
    }

    metric_only = _skill_request_with(
        base_request,
        requested_metrics=(MetricKind.ALFF,),
        primary_outputs=("zALFF",),
        alff_falff=alff,
        reho=None,
        request_preprocessing=False,
        preprocessing=None,
        base_cfg_artifact_id=None,
    )
    metric_only_resolution = SkillResolver(build_builtin_registry()).resolve(
        metric_only, environment()
    )
    assert "RESULT_NORMALIZATION_METRIC_MASK_SPACES_UNSUPPORTED" not in {
        item.code for item in validator.validate_resolution(metric_only_resolution).issues
    }


def test_result_stage_without_metric_gate_reports_each_on_results_field(
    base_lineage: ArtifactLineage,
) -> None:
    base_request = request(base_lineage)
    validator = SkillValidator()

    def preprocessing_with_result_stages(
        *, normalization: bool = False, smoothing: bool = False
    ) -> PreprocessingParameters:
        values = preprocessing_parameters().model_dump(mode="json")
        values["scrubbing"]["method"] = "nearest"
        if normalization:
            values["nuisance"]["timing"] = "after_realign"
            values["normalization"]["timing"] = "on_results"
        if smoothing:
            values["smoothing"] = {
                "timing": "on_results",
                "method": 1,
                "fwhm_mm": [6.0, 6.0, 6.0],
            }
        return PreprocessingParameters.model_validate(values)

    expected_paths = {
        (True, False): {"preprocessing.normalization.timing"},
        (False, True): {"preprocessing.smoothing.timing"},
        (True, True): {
            "preprocessing.normalization.timing",
            "preprocessing.smoothing.timing",
        },
    }
    for (normalization, smoothing), paths in expected_paths.items():
        request_without_metrics = _skill_request_with(
            base_request,
            requested_metrics=(),
            primary_outputs=("timeseries.preprocessed",),
            alff_falff=None,
            reho=None,
            request_preprocessing=True,
            preprocessing=preprocessing_with_result_stages(
                normalization=normalization,
                smoothing=smoothing,
            ),
            base_cfg_artifact_id="cfg-base-001",
        )
        resolution = SkillResolver(build_builtin_registry()).resolve(
            request_without_metrics, environment()
        )
        report = validator.validate_resolution(resolution)
        blockers = [
            item
            for item in report.issues
            if item.code == "RESULT_STAGE_OPERATION_WITHOUT_METRIC_UNSUPPORTED"
        ]
        assert {item.path for item in blockers} == paths
        assert all(item.severity.value == "blocking" for item in blockers)
        assert all(item.remediation for item in blockers)

    non_result_stage_request = _skill_request_with(
        base_request,
        requested_metrics=(),
        primary_outputs=("timeseries.preprocessed",),
        alff_falff=None,
        reho=None,
        request_preprocessing=True,
        preprocessing=preprocessing_with_result_stages(),
        base_cfg_artifact_id="cfg-base-001",
    )
    non_result_stage_resolution = SkillResolver(build_builtin_registry()).resolve(
        non_result_stage_request, environment()
    )
    assert "RESULT_STAGE_OPERATION_WITHOUT_METRIC_UNSUPPORTED" not in {
        item.code for item in validator.validate_resolution(non_result_stage_resolution).issues
    }

    assert base_request.alff_falff is not None
    alff = base_request.alff_falff.model_copy(update={"requested_metrics": (MetricKind.ALFF,)})
    smoothing_with_metric = _skill_request_with(
        base_request,
        requested_metrics=(MetricKind.ALFF,),
        primary_outputs=("zALFF",),
        alff_falff=alff,
        reho=None,
        request_preprocessing=True,
        preprocessing=preprocessing_with_result_stages(smoothing=True),
        base_cfg_artifact_id="cfg-base-001",
    )
    smoothing_resolution = SkillResolver(build_builtin_registry()).resolve(
        smoothing_with_metric, environment()
    )
    assert "RESULT_STAGE_OPERATION_WITHOUT_METRIC_UNSUPPORTED" not in {
        item.code for item in validator.validate_resolution(smoothing_resolution).issues
    }

    metric_only = _skill_request_with(
        base_request,
        requested_metrics=(MetricKind.ALFF,),
        primary_outputs=("zALFF",),
        alff_falff=alff,
        reho=None,
        request_preprocessing=False,
        preprocessing=None,
        base_cfg_artifact_id=None,
    )
    metric_only_resolution = SkillResolver(build_builtin_registry()).resolve(
        metric_only, environment()
    )
    assert "RESULT_STAGE_OPERATION_WITHOUT_METRIC_UNSUPPORTED" not in {
        item.code for item in validator.validate_resolution(metric_only_resolution).issues
    }


def test_reho_cut_checkpoint_capability_gate_has_exact_trigger_and_path(
    base_lineage: ArtifactLineage,
) -> None:
    base_request = request(base_lineage)
    validator = SkillValidator()
    cut_values = preprocessing_parameters().model_dump(mode="json")
    cut_values["scrubbing"]["method"] = "cut"
    cut_preprocessing = PreprocessingParameters.model_validate(cut_values)

    combined = _skill_request_with(
        base_request,
        request_preprocessing=True,
        preprocessing=cut_preprocessing,
        base_cfg_artifact_id="cfg-base-001",
    )
    combined_resolution = SkillResolver(build_builtin_registry()).resolve(combined, environment())
    combined_issues = validator.validate_resolution(combined_resolution).issues
    capability_issues = [
        item for item in combined_issues if item.code == "REHO_CUT_CHECKPOINT_UNSUPPORTED"
    ]
    assert len(capability_issues) == 1
    assert capability_issues[0].path == "preprocessing.scrubbing.method"
    assert capability_issues[0].severity.value == "blocking"
    assert capability_issues[0].remediation
    assert any(item.code == "EFFECTIVE_VOLUME_COUNT_UNKNOWN" for item in combined_issues)

    assert base_request.alff_falff is not None
    alff = base_request.alff_falff.model_copy(update={"requested_metrics": (MetricKind.ALFF,)})
    alff_only_cut = _skill_request_with(
        base_request,
        requested_metrics=(MetricKind.ALFF,),
        primary_outputs=("zALFF",),
        alff_falff=alff,
        reho=None,
        request_preprocessing=True,
        preprocessing=cut_preprocessing,
        base_cfg_artifact_id="cfg-base-001",
    )
    alff_resolution = SkillResolver(build_builtin_registry()).resolve(alff_only_cut, environment())
    assert "REHO_CUT_CHECKPOINT_UNSUPPORTED" not in {
        item.code for item in validator.validate_resolution(alff_resolution).issues
    }

    reho_metric_only = _skill_request_with(
        base_request,
        requested_metrics=(MetricKind.REHO,),
        primary_outputs=("zReHo",),
        alff_falff=None,
        request_preprocessing=False,
        preprocessing=None,
        base_cfg_artifact_id=None,
    )
    metric_only_resolution = SkillResolver(build_builtin_registry()).resolve(
        reho_metric_only, environment()
    )
    assert "REHO_CUT_CHECKPOINT_UNSUPPORTED" not in {
        item.code for item in validator.validate_resolution(metric_only_resolution).issues
    }
