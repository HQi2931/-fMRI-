"""Reviewed built-in rs-fMRI Skill specifications.

The built-ins mirror the checked-in ``skills/*/skill.yaml`` contracts.  They
remain declarative and never execute tools or store mutable run state.
"""

from __future__ import annotations

from neuroagent.domain.fmri.metrics import MetricKind
from neuroagent.skills.models import (
    ArtifactContract,
    SkillCompatibility,
    SkillSpec,
    SkillStatus,
    SkillStep,
)
from neuroagent.skills.registry import SkillRegistry

_COMPATIBILITY = SkillCompatibility(
    matlab="R2023b",
    spm="SPM12",
    dpabi="V8.2_240510",
    adapter="1.0.0",
)

_METRIC_EVIDENCE = (
    "DPABI_V8.2_240510/DPARSF/DPARSFA_run.m:3925-4101",
    "DPABI_V8.2_240510/DPARSF/Subfunctions/y_alff_falff.m:90-155",
    "DPABI_V8.2_240510/DPARSF/Subfunctions/y_reho.m:69-95",
    "docs/architecture/fmri-skill-layer.md#7-alfffalff-and-reho",
)


def _common_steps() -> tuple[SkillStep, ...]:
    return (
        SkillStep(
            step_id="freeze_manifest",
            capability="fmri.manifest.verify",
            produces=("manifest.frozen",),
        ),
        SkillStep(
            step_id="stage_dpabi_input",
            capability="fmri.dpabi.stage_input",
            needs=("freeze_manifest",),
            consumes=("manifest.frozen", "functional.source"),
            produces=("functional.staged",),
        ),
        SkillStep(
            step_id="preprocess_common",
            capability="fmri.dpabi.preprocess",
            needs=("stage_dpabi_input",),
            consumes=("functional.staged",),
            produces=(
                "timeseries.unverified.unfiltered.unsmoothed",
                "timeseries.preprocessed",
            ),
            parameter_names=("preprocessing", "base_cfg_artifact_id"),
            qc_gate=True,
        ),
        SkillStep(
            step_id="verify_preprocessed_metadata",
            capability="fmri.artifact.verify_metadata",
            needs=("preprocess_common",),
            consumes=("timeseries.unverified.unfiltered.unsmoothed",),
            produces=("timeseries.verified.unfiltered.unsmoothed",),
            parameter_names=("preprocessing", "alff_falff", "reho"),
            qc_gate=True,
        ),
    )


def _alff_step() -> SkillStep:
    return SkillStep(
        step_id="calculate_alff_falff",
        capability="fmri.dpabi.alff_falff",
        consumes=("timeseries.verified.unfiltered.unsmoothed",),
        produces=("metric.alff", "metric.falff"),
        parameter_names=("alff_falff",),
    )


def _reho_steps(
    *, prepare_needs: tuple[str, ...] = (), bind_scrubbing: bool = False
) -> tuple[SkillStep, ...]:
    parameter_names = (
        "reho.temporal_filter_band",
        "reho.temporal_filter_add_mean_back",
        *(("preprocessing.scrubbing",) if bind_scrubbing else ()),
    )
    return (
        SkillStep(
            step_id="prepare_reho_timeseries",
            capability="fmri.dpabi.prepare_reho",
            needs=prepare_needs,
            consumes=("timeseries.verified.unfiltered.unsmoothed",),
            produces=("timeseries.reho_ready.verified.unsmoothed",),
            parameter_names=parameter_names,
        ),
        SkillStep(
            step_id="calculate_reho",
            capability="fmri.dpabi.reho",
            needs=("prepare_reho_timeseries",),
            consumes=("timeseries.reho_ready.verified.unsmoothed",),
            produces=("metric.reho",),
            parameter_names=("reho",),
        ),
    )


def _metric_qc(needs: tuple[str, ...]) -> SkillStep:
    artifacts: list[str] = []
    if "calculate_alff_falff" in needs:
        artifacts.extend(("metric.alff", "metric.falff"))
    if "calculate_reho" in needs:
        artifacts.append("metric.reho")
    return SkillStep(
        step_id="metric_qc",
        capability="fmri.qc.metrics",
        needs=needs,
        consumes=tuple(artifacts),
        produces=("qc.metric_report",),
        qc_gate=True,
    )


def _dataset_skill() -> SkillSpec:
    step = SkillStep(
        step_id="inspect_dataset",
        capability="fmri.dataset.inspect",
        consumes=("dataset.source",),
        produces=("dataset.inspection_report",),
        parameter_names=("source_root", "dataset_ref"),
        qc_gate=True,
    )
    return SkillSpec(
        schema_version="1.0",
        skill_id="rsfmri.dataset.inspect",
        version="1.0.0",
        title="Read-only rs-fMRI dataset inspection",
        status=SkillStatus.REVIEWED,
        requested_metrics=(),
        required_parameters=("source_root", "dataset_ref", "input_manifest_hash"),
        input_artifacts=(
            ArtifactContract(
                name="source",
                artifact_type="dataset.source",
                required_lineage=("read_only=true",),
            ),
        ),
        output_artifacts=(
            ArtifactContract(
                name="report",
                artifact_type="dataset.inspection_report",
                required_lineage=("source_hashes",),
            ),
        ),
        required_capabilities=(step.capability,),
        workflow_template_ref="dataset.inspect.v1",
        steps=(step,),
        qc_requirements=("source_read_only", "subject_manifest_explicit"),
        compatibility=_COMPATIBILITY,
        evidence_refs=("AGENTS.md#6-数据与科研安全",),
        known_limitations=("Inspection does not establish scientific suitability",),
        reviewed_by=("skill_workflow_engineer", "matlab_dpabi_engineer"),
    )


def _common_skill() -> SkillSpec:
    steps = _common_steps()
    return SkillSpec(
        schema_version="1.0",
        skill_id="rsfmri.preprocess.common",
        version="1.0.0",
        title="Common DPABI preprocessing checkpoint",
        status=SkillStatus.REVIEWED,
        requested_metrics=(),
        required_parameters=("preprocessing", "base_cfg_artifact_id"),
        input_artifacts=(
            ArtifactContract(
                name="source",
                artifact_type="functional.source",
                required_lineage=("subject_manifest_hash", "read_only=true"),
            ),
        ),
        output_artifacts=(
            ArtifactContract(
                name="checkpoint",
                artifact_type="timeseries.verified.unfiltered.unsmoothed",
                required_lineage=(
                    "metadata_verified=true",
                    "metadata_evidence_hash",
                    "tr_seconds",
                    "volume_count",
                    "grid_signature",
                    "producer_step_hash",
                ),
            ),
            ArtifactContract(
                name="preprocessed",
                artifact_type="timeseries.preprocessed",
                required_lineage=("grid_signature", "producer_step_hash"),
            ),
        ),
        required_capabilities=tuple(step.capability for step in steps),
        workflow_template_ref="dpabi.preprocess.v1",
        steps=steps,
        qc_requirements=(
            "input_manifest_frozen",
            "source_read_only",
            "executor_header_metadata_verified",
            "manual_plan_approval",
        ),
        compatibility=_COMPATIBILITY,
        evidence_refs=("DPABI_V8.2_240510/DPARSF/DPARSFA_run.m:1-192",),
        known_limitations=("Study-specific preprocessing parameters remain mandatory",),
        reviewed_by=(
            "skill_workflow_engineer",
            "matlab_dpabi_engineer",
            "fmri_methodologist",
        ),
    )


def _alff_skill() -> SkillSpec:
    steps = (_alff_step(), _metric_qc(("calculate_alff_falff",)))
    return SkillSpec(
        schema_version="1.0",
        skill_id="rsfmri.metric.alff_falff",
        version="1.0.0",
        title="DPABI ALFF and fALFF",
        status=SkillStatus.REVIEWED,
        requested_metrics=(MetricKind.ALFF, MetricKind.FALFF),
        required_parameters=("alff_falff",),
        input_artifacts=(
            ArtifactContract(
                name="functional",
                artifact_type="functional_timeseries",
                required_lineage=(
                    "metadata_verified=true",
                    "metadata_evidence_hash",
                    "tr_seconds",
                    "volume_count",
                    "mask",
                    "temporally_filtered=false",
                    "spatially_smoothed=false",
                ),
            ),
        ),
        output_artifacts=(
            ArtifactContract(
                name="alff",
                artifact_type="metric.alff",
                required_lineage=("frequency_band", "mask", "scaling"),
            ),
            ArtifactContract(
                name="falff",
                artifact_type="metric.falff",
                required_lineage=("frequency_band", "mask", "scaling"),
            ),
        ),
        required_capabilities=tuple(step.capability for step in steps),
        workflow_template_ref="dpabi.alff_falff.v1",
        steps=steps,
        qc_requirements=("alff_falff_metric_qc", "manual_pre_statistics_qc"),
        compatibility=_COMPATIBILITY,
        evidence_refs=(
            "DPABI_V8.2_240510/DPARSF/DPARSFA_run.m:3925-4008",
            "DPABI_V8.2_240510/DPARSF/Subfunctions/y_alff_falff.m:90-155",
        ),
        known_limitations=("IsCalALFF produces ALFF and fALFF together",),
        reviewed_by=(
            "skill_workflow_engineer",
            "matlab_dpabi_engineer",
            "fmri_methodologist",
        ),
    )


def _reho_skill() -> SkillSpec:
    steps = (*_reho_steps(), _metric_qc(("calculate_reho",)))
    return SkillSpec(
        schema_version="1.0",
        skill_id="rsfmri.metric.reho",
        version="1.0.0",
        title="DPABI ReHo",
        status=SkillStatus.REVIEWED,
        requested_metrics=(MetricKind.REHO,),
        required_parameters=("reho",),
        input_artifacts=(
            ArtifactContract(
                name="functional",
                artifact_type="functional_timeseries",
                required_lineage=(
                    "metadata_verified=true",
                    "metadata_evidence_hash",
                    "tr_seconds",
                    "volume_count",
                    "mask",
                    "spatially_smoothed=false",
                    "temporally_filtered=false",
                ),
            ),
        ),
        output_artifacts=(
            ArtifactContract(
                name="reho",
                artifact_type="metric.reho",
                required_lineage=("cluster_voxels", "mask", "scaling", "smoothing"),
            ),
        ),
        required_capabilities=tuple(step.capability for step in steps),
        workflow_template_ref="dpabi.reho.v1",
        steps=steps,
        qc_requirements=("reho_metric_qc", "manual_pre_statistics_qc"),
        compatibility=_COMPATIBILITY,
        evidence_refs=(
            "DPABI_V8.2_240510/DPARSF/DPARSFA_run.m:4056-4095",
            "DPABI_V8.2_240510/DPARSF/Subfunctions/y_reho.m:69-95",
        ),
        known_limitations=("Physical neighborhood comparability still requires grid QC",),
        reviewed_by=(
            "skill_workflow_engineer",
            "matlab_dpabi_engineer",
            "fmri_methodologist",
        ),
    )


def _combined_skill() -> SkillSpec:
    steps = (
        _alff_step(),
        *_reho_steps(
            prepare_needs=("calculate_alff_falff",),
            bind_scrubbing=True,
        ),
        _metric_qc(("calculate_alff_falff", "calculate_reho")),
    )
    return SkillSpec(
        schema_version="1.0",
        skill_id="rsfmri.pipeline.alff_reho_combined",
        version="1.0.0",
        title="DPABI combined ALFF/fALFF and ReHo pipeline",
        status=SkillStatus.REVIEWED,
        requested_metrics=(MetricKind.ALFF, MetricKind.FALFF, MetricKind.REHO),
        required_parameters=("alff_falff", "reho"),
        input_artifacts=(
            ArtifactContract(
                name="functional",
                artifact_type="functional_timeseries",
                required_lineage=(
                    "temporally_filtered=false",
                    "spatially_smoothed=false",
                ),
            ),
        ),
        output_artifacts=(
            ArtifactContract(name="alff", artifact_type="metric.alff"),
            ArtifactContract(name="falff", artifact_type="metric.falff"),
            ArtifactContract(name="reho", artifact_type="metric.reho"),
        ),
        required_capabilities=tuple(dict.fromkeys(step.capability for step in steps)),
        workflow_template_ref="dpabi.alff_reho_combined.v1",
        steps=steps,
        qc_requirements=(
            "alff_falff_metric_qc",
            "reho_metric_qc",
            "manual_pre_statistics_qc",
        ),
        compatibility=_COMPATIBILITY,
        evidence_refs=_METRIC_EVIDENCE,
        known_limitations=(
            "ALFF/fALFF consumes the pre-filter checkpoint; "
            "ReHo may consume a later filter checkpoint",
        ),
        reviewed_by=(
            "skill_workflow_engineer",
            "matlab_dpabi_engineer",
            "fmri_methodologist",
        ),
    )


def _qc_skill() -> SkillSpec:
    step = SkillStep(
        step_id="pre_statistics_qc",
        capability="fmri.qc.pre_statistics",
        consumes=("metric.primary_maps",),
        produces=("qc.review_revision",),
        parameter_names=("qc_protocol", "input_manifest_hash"),
        qc_gate=True,
    )
    return SkillSpec(
        schema_version="1.0",
        skill_id="rsfmri.qc.pre_statistics",
        version="1.0.0",
        title="Manual pre-statistics QC gate",
        status=SkillStatus.REVIEWED,
        requested_metrics=(),
        required_parameters=("qc_protocol", "input_manifest_hash"),
        input_artifacts=(
            ArtifactContract(
                name="metrics",
                artifact_type="metric.primary_maps",
                required_lineage=("subject_id", "producer_step_hash", "grid_signature"),
            ),
        ),
        output_artifacts=(
            ArtifactContract(
                name="review",
                artifact_type="qc.review_revision",
                required_lineage=("input_manifest_hash", "included_subject_order"),
            ),
        ),
        required_capabilities=(step.capability,),
        workflow_template_ref="qc.pre_statistics.v1",
        steps=(step,),
        qc_requirements=("manual_approval", "frozen_inclusion_order"),
        compatibility=_COMPATIBILITY,
        evidence_refs=("docs/architecture/fmri-skill-layer.md#74-qc-gate",),
        known_limitations=("QC thresholds must come from the study protocol",),
        reviewed_by=("skill_workflow_engineer", "fmri_methodologist"),
    )


def _statistics_skills() -> tuple[SkillSpec, SkillSpec, SkillSpec]:
    ttest_step = SkillStep(
        step_id="ttest",
        capability="fmri.dpabi.statistics.ttest",
        consumes=("metric.qc_approved_maps",),
        produces=("statistics.uncorrected_map",),
        parameter_names=("statistical_design", "correction"),
        qc_gate=True,
    )
    ttest = SkillSpec(
        schema_version="1.0",
        skill_id="rsfmri.statistics.ttest",
        version="1.0.0",
        title="DPABI group-level t tests",
        status=SkillStatus.REVIEWED,
        requested_metrics=(),
        required_parameters=("statistical_design", "correction"),
        input_artifacts=(
            ArtifactContract(
                name="metrics",
                artifact_type="metric.qc_approved_maps",
                required_lineage=("qc_review_hash", "subject_order", "grid_signature"),
            ),
        ),
        output_artifacts=(
            ArtifactContract(
                name="statistics",
                artifact_type="statistics.uncorrected_map",
                required_lineage=("design_hash", "contrast"),
            ),
        ),
        required_capabilities=(ttest_step.capability,),
        workflow_template_ref="rsfmri.statistics.ttest.v1",
        steps=(ttest_step,),
        qc_requirements=("approved_qc_revision", "frozen_subject_order"),
        compatibility=_COMPATIBILITY,
        evidence_refs=(
            "DPABI_V8.2_240510/StatisticalAnalysis/y_TTest1_Image.m:1",
            "DPABI_V8.2_240510/StatisticalAnalysis/y_TTest2_Image.m:1",
            "DPABI_V8.2_240510/StatisticalAnalysis/y_TTestPaired_Image.m:1",
            "DPABI_V8.2_240510/StatisticalAnalysis/y_FDR_Image.m:1",
            "DPABI_V8.2_240510/StatisticalAnalysis/y_GRF_Threshold.m:1",
        ),
        known_limitations=("Thresholds, tails and covariates require study-level approval",),
        reviewed_by=(
            "skill_workflow_engineer",
            "matlab_dpabi_engineer",
            "fmri_methodologist",
        ),
    )
    return ttest, _correction_skill("fdr"), _correction_skill("grf")


def _correction_skill(method: str) -> SkillSpec:
    is_fdr = method == "fdr"
    capability = f"fmri.dpabi.statistics.{method}"
    output = f"statistics.{method}_corrected_map"
    step = SkillStep(
        step_id=method,
        capability=capability,
        consumes=("statistics.uncorrected_map",),
        produces=(output,),
        parameter_names=("correction",),
    )
    source = "y_FDR_Image.m:1" if is_fdr else "y_GRF_Threshold.m:1"
    return SkillSpec(
        schema_version="1.0",
        skill_id=f"rsfmri.statistics.{method}",
        version="1.0.0",
        title=f"DPABI {method.upper()} correction",
        status=SkillStatus.REVIEWED,
        requested_metrics=(),
        required_parameters=("correction",),
        input_artifacts=(
            ArtifactContract(name="input", artifact_type="statistics.uncorrected_map"),
        ),
        output_artifacts=(ArtifactContract(name="output", artifact_type=output),),
        required_capabilities=(capability,),
        workflow_template_ref=f"rsfmri.statistics.{method}.v1",
        steps=(step,),
        qc_requirements=("approved_qc_revision", "frozen_subject_order"),
        compatibility=_COMPATIBILITY,
        evidence_refs=(f"DPABI_V8.2_240510/StatisticalAnalysis/{source}",),
        known_limitations=("Thresholds and tails require explicit study-level approval",),
        reviewed_by=(
            "skill_workflow_engineer",
            "matlab_dpabi_engineer",
            "fmri_methodologist",
        ),
    )


def builtin_skill_specs() -> tuple[SkillSpec, ...]:
    ttest, fdr, grf = _statistics_skills()
    return (
        _common_skill(),
        _alff_skill(),
        _reho_skill(),
        _combined_skill(),
        _dataset_skill(),
        _qc_skill(),
        ttest,
        fdr,
        grf,
    )


def build_builtin_registry() -> SkillRegistry:
    registry = SkillRegistry()
    for spec in builtin_skill_specs():
        registry.register(spec)
    registry.freeze()
    return registry
