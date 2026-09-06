"""Compile frozen scientific plans into bounded DPARSFA inputs and outputs."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any

from neuroagent.application.contracts import (
    DatasetKind,
    ManifestRevisionView,
    SubjectManifestEntry,
)
from neuroagent.domain.fmri.metrics import AlffFalffParameters, RehoParameters, SmoothingTiming
from neuroagent.domain.fmri.preprocessing import (
    NormalizationMode,
    NormalizationTiming,
    PreprocessingParameters,
    ScrubbingMethod,
)
from neuroagent.execution.models import (
    ArtifactPathBinding,
    ExpectedArtifact,
    MatlabJobKind,
    MatlabJobSpec,
    PreprocessingJobPayload,
    PreprocessingOutput,
)
from neuroagent.skills.models import SkillPlan, stable_hash
from neuroagent.tools.dpabi_v82 import (
    DpabiCfgProjection,
    DpabiMetricRequest,
    DpabiPreprocessingRequest,
    DpabiV82Adapter,
)
from neuroagent.tools.input_staging import (
    InputFormat,
    StagingCopyOperation,
    StagingCopyPlan,
)


@dataclass(frozen=True, slots=True)
class PreprocessingCompilation:
    spec: MatlabJobSpec
    staging_plan: StagingCopyPlan | None
    parameters: PreprocessingParameters | None
    alff: AlffFalffParameters | None
    reho: RehoParameters | None
    subjects: tuple[SubjectManifestEntry, ...]
    producer_step_hashes: tuple[tuple[str, str], ...]


def compile_preprocessing(
    plan: SkillPlan,
    manifest: ManifestRevisionView,
    manifest_content: dict[str, Any],
    *,
    job_id: str,
    run_id: str,
    approval_record_id: str,
    mask_binding: ArtifactPathBinding | None,
    input_binding: ArtifactPathBinding | None = None,
) -> PreprocessingCompilation:
    """Project approved values; filesystem reads and metadata checks stay with execution.

    The caller must obtain all arguments from the frozen repository revision and
    verify registered input/mask lineage. Raw data are staged only from manifest
    role assignments, never by recursively guessing a functional series.
    """
    _validate_frozen_inputs(plan, manifest, manifest_content)
    names = [name for name, _ in plan.resolved_parameters]
    if len(names) != len(set(names)):
        raise ValueError("frozen scientific parameters contain duplicate names")
    if set(names) - {"preprocessing", "alff_falff", "reho", "base_cfg_artifact_id"}:
        raise ValueError("frozen plan contains unsupported preprocessing parameters")
    values = dict(plan.resolved_parameters)
    parameters = (
        PreprocessingParameters.model_validate(values["preprocessing"])
        if "preprocessing" in values
        else None
    )
    alff = (
        AlffFalffParameters.model_validate(values["alff_falff"]) if "alff_falff" in values else None
    )
    reho = RehoParameters.model_validate(values["reho"]) if "reho" in values else None
    if parameters is None and alff is None and reho is None:
        raise ValueError("plan does not request preprocessing or metrics")
    expected_hash = stable_hash(parameters.model_dump(mode="json")) if parameters else None
    if plan.preprocessing_parameters_hash != expected_hash:
        raise ValueError("preprocessing parameter hash differs from the frozen plan")
    subjects = tuple(manifest.subjects)
    subject_ids = tuple(subject.subject_id for subject in subjects)
    _validate_subjects(subjects)
    builtin_cfg = f"builtin:dpabi-v82-base-cfg:{plan.environment.adapter_version}"
    if parameters is not None:
        if plan.base_cfg_artifact_id != builtin_cfg:
            raise ValueError("only the server-owned builtin base Cfg is supported")
        if values.get("base_cfg_artifact_id") != builtin_cfg:
            raise ValueError("base Cfg parameter does not match the frozen binding")
        if input_binding is not None:
            raise ValueError("raw preprocessing must consume the frozen manifest")
        expected_source = f"manifest:{manifest.manifest_id}:functional-source"
        if plan.input_artifact_id != expected_source:
            raise ValueError("preprocessing source does not match the frozen manifest")
    elif plan.base_cfg_artifact_id is not None or "base_cfg_artifact_id" in values:
        raise ValueError("metric-only plans must not supply a base Cfg")

    bindings = [
        ArtifactPathBinding(
            artifact_id=builtin_cfg, relative_path="input/base_cfg.mat", read_only=True
        )
    ]
    metrics_requested = alff is not None or reho is not None
    if metrics_requested:
        mask_ids = {item.mask_artifact_id for item in (alff, reho) if item is not None}
        if mask_binding is None or mask_ids != {mask_binding.artifact_id}:
            raise ValueError("all requested metrics require the same registered mask binding")
        mask_name = _nifti_name(mask_binding.relative_path)
        bindings.append(
            ArtifactPathBinding(
                artifact_id=mask_binding.artifact_id,
                relative_path=f"staging/mask/{mask_name}",
                read_only=mask_binding.read_only,
            )
        )
    elif mask_binding is not None:
        raise ValueError("a mask binding requires a requested metric")
    metric_request = (
        DpabiMetricRequest(
            subject_ids=subject_ids,
            functional_session_number=1,
            starting_dir_name="FunImg",
            input_manifest_hash=manifest.content_hash,
            mask_relative_path=PurePosixPath(bindings[-1].relative_path)
            .relative_to("staging")
            .as_posix(),
            alff_falff=alff,
            reho=reho,
        )
        if metrics_requested
        else None
    )
    adapter = DpabiV82Adapter()
    staging_plan: StagingCopyPlan | None = None
    if parameters is not None:
        if parameters.normalization.mode in {
            NormalizationMode.T1_SEGMENT,
            NormalizationMode.DARTEL,
        }:
            raise ValueError(
                "T1 segmentation and DARTEL require an interactive DPARSFA path and are "
                "not supported by the headless executor"
            )
        if (
            parameters.normalization.mode is NormalizationMode.EPI_TEMPLATE
            and not parameters.realignment.enabled
        ):
            raise ValueError("EPI template normalization requires realignment mean-image evidence")
        if not metrics_requested and (
            parameters.normalization.timing is NormalizationTiming.ON_RESULTS
            or parameters.smoothing.timing is SmoothingTiming.ON_RESULTS
        ):
            raise ValueError("result-stage operations require a requested metric output")
        if metrics_requested and parameters.normalization.timing is NormalizationTiming.ON_RESULTS:
            raise ValueError("result normalization with metrics requires separate mask spaces")
        if reho is not None and parameters.scrubbing.method is ScrubbingMethod.CUT:
            raise ValueError("CUT scrubbing requires a verified checkpoint before a ReHo plan")
        staging_plan = _staging_plan(plan, manifest, manifest_content, parameters)
        projection = adapter.project_pipeline_cfg(
            DpabiPreprocessingRequest(
                subject_ids=subject_ids,
                functional_session_number=1,
                starting_dir_name="FunImg",
                input_manifest_hash=manifest.content_hash,
                parameters=parameters,
            ),
            metric_request,
        )
    else:
        if input_binding is None or input_binding.artifact_id != plan.input_artifact_id:
            raise ValueError("metric-only execution requires its registered checkpoint binding")
        if len(subjects) != 1:
            raise ValueError("metric-only execution requires a single subject checkpoint")
        bindings.append(
            ArtifactPathBinding(
                artifact_id=input_binding.artifact_id,
                relative_path=(
                    f"staging/FunImg/{subject_ids[0]}/{_nifti_name(input_binding.relative_path)}"
                ),
                read_only=input_binding.read_only,
            )
        )
        assert metric_request is not None
        metric_projection = adapter.project_metric_cfg(metric_request)
        cfg = {**_disabled_preprocessing_cfg(), **metric_projection.cfg}
        projection = DpabiCfgProjection(
            adapter_version=metric_projection.adapter_version,
            source_evidence=metric_projection.source_evidence,
            cfg=cfg,
            cfg_hash=stable_hash(cfg),
        )
    outputs = _outputs(subject_ids, alff, reho)
    expected_artifacts = (
        *(
            ExpectedArtifact(
                artifact_type=(
                    "preprocessing.functional_timeseries"
                    if output.metric == "timeseries"
                    else f"metric.{output.metric}.{output.scaling}"
                ),
                relative_pattern=output.relative_path,
                required=True,
            )
            for output in outputs
        ),
        ExpectedArtifact(
            artifact_type="preprocessing.metadata_evidence",
            relative_pattern="output/preprocessing-evidence.json",
            required=True,
        ),
        ExpectedArtifact(
            artifact_type="preprocessing.execution_log",
            relative_pattern="logs/matlab.log",
            required=True,
        ),
    )
    spec = MatlabJobSpec(
        job_id=job_id,
        run_id=run_id,
        kind=MatlabJobKind.DPARSFA_PREPROCESSING,
        plan_hash=plan.plan_hash,
        approval_record_id=approval_record_id,
        input_manifest_hash=manifest.content_hash,
        timeout_seconds=86_400,
        artifact_bindings=tuple(bindings),
        expected_artifacts=expected_artifacts,
        payload=PreprocessingJobPayload(
            base_cfg_artifact_id=builtin_cfg,
            staging_relative_path="staging",
            metric_projection=projection,
            subject_ids=subject_ids,
            base_cfg_allowed_fields=("Realign",) if projection.cfg.get("IsRealign") else (),
            outputs=outputs,
        ),
    )
    producer_step_hashes = tuple(
        (step.step_id, stable_hash(step.model_dump(mode="json")))
        for step in plan.steps
        if step.step_id
        in {"verify_preprocessed_metadata", "calculate_alff_falff", "calculate_reho"}
    )
    return PreprocessingCompilation(
        spec, staging_plan, parameters, alff, reho, subjects, producer_step_hashes
    )


def _validate_frozen_inputs(
    plan: SkillPlan, manifest: ManifestRevisionView, content: dict[str, Any]
) -> None:
    body = plan.model_dump(mode="json", exclude={"plan_id", "plan_hash", "warnings"})
    if stable_hash(body) != plan.plan_hash:
        raise ValueError("frozen SkillPlan content hash does not match")
    if manifest.content_hash != plan.input_manifest_hash:
        raise ValueError("manifest hash does not match the frozen plan")
    if manifest.dataset_id != plan.dataset_ref:
        raise ValueError("manifest dataset does not match the frozen plan")
    if stable_hash(content) != manifest.content_hash:
        raise ValueError("frozen manifest content hash does not match")
    if content.get("subjects") != [
        subject.model_dump(mode="json") for subject in manifest.subjects
    ]:
        raise ValueError("manifest subject roles do not match frozen content")
    if content.get("profile") != manifest.profile.model_dump(mode="json"):
        raise ValueError("manifest profile does not match frozen content")
    if manifest.profile.kind not in {DatasetKind.NIFTI, DatasetKind.BIDS, DatasetKind.DPABI_READY}:
        raise ValueError("real preprocessing requires a frozen NIfTI input manifest")


def _validate_subjects(subjects: tuple[SubjectManifestEntry, ...]) -> None:
    ids = [subject.subject_id for subject in subjects]
    if not ids or len(ids) != len(set(ids)):
        raise ValueError("preprocessing requires unique subjects in a single session")
    if len({subject.session_id for subject in subjects}) != 1:
        raise ValueError("multiple sessions are not supported by this preprocessing compiler")
    if len({subject.subject_id.casefold() for subject in subjects}) != len(ids):
        raise ValueError("subject IDs collide on a case-insensitive filesystem")
    if any(not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", item) for item in ids):
        raise ValueError("subject IDs contain unsafe path characters")


def _nifti_name(relative: str) -> str:
    path = PurePosixPath(relative.replace("\\", "/"))
    if path.is_absolute() or ".." in path.parts or ":" in relative:
        raise ValueError("NIfTI source must be a safe relative path")
    if not path.name.lower().endswith((".nii", ".nii.gz")):
        raise ValueError("preprocessing inputs must be NIfTI files")
    return path.name


def _staging_plan(
    plan: SkillPlan,
    manifest: ManifestRevisionView,
    content: dict[str, Any],
    parameters: PreprocessingParameters,
) -> StagingCopyPlan:
    hashes, sizes = content.get("file_hashes"), content.get("file_sizes")
    if not isinstance(hashes, dict) or not isinstance(sizes, dict):
        raise ValueError("frozen manifest requires file hashes and sizes")
    needs_t1 = parameters.normalization.mode in {
        NormalizationMode.T1_SEGMENT,
        NormalizationMode.DARTEL,
    }
    if needs_t1 and parameters.normalization.structural_artifact_id != (
        f"manifest:{manifest.manifest_id}:anatomical-source"
    ):
        raise ValueError("structural input does not match the frozen manifest")
    operations: list[StagingCopyOperation] = []
    sources: set[str] = set()
    for subject in manifest.subjects:
        if len(subject.functional_files) != 1:
            raise ValueError("each subject requires exactly one explicit 4D functional NIfTI")
        if len(subject.anatomical_files) > 1 or (needs_t1 and not subject.anatomical_files):
            raise ValueError("T1 preprocessing requires one unambiguous anatomical image")
        role_files = [("FunImg", subject.functional_files)]
        if needs_t1:
            role_files.append(("T1Img", subject.anatomical_files))
        for directory, paths in role_files:
            for relative in paths:
                name = _nifti_name(relative)
                if relative.casefold() in sources:
                    raise ValueError("a scientific input file cannot be assigned to two roles")
                sources.add(relative.casefold())
                if relative not in hashes or relative not in sizes:
                    raise ValueError("scientific input is missing frozen hash or size evidence")
                operations.append(
                    StagingCopyOperation(
                        source_artifact_id=f"manifest:{manifest.manifest_id}:file:{stable_hash(relative)}",
                        source_relative_path=relative,
                        destination_relative_path=f"staging/{directory}/{subject.subject_id}/{name}",
                        sha256=hashes[relative],
                        size_bytes=sizes[relative],
                    )
                )
    input_format = {
        DatasetKind.BIDS: InputFormat.BIDS_NIFTI,
        DatasetKind.NIFTI: InputFormat.NIFTI,
        DatasetKind.DPABI_READY: InputFormat.DPABI_READY,
    }[manifest.profile.kind]
    body = {
        "source_artifact_id": plan.input_artifact_id,
        "input_manifest_hash": manifest.content_hash,
        "input_format": input_format.value,
        "source_read_only": True,
        "operations": [operation.model_dump(mode="json") for operation in operations],
    }
    return StagingCopyPlan(**body, plan_hash=stable_hash(body))


def _outputs(
    subjects: tuple[str, ...], alff: AlffFalffParameters | None, reho: RehoParameters | None
) -> tuple[PreprocessingOutput, ...]:
    result: list[PreprocessingOutput] = []
    for subject in subjects:
        result.append(
            PreprocessingOutput(
                subject_id=subject,
                metric="timeseries",
                scaling=None,
                relative_path=f"output/{subject}/timeseries.nii",
            )
        )
        for parameters in (alff, reho):
            if parameters is None:
                continue
            metrics = (
                tuple(metric.value for metric in parameters.requested_metrics)
                if isinstance(parameters, AlffFalffParameters)
                else ("reho",)
            )
            if len(set(parameters.requested_scalings)) != len(parameters.requested_scalings):
                raise ValueError("requested metric scalings must be unique")
            for metric in metrics:
                for scaling in parameters.requested_scalings:
                    result.append(
                        PreprocessingOutput(
                            subject_id=subject,
                            metric=metric,
                            scaling=scaling.value,
                            relative_path=f"output/{subject}/{scaling.value}_{metric}.nii",
                        )
                    )
    return tuple(result)


def _disabled_preprocessing_cfg() -> dict[str, Any]:
    """No inherited processing for a previously verified functional checkpoint."""
    disabled = (
        "IsNeedConvertFunDCM2IMG",
        "IsNeedConvertT1DCM2IMG",
        "IsBIDStoDPARSF",
        "IsApplyDownloadedReorientMats",
        "IsNeedReorientFunImgInteractively",
        "IsNeedReorientCropT1Img",
        "IsNeedReorientT1ImgInteractively",
        "IsNeedReorientInteractivelyAfterCoreg",
        "IsBet",
        "IsCalVoxelSpecificHeadMotion",
        "IsCalDegreeCentrality",
        "IsCalFC",
        "IsExtractROISignals",
        "IsDefineROIInteractively",
        "IsExtractAALTC",
        "IsNormalizeToSymmetricGroupT1Mean",
        "IsSmoothBeforeVMHC",
        "IsCalVMHC",
        "IsCWAS",
        "IsAllowGUI",
        "IsSliceTiming",
        "IsRealign",
        "IsDetrend",
        "IsAutoMask",
        "IsWarpMasksIntoIndividualSpace",
        "IsCovremove",
        "IsNormalize",
        "IsNeedT1CoregisterToFun",
        "IsSegment",
        "IsDARTEL",
        "IsScrubbing",
        "IsFilter",
        "IsSmooth",
        "RemoveFirstTimePoints",
        "TimePoints",
    )
    return dict.fromkeys(disabled, 0)
