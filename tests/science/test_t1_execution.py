"""Synthetic scientific contracts; these tests never launch MATLAB."""

import pytest
from pydantic import ValidationError

from neuroagent.domain.fmri.preprocessing import PreprocessingParameters, T1Parameters
from neuroagent.execution.preprocessing import ReviewedT1Binding, compile_preprocessing
from neuroagent.tools.dpabi_v82 import DpabiPreprocessingRequest, DpabiV82Adapter
from tests.science.conftest import preprocessing_parameters
from tests.science.test_preprocessing_compilation import frozen_plan, manifest_for


def t1_parameters(manifest, *, dartel=True, normalize=False):
    values = preprocessing_parameters().model_dump(mode="json")
    values["scrubbing"]["method"] = "nearest"
    values["nuisance"]["timing"] = "after_realign"
    values["normalization"] = {
        "mode": 3 if normalize else 0,
        "timing": "on_functional_data" if normalize else None,
        "bounding_box_mm": [[-90, -126, -72], [90, 90, 108]] if normalize else None,
        "voxel_size_mm": [3, 3, 3] if normalize else None,
        "structural_artifact_id": f"manifest:{manifest.manifest_id}:anatomical-source"
        if normalize
        else None,
        "affine_regularization": "mni" if normalize else None,
    }
    values["t1"] = {
        "preparation": "reorient_crop",
        "structural_artifact_id": f"manifest:{manifest.manifest_id}:anatomical-source",
        "coregister_to_functional": True,
        "segmentation": "new_segment" if dartel else "segment",
        "affine_regularization": "mni",
        "dartel": dartel,
    }
    values["provenance"].append(
        {"name": "t1", "source": "user", "evidence_ref": "synthetic-choice"}
    )
    return PreprocessingParameters.model_validate(values)


def compile_t1(parameters, manifest, content, **kwargs):
    return compile_preprocessing(
        frozen_plan(manifest, parameters=parameters),
        manifest,
        content,
        job_id="job-t1",
        run_id="run-t1",
        approval_record_id="approval-t1",
        mask_binding=None,
        **kwargs,
    )


def test_legacy_serialization_has_no_new_field():
    original = preprocessing_parameters()
    assert "t1" not in original.model_dump(mode="json")
    assert (
        PreprocessingParameters.model_validate(original.model_dump()).model_dump()
        == original.model_dump()
    )


def test_dartel_requires_new_segment():
    with pytest.raises(ValidationError, match="DARTEL requires New Segment"):
        T1Parameters(
            preparation="reorient_crop",
            structural_artifact_id="structural",
            coregister_to_functional=True,
            segmentation="segment",
            affine_regularization="mni",
            dartel=True,
        )


def test_segment_projects_without_normalization():
    manifest, _ = manifest_for()
    parameters = t1_parameters(manifest, dartel=False)
    request = DpabiPreprocessingRequest(
        subject_ids=("sub-01",),
        functional_session_number=1,
        starting_dir_name="FunImg",
        input_manifest_hash=manifest.content_hash,
        parameters=parameters,
    )
    cfg = DpabiV82Adapter().project_preprocessing_cfg(request).cfg
    assert cfg["IsSegment"] == 1
    assert cfg["IsNormalize"] == 0
    assert cfg["IsAllowGUI"] == 0


def test_preparation_has_only_structural_outputs_and_no_downstream_flags():
    manifest, content = manifest_for()
    compilation = compile_t1(t1_parameters(manifest), manifest, content, preparation_only=True)
    payload = compilation.spec.payload
    assert payload.preparation_only and not payload.outputs
    for flag in ("IsSegment", "IsDARTEL", "IsNormalize", "IsRealign", "IsCovremove"):
        assert payload.metric_projection.cfg[flag] == 0
    roles = {artifact.artifact_type for artifact in compilation.spec.expected_artifacts}
    assert {"preprocessing.t1_prepared", "preprocessing.t1_review_image"} <= roles


def test_reviewed_cohort_binds_real_output_names_and_all_dartel_artifacts():
    manifest, content = manifest_for()
    parameters = t1_parameters(manifest, normalize=True)
    binding = ReviewedT1Binding(
        subject_id="sub-01",
        artifact_id="prepared-1",
        relative_path="attempts/attempt-1/output/t1/sub-01/coT1.nii",
        sha256="b" * 64,
        size_bytes=4096,
        source_manifest_hash=manifest.content_hash,
        review_record_id="review-1",
        preparation_run_id="prep-run",
    )
    compilation = compile_t1(parameters, manifest, content, reviewed_t1_bindings=(binding,))
    assert compilation.spec.payload.metric_projection.cfg["IsDARTEL"] == 1
    assert any(
        a.artifact_type == "preprocessing.dartel_template"
        for a in compilation.spec.expected_artifacts
    )
    assert not any(
        "/T1Img/" in op.destination_relative_path for op in compilation.staging_plan.operations
    )
    with pytest.raises(ValueError, match="genuine preparation"):
        compile_t1(
            parameters,
            manifest,
            content,
            reviewed_t1_bindings=(binding.model_copy(update={"relative_path": "plain.nii"}),),
        )


def test_integrated_t1_has_no_intermediate_review_requirement():
    manifest, content = manifest_for()
    compilation = compile_t1(t1_parameters(manifest, normalize=True), manifest, content)
    assert compilation.spec.payload.t1_preparation == "reorient_crop"
    assert not compilation.spec.payload.preparation_only
    assert any(
        "/T1Img/" in op.destination_relative_path for op in compilation.staging_plan.operations
    )
    assert compilation.spec.payload.metric_projection.cfg["IsNeedReorientCropT1Img"] == 0
    assert all(
        not item.required
        for item in compilation.spec.expected_artifacts
        if item.artifact_type == "preprocessing.t1_review_image"
    )
