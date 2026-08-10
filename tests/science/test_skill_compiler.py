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
    return EnvironmentSnapshot(
        matlab_version="R2023b",
        spm_version="SPM12",
        dpabi_version="V8.2_240510",
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
        study_protocol_ref="protocol-1",
    )


def compile_plan(lineage: ArtifactLineage):  # type: ignore[no-untyped-def]
    resolver = SkillResolver(build_builtin_registry())
    resolution = resolver.resolve(request(lineage), environment())
    compiler = SkillCompiler(build_default_tool_registry(), SkillValidator())
    return compiler.compile(resolution)


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
        study_protocol_ref="protocol-1",
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
