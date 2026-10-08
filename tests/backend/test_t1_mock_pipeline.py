"""Approved automatic T1 workflow, with optional inspection and no interim approval."""

import hashlib

import pytest

from neuroagent.application.contracts import (
    ApprovalCreate,
    ApprovalDecision,
    DatasetCreate,
    ManifestScanRequest,
    QcExecutionMode,
    RunCreate,
    SkillPlanIntent,
    SkillPlanResolveRequest,
    WorkflowState,
)
from neuroagent.bootstrap import build_worker

from .conftest import make_bids_dataset, make_project
from .test_science_agent_api import _minimal_preprocessing_payload


@pytest.fixture
def approved_t1_plan(service, source_root, work_root):
    source = make_bids_dataset(source_root)
    before = {
        p.relative_to(source).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in source.rglob("*")
        if p.is_file()
    }
    project = make_project(service, source_root, work_root)
    dataset = service.create_dataset(
        project.project_id,
        DatasetCreate(
            expected_project_version=project.version, name="Synthetic T1", source_path=str(source)
        ),
        "dataset-create",
    )
    manifest = service.inspect_dataset(
        dataset.dataset_id,
        ManifestScanRequest(expected_dataset_version=dataset.version),
        "manifest-create",
    )
    parameters = _minimal_preprocessing_payload()
    parameters["realignment"] = {"enabled": True, "options_source": "dpabi_v82_jobmat"}
    parameters["provenance"].append(
        {"name": "t1", "source": "study_protocol", "evidence_ref": "protocol://t1"}
    )
    parameters["t1"] = {
        "preparation": "reorient_crop",
        "structural_artifact_id": f"manifest:{manifest.manifest_id}:anatomical-source",
        "coregister_to_functional": True,
        "segmentation": "new_segment",
        "affine_regularization": "mni",
        "dartel": True,
    }
    parameters["normalization"] = {
        "mode": 3,
        "timing": "on_functional_data",
        "bounding_box_mm": [[-90, -126, -72], [90, 90, 108]],
        "voxel_size_mm": [3, 3, 3],
        "structural_artifact_id": f"manifest:{manifest.manifest_id}:anatomical-source",
        "affine_regularization": "mni",
    }
    resolved = service.resolve_skill_plan(
        SkillPlanResolveRequest(
            request=SkillPlanIntent(
                project_id=project.project_id,
                dataset_ref=dataset.dataset_id,
                input_manifest_hash=manifest.content_hash,
                requested_metrics=(),
                primary_outputs=(),
                request_preprocessing=True,
                preprocessing=parameters,
            ),
            expected_project_version=service.get_project(project.project_id).version,
        ),
        "resolve-t1",
    )
    plan = resolved.plan_revision
    service.approve_plan(
        plan.plan_revision_id,
        ApprovalCreate(
            expected_version=plan.version,
            plan_hash=plan.plan_hash,
            actor="tester",
            decision=ApprovalDecision.APPROVED,
            reason="synthetic test",
        ),
        "approve-t1",
    )
    return project, plan, manifest, source, before


def test_automatic_t1_mock_pipeline_requires_no_interim_review(service, approved_t1_plan):
    project, plan, manifest, source, before = approved_t1_plan
    request = RunCreate(
        project_id=project.project_id,
        plan_revision_id=plan.plan_revision_id,
        expected_plan_hash=plan.plan_hash,
        qc_mode=QcExecutionMode.SKIP,
    )
    run = service.create_run(request, "automatic-t1-run")
    worker = build_worker(service)
    assert worker.run_once()
    completed = service.get_run(run.run_id)
    assert completed.state is WorkflowState.SUCCEEDED
    artifacts = service.repository.list_artifacts(run.run_id)
    anatomy = [a for a in artifacts if a.artifact_type == "preprocessing.t1_prepared"]
    assert len(anatomy) == len(manifest.subjects)
    assert all(a.provenance["non_scientific"] is True for a in anatomy)
    assert {
        p.relative_to(source).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in source.rglob("*")
        if p.is_file()
    } == before
    assert service.repository.get_plan(plan.plan_revision_id).plan_hash == plan.plan_hash
    assert not any(
        e.event_type == "T1PreparationReviewed" for e in service.repository.list_events(run.run_id)
    )
