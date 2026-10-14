from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from neuroagent.application.contracts.projects import (
    DatasetProfile,
    ManifestRevisionView,
    SubjectManifestEntry,
)
from neuroagent.domain.fmri import (
    AlffFalffParameters,
    ArtifactLineage,
    MetricScaling,
    PreprocessingParameters,
    RehoParameters,
)
from neuroagent.domain.fmri.dataset import (
    DatasetKind,
)
from neuroagent.domain.fmri.preprocessing_outputs import (
    enumerate_dpabi_preprocessing_outputs,
)
from neuroagent.execution.models import ArtifactPathBinding, PreprocessingJobPayload
from neuroagent.execution.preprocessing import PreprocessingCompilation, compile_preprocessing
from neuroagent.skills.models import SkillPlan, stable_hash
from neuroagent.tools.input_staging import StagingCopyTool
from tests.science.conftest import preprocessing_parameters
from tests.science.test_skill_compiler import environment, request


def manifest_for(
    subjects: list[SubjectManifestEntry] | None = None,
    *,
    kind: DatasetKind = DatasetKind.NIFTI,
) -> tuple[ManifestRevisionView, dict[str, Any]]:
    if subjects is None:
        subjects = [
            SubjectManifestEntry(
                subject_id="sub-01",
                functional_files=["sub-01/func/rest.nii"],
                anatomical_files=["sub-01/anat/t1.nii"],
            )
        ]
    files = [
        path
        for subject in subjects
        for path in (*subject.functional_files, *subject.anatomical_files)
    ]
    profile = DatasetProfile(
        kind=kind,
        file_count=len(files),
        nifti_count=len(files),
        dicom_count=0,
        subject_count=len(subjects),
    )
    content = {
        "subjects": [subject.model_dump(mode="json") for subject in subjects],
        "profile": profile.model_dump(mode="json"),
        "file_hashes": dict.fromkeys(files, "a" * 64),
        "file_sizes": dict.fromkeys(files, 12),
    }
    return (
        ManifestRevisionView(
            manifest_id="manifest-01",
            dataset_id="dataset-1",
            revision=1,
            content_hash=stable_hash(content),
            profile=profile,
            subjects=subjects,
            created_at=datetime.now(UTC),
        ),
        content,
    )


def common_parameters() -> PreprocessingParameters:
    data = preprocessing_parameters().model_dump(mode="json")
    data["normalization"].update(
        mode=1,
        structural_artifact_id=None,
        affine_regularization=None,
    )
    data["nuisance"]["csf"]["mask_source"] = "spm"
    data["scrubbing"]["method"] = "nearest"
    return PreprocessingParameters.model_validate(data)


def frozen_plan(
    manifest: ManifestRevisionView,
    *,
    parameters: PreprocessingParameters | None = None,
    alff: AlffFalffParameters | None = None,
    reho: RehoParameters | None = None,
) -> SkillPlan:
    values: list[tuple[str, Any]] = []
    base_cfg = None
    if parameters is not None:
        base_cfg = "builtin:dpabi-v82-base-cfg:1.0.0"
        values.extend(
            [
                ("preprocessing", parameters.model_dump(mode="json")),
                ("base_cfg_artifact_id", base_cfg),
            ]
        )
    if alff is not None:
        values.append(("alff_falff", alff.model_dump(mode="json")))
    if reho is not None:
        values.append(("reho", reho.model_dump(mode="json")))
    plan = SkillPlan(
        plan_id="plan-01",
        project_id="project-1",
        dataset_ref=manifest.dataset_id,
        input_manifest_hash=manifest.content_hash,
        input_artifact_id=(
            f"manifest:{manifest.manifest_id}:functional-source" if parameters else "checkpoint-01"
        ),
        base_cfg_artifact_id=base_cfg,
        preprocessing_parameters_hash=(
            stable_hash(parameters.model_dump(mode="json")) if parameters else None
        ),
        skill_locks=(),
        resolved_parameters=tuple(sorted(values)),
        environment=environment(),
        steps=(),
        artifact_expectations=(),
        qc_gates=(),
        approval_requirements=(),
        warnings=(),
        plan_hash="0" * 64,
    )
    return rehash(plan)


def rehash(plan: SkillPlan) -> SkillPlan:
    body = plan.model_dump(mode="json", exclude={"plan_id", "plan_hash", "warnings"})
    return plan.model_copy(update={"plan_hash": stable_hash(body)})


def compile_fixture(
    plan: SkillPlan,
    manifest: ManifestRevisionView,
    content: dict[str, Any],
    *,
    metric_only: bool = False,
) -> PreprocessingCompilation:
    values = dict(plan.resolved_parameters)
    return compile_preprocessing(
        plan,
        manifest,
        content,
        job_id="job-01",
        run_id="run-01",
        approval_record_id="approval-01",
        mask_binding=(
            ArtifactPathBinding(
                artifact_id="mask-001", relative_path="output/mask.nii", read_only=True
            )
            if "alff_falff" in values or "reho" in values
            else None
        ),
        input_binding=(
            ArtifactPathBinding(
                artifact_id="checkpoint-01", relative_path="output/verified.nii", read_only=True
            )
            if metric_only
            else None
        ),
    )


def test_frozen_common_plan_stages_roles_with_precise_subject_outputs() -> None:
    manifest, content = manifest_for()
    plan = frozen_plan(manifest, parameters=common_parameters())
    result = compile_fixture(plan, manifest, content)
    assert result.staging_plan is not None
    assert [
        operation.destination_relative_path for operation in result.staging_plan.operations
    ] == [
        "staging/FunImg/sub-01/rest.nii",
    ]
    payload = result.spec.payload
    assert isinstance(payload, PreprocessingJobPayload)
    assert payload.base_cfg_allowed_fields == ("Realign",)
    assert payload.metric_projection.cfg["StartingDirName"] == "FunImg"
    assert payload.metric_projection.cfg["IsCalALFF"] == 0
    assert {item.relative_pattern for item in result.spec.expected_artifacts} == {
        "output/sub-01/timeseries.nii",
        "output/preprocessing-evidence.json",
        "logs/matlab.log",
    }
    assert result.spec.job_hash == compile_fixture(plan, manifest, content).spec.job_hash


@pytest.mark.parametrize("mode", [2, 3])
def test_headless_compiler_integrates_t1_preparation(mode: int) -> None:
    manifest, content = manifest_for()
    values = preprocessing_parameters().model_dump(mode="json")
    values["scrubbing"]["method"] = "nearest"
    values["normalization"].update(
        mode=mode,
        structural_artifact_id=f"manifest:{manifest.manifest_id}:anatomical-source",
        affine_regularization="mni",
    )
    parameters = PreprocessingParameters.model_validate(values)

    result = compile_fixture(frozen_plan(manifest, parameters=parameters), manifest, content)
    assert result.spec.payload.t1_preparation == "reorient_crop"
    assert result.spec.payload.metric_projection.cfg["IsNeedReorientCropT1Img"] == 0
    assert any("/T1Img/" in op.destination_relative_path for op in result.staging_plan.operations)


def test_epi_template_normalization_requires_realignment() -> None:
    manifest, content = manifest_for()
    values = common_parameters().model_dump(mode="json")
    values["realignment"] = {"enabled": False, "options_source": None}
    values["nuisance"] = {
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
    values["scrubbing"] = {
        "enabled": False,
        "timing": None,
        "censoring": None,
        "method": None,
    }
    parameters = PreprocessingParameters.model_validate(values)

    with pytest.raises(ValueError, match="requires realignment"):
        compile_fixture(frozen_plan(manifest, parameters=parameters), manifest, content)


@pytest.mark.parametrize("operation", ["normalization", "smoothing"])
def test_result_stage_operation_without_metrics_is_rejected(operation: str) -> None:
    manifest, content = manifest_for()
    values = common_parameters().model_dump(mode="json")
    if operation == "normalization":
        values["normalization"]["timing"] = "on_results"
        values["nuisance"]["timing"] = "after_realign"
    else:
        values["smoothing"] = {"timing": "on_results", "method": 1, "fwhm_mm": [6, 6, 6]}
    parameters = PreprocessingParameters.model_validate(values)

    with pytest.raises(ValueError, match="require a requested metric"):
        compile_fixture(frozen_plan(manifest, parameters=parameters), manifest, content)


def test_combined_metrics_preserve_each_requested_scaling(base_lineage: ArtifactLineage) -> None:
    manifest, content = manifest_for()
    requested = request(base_lineage)
    assert requested.alff_falff is not None
    alff = requested.alff_falff.model_copy(update={"requested_scalings": tuple(MetricScaling)})
    plan = frozen_plan(manifest, parameters=common_parameters(), alff=alff, reho=requested.reho)
    result = compile_fixture(plan, manifest, content)
    payload = result.spec.payload
    assert isinstance(payload, PreprocessingJobPayload)
    assert payload.metric_projection.cfg["MaskFile"] == "mask/mask.nii"
    names = {item.relative_path for item in payload.outputs}
    assert names == {
        "output/sub-01/timeseries.nii",
        "output/sub-01/raw_alff.nii",
        "output/sub-01/global_mean_alff.nii",
        "output/sub-01/z_score_alff.nii",
        "output/sub-01/raw_falff.nii",
        "output/sub-01/global_mean_falff.nii",
        "output/sub-01/z_score_falff.nii",
        "output/sub-01/z_score_reho.nii",
    }


def test_runtime_outputs_match_canonical_contract_across_subjects_and_scalings(
    base_lineage: ArtifactLineage,
) -> None:
    subjects = [
        SubjectManifestEntry(
            subject_id=subject_id,
            functional_files=[f"{subject_id}/func/rest.nii"],
        )
        for subject_id in ("sub-01", "sub-02")
    ]
    manifest, content = manifest_for(subjects)
    requested = request(base_lineage)
    assert requested.alff_falff is not None
    assert requested.reho is not None
    alff = requested.alff_falff.model_copy(update={"requested_scalings": tuple(MetricScaling)})
    reho = requested.reho.model_copy(update={"requested_scalings": tuple(MetricScaling)})
    plan = frozen_plan(manifest, parameters=common_parameters(), alff=alff, reho=reho)

    result = compile_fixture(plan, manifest, content)
    payload = result.spec.payload
    assert isinstance(payload, PreprocessingJobPayload)
    contract = enumerate_dpabi_preprocessing_outputs(
        tuple(subject.subject_id for subject in manifest.subjects), alff, reho
    )

    assert tuple(
        (item.subject_id, item.metric, item.scaling, item.relative_path) for item in payload.outputs
    ) == tuple(
        (
            item.subject_id,
            item.metric,
            item.scaling.value if item.scaling is not None else None,
            item.relative_path,
        )
        for item in contract
    )
    assert tuple(item.artifact_type for item in contract) == (
        "preprocessing.functional_timeseries",
        "metric.alff.raw",
        "metric.alff.global_mean",
        "metric.alff.z_score",
        "metric.falff.raw",
        "metric.falff.global_mean",
        "metric.falff.z_score",
        "metric.reho.raw",
        "metric.reho.global_mean",
        "metric.reho.z_score",
        "preprocessing.functional_timeseries",
        "metric.alff.raw",
        "metric.alff.global_mean",
        "metric.alff.z_score",
        "metric.falff.raw",
        "metric.falff.global_mean",
        "metric.falff.z_score",
        "metric.reho.raw",
        "metric.reho.global_mean",
        "metric.reho.z_score",
    )


def test_canonical_contract_includes_dpabi_auto_mask_artifacts(
    base_lineage: ArtifactLineage,
) -> None:
    requested = request(base_lineage)
    assert requested.alff_falff is not None
    alff = requested.alff_falff.model_copy(update={"requested_scalings": (MetricScaling.Z_SCORE,)})
    outputs = enumerate_dpabi_preprocessing_outputs(("sub-01",), alff, None, auto_mask=True)

    assert tuple(
        (item.metric, item.scaling, item.relative_path, item.artifact_type) for item in outputs
    ) == (
        (
            "timeseries",
            None,
            "output/sub-01/timeseries.nii",
            "preprocessing.functional_timeseries",
        ),
        ("brain_mask", None, "output/sub-01/brain_mask.nii", "preprocessing.brain_mask"),
        (
            "alff",
            MetricScaling.Z_SCORE,
            "output/sub-01/z_score_alff.nii",
            "metric.alff.z_score",
        ),
        (
            "falff",
            MetricScaling.Z_SCORE,
            "output/sub-01/z_score_falff.nii",
            "metric.falff.z_score",
        ),
    )

    duplicate_scalings = alff.model_copy(
        update={"requested_scalings": (MetricScaling.Z_SCORE, MetricScaling.Z_SCORE)}
    )
    with pytest.raises(ValueError, match="requested metric scalings must be unique"):
        enumerate_dpabi_preprocessing_outputs(("sub-01",), duplicate_scalings, None)


def test_metric_only_stages_registered_checkpoint_and_disables_earlier_steps(
    base_lineage: ArtifactLineage,
) -> None:
    manifest, content = manifest_for()
    requested = request(base_lineage)
    plan = frozen_plan(manifest, alff=requested.alff_falff, reho=requested.reho)
    result = compile_fixture(plan, manifest, content, metric_only=True)
    assert result.staging_plan is None
    assert result.parameters is None
    assert result.spec.artifact_bindings[-1].relative_path == "staging/FunImg/sub-01/verified.nii"
    payload = result.spec.payload
    assert isinstance(payload, PreprocessingJobPayload)
    cfg = payload.metric_projection.cfg
    assert all(
        cfg[field] == 0
        for field in (
            "IsSliceTiming",
            "IsRealign",
            "IsNormalize",
            "IsCovremove",
            "IsSegment",
            "IsDARTEL",
            "IsScrubbing",
            "RemoveFirstTimePoints",
            "IsNeedConvertFunDCM2IMG",
        )
    )
    assert cfg["IsFilter"] == 1  # Only the approved ReHo-specific temporal operation remains.


def test_tampered_plan_and_manifest_content_are_rejected() -> None:
    manifest, content = manifest_for()
    plan = frozen_plan(manifest, parameters=common_parameters())
    with pytest.raises(ValueError, match="SkillPlan content hash"):
        compile_fixture(plan.model_copy(update={"input_artifact_id": "other"}), manifest, content)
    with pytest.raises(ValueError, match="manifest content hash"):
        compile_fixture(plan, manifest, {**content, "file_sizes": {}})
    with pytest.raises(ValueError, match="manifest hash"):
        compile_fixture(plan, manifest.model_copy(update={"content_hash": "b" * 64}), content)


@pytest.mark.parametrize(
    "subjects",
    [
        [],
        [SubjectManifestEntry(subject_id="../escape", functional_files=["f.nii"])],
        [
            SubjectManifestEntry(
                subject_id="subject", session_id="one", functional_files=["f.nii"]
            ),
            SubjectManifestEntry(
                subject_id="subject", session_id="two", functional_files=["g.nii"]
            ),
        ],
        [
            SubjectManifestEntry(subject_id="one", session_id="one", functional_files=["f.nii"]),
            SubjectManifestEntry(subject_id="two", session_id="two", functional_files=["g.nii"]),
        ],
        [SubjectManifestEntry(subject_id="one", functional_files=["f.nii", "g.nii"])],
        [SubjectManifestEntry(subject_id="one", functional_files=["../f.nii"])],
        [
            SubjectManifestEntry(subject_id="SUB", functional_files=["f.nii"]),
            SubjectManifestEntry(subject_id="sub", functional_files=["g.nii"]),
        ],
    ],
)
def test_ambiguous_or_unsafe_subject_inputs_are_rejected(
    subjects: list[SubjectManifestEntry],
) -> None:
    manifest, content = manifest_for(subjects)
    plan = frozen_plan(manifest, parameters=common_parameters())
    with pytest.raises(ValueError):
        compile_fixture(plan, manifest, content)


@pytest.mark.parametrize("kind", [DatasetKind.DICOM, DatasetKind.MIXED, DatasetKind.UNKNOWN])
def test_non_nifti_manifests_require_explicit_conversion(kind: DatasetKind) -> None:
    manifest, content = manifest_for(kind=kind)
    plan = frozen_plan(manifest, parameters=common_parameters())
    with pytest.raises(ValueError, match="NIfTI input manifest"):
        compile_fixture(plan, manifest, content)


def test_external_cfg_is_rejected() -> None:
    manifest, content = manifest_for()
    plan = frozen_plan(manifest, parameters=common_parameters())
    with pytest.raises(ValueError, match="builtin base Cfg"):
        compile_fixture(
            rehash(plan.model_copy(update={"base_cfg_artifact_id": "cfg-other"})), manifest, content
        )


def test_wrong_mask_and_missing_checkpoint_are_rejected(base_lineage: ArtifactLineage) -> None:
    manifest, content = manifest_for()
    requested = request(base_lineage)
    plan = frozen_plan(manifest, reho=requested.reho)
    with pytest.raises(ValueError, match="checkpoint binding"):
        compile_fixture(plan, manifest, content)
    with pytest.raises(ValueError, match="same registered mask"):
        compile_preprocessing(
            plan,
            manifest,
            content,
            job_id="job-01",
            run_id="run-01",
            approval_record_id="approval-01",
            mask_binding=None,
        )


def test_unknown_effective_volume_count_and_result_normalization_are_rejected(
    base_lineage: ArtifactLineage,
) -> None:
    manifest, content = manifest_for()
    values = common_parameters().model_dump(mode="json")
    values["scrubbing"]["method"] = "cut"
    plan = frozen_plan(
        manifest,
        parameters=PreprocessingParameters.model_validate(values),
        reho=request(base_lineage).reho,
    )
    with pytest.raises(ValueError, match="CUT scrubbing"):
        compile_fixture(plan, manifest, content)
    values = common_parameters().model_dump(mode="json")
    values["normalization"]["timing"] = "on_results"
    values["nuisance"]["timing"] = "after_realign"
    plan = frozen_plan(
        manifest,
        parameters=PreprocessingParameters.model_validate(values),
        reho=request(base_lineage).reho,
    )
    with pytest.raises(ValueError, match="separate mask spaces"):
        compile_fixture(plan, manifest, content)


def test_compiled_staging_enforces_source_hashes_before_copy(tmp_path: Path) -> None:
    manifest, content = manifest_for()
    result = compile_fixture(
        frozen_plan(manifest, parameters=common_parameters()), manifest, content
    )
    assert result.staging_plan is not None
    source = tmp_path / "source"
    run = tmp_path / "run"
    run.mkdir()
    for operation in result.staging_plan.operations:
        path = source / operation.source_relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"different!!!")
    with pytest.raises(ValueError, match="source hash changed"):
        StagingCopyTool().execute(result.staging_plan, source_root=source, run_root=run)
    assert not list(run.rglob("*.nii"))
