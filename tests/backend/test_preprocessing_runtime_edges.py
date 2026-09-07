from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from neuroagent.application.contracts import (
    ApprovalCreate,
    ApprovalDecision,
    ExecutionBackend,
    RunCreate,
    SkillPlanIntent,
    SkillPlanResolveRequest,
)
from neuroagent.domain.fmri.artifacts import ArtifactKind, ArtifactLineage
from neuroagent.execution.matlab import ControlledMatlabExecutor
from neuroagent.execution.models import MatlabJobResult, MatlabJobStatus
from neuroagent.infrastructure.matlab_executor import MatlabJobExecutor
from neuroagent.tools import input_staging
from neuroagent.workflow.runtime import WorkflowFactory

from .test_preprocessing_execution import _fake_execute, _prepare
from .test_science_agent_api import _mask_lineage, _skill_request


@pytest.fixture
def prepared(tmp_path):
    service, run, image = _prepare(tmp_path)
    try:
        yield service, run, image
    finally:
        service.close()


def _executor(service):
    return MatlabJobExecutor(
        service.repository,
        service.settings,
        environment_provider=service.environment_provider,
        workflow_factory=WorkflowFactory(service.skill_registry, service.tool_registry),
    )


def _payload(service, run):
    frozen = service.repository.get_plan(run.plan_revision_id)
    approval = service.repository.get_approved_plan_approval(run.plan_revision_id)
    return {
        "executor_type": "matlab_preprocessing",
        "real_execution_confirmed": True,
        "run_id": run.run_id,
        "job_id": "test-registered-job",
        "plan_hash": frozen.plan_hash,
        "input_manifest_hash": frozen.manifest_hash,
        "environment_hash": frozen.environment_hash,
        "approval_record_id": approval.approval_id,
        "workflow_plan": frozen.plan["skill_plan"],
    }


def _register_input(service, run, relative_path, data, lineage):
    project = service.get_project(run.project_id)
    path = Path(project.work_root) / run.run_id / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    service.repository.register_artifacts(
        project.project_id,
        run.run_id,
        (
            {
                "artifact_type": lineage.kind.value,
                "relative_path": relative_path,
                "checksum": hashlib.sha256(data).hexdigest(),
                "size_bytes": len(data),
                "provenance": {"lineage": lineage.model_dump(mode="json")},
            },
        ),
    )
    artifact = next(
        item for item in service.list_artifacts(run.run_id) if item.relative_path == relative_path
    )
    return artifact, path


def _metric_run(service, source_run, *, metric_only=False, smooth_results=False):
    frozen = service.repository.get_plan(source_run.plan_revision_id)
    manifest = service.get_manifest(frozen.plan["dataset_manifest_id"])
    mask, mask_path = _register_input(
        service,
        source_run,
        "output/registered-mask.nii",
        b"frozen-mask-data",
        _mask_lineage(manifest.content_hash),
    )
    input_data = _mask_lineage(manifest.content_hash).model_dump(mode="json")
    input_data.update(
        kind=ArtifactKind.FUNCTIONAL_TIMESERIES.value,
        subject_id="sub-01",
        tr_seconds=2,
        volume_count=120,
        mask_artifact_id=mask.artifact_id,
        mask_grid_signature="grid-3mm",
        space="MNI152-test-template",
        scrubbed=True,
    )
    checkpoint, checkpoint_path = _register_input(
        service,
        source_run,
        "output/registered-checkpoint.nii",
        b"frozen-functional-data",
        ArtifactLineage.model_validate(input_data),
    )
    intent = _skill_request(
        source_run.project_id, manifest.dataset_id, manifest.content_hash, checkpoint.artifact_id
    ).model_dump(mode="json")
    intent["alff_falff"]["mask_artifact_id"] = mask.artifact_id
    intent["reho"]["mask_artifact_id"] = mask.artifact_id
    if smooth_results:
        intent["alff_falff"].update(result_smoothing=True, result_smoothing_fwhm_mm=[6, 6, 6])
        intent["reho"].update(
            global_result_smoothing=True, global_result_smoothing_fwhm_mm=[6, 6, 6]
        )
    if not metric_only:
        parameters = dict(frozen.plan["skill_plan"]["resolved_parameters"])["preprocessing"]
        parameters = {
            **parameters,
            "temporal_filter": {
                "timing": "after_normalize",
                "frequency_band": {"low_hz": 0.01, "high_hz": 0.08},
                "add_mean_back": True,
            },
        }
        intent.update(request_preprocessing=True, preprocessing=parameters, input_artifact_id=None)
    plan = service.resolve_skill_plan(
        SkillPlanResolveRequest(
            request=SkillPlanIntent.model_validate(intent),
            expected_project_version=service.get_project(source_run.project_id).version,
        ),
        "metric-resolve",
    ).plan_revision
    service.approve_plan(
        plan.plan_revision_id,
        ApprovalCreate(
            expected_version=plan.version,
            plan_hash=plan.plan_hash,
            actor="edge-test",
            decision=ApprovalDecision.APPROVED,
            reason="Frozen synthetic runtime boundary test",
        ),
        "metric-approve",
    )
    run = service.create_run(
        RunCreate(
            project_id=source_run.project_id,
            plan_revision_id=plan.plan_revision_id,
            expected_plan_hash=plan.plan_hash,
            execution_backend=ExecutionBackend.MATLAB,
            real_execution_confirmed=True,
        ),
        "metric-run",
    )
    return run, mask, mask_path, checkpoint, checkpoint_path


def _fake_metric_execute(self, job, *, is_cancelled):
    root = self.dry_run(job).rendered.run_directory
    # The shared fake asserts the raw fixture filename. For a metric-only
    # job, the actual registered checkpoint has already been staged as input.nii.
    folder = root / "staging/FunImg/sub-01"
    if not (folder / "rest.nii").exists():
        assert (folder / "input.nii").is_file()
        (folder / "rest.nii").write_bytes((folder / "input.nii").read_bytes())
    return _fake_execute(self, job, is_cancelled=is_cancelled)


def test_retry_uses_fresh_staging_and_preserves_first_attempt_logs(prepared, monkeypatch):
    service, run, source = prepared
    original = source.read_bytes()
    roots = []

    def fail_then_succeed(self, job, *, is_cancelled):
        rendered = self.dry_run(job).rendered
        roots.append(rendered.run_directory)
        staged = rendered.run_directory / "staging/FunImg/sub-01/rest.nii"
        assert staged.read_bytes() == original
        if len(roots) == 1:
            staged.write_bytes(b"dummy-volumes-already-removed")
            (rendered.run_directory / "logs/matlab.log").write_text("preserved first failure")
            return MatlabJobResult(
                status=MatlabJobStatus.FAILED,
                job_hash=job.job_hash,
                exit_code=1,
                stdout="",
                stderr="simulated failure after dummy removal",
                rendered=rendered,
                registered_artifacts=(),
            )
        return _fake_execute(self, job, is_cancelled=is_cancelled)

    monkeypatch.setattr(ControlledMatlabExecutor, "execute", fail_then_succeed)
    executor = _executor(service)
    payload = _payload(service, run)
    assert executor.execute(payload, is_cancelled=lambda: False).status == "failed_retryable"
    result = executor.execute(payload, is_cancelled=lambda: False)
    assert result.status == "succeeded", result.error
    assert len(roots) == 2 and roots[0] != roots[1]
    assert (roots[0] / "logs/matlab.log").read_text() == "preserved first failure"
    assert source.read_bytes() == original
    assert all(
        artifact["relative_path"].startswith(f"attempts/{roots[1].name}/")
        for artifact in result.artifacts
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("real_execution_confirmed", False),
        ("approval_record_id", "unapproved"),
        ("workflow_plan", {}),
        ("plan_hash", "0" * 64),
        ("input_manifest_hash", "0" * 64),
    ],
)
def test_unconfirmed_or_changed_approval_payload_never_launches(
    prepared, monkeypatch, field, value
):
    service, run, _ = prepared

    def forbidden(*args, **kwargs):
        pytest.fail("Rejected approval payload must not reach MATLAB")

    monkeypatch.setattr(ControlledMatlabExecutor, "execute", forbidden)
    payload = {**_payload(service, run), field: value}
    result = _executor(service).execute(payload, is_cancelled=lambda: False)
    assert result.status == "failed_terminal"
    assert not result.artifacts


@pytest.mark.parametrize(
    "target,drift",
    [
        ("mask", "project"),
        ("mask", "hash"),
        ("input", "project"),
        ("input", "hash"),
    ],
)
def test_registered_mask_and_checkpoint_project_or_hash_drift_fail_closed(
    prepared,
    monkeypatch,
    target,
    drift,
):
    service, source_run, _ = prepared
    run, mask, mask_path, checkpoint, checkpoint_path = _metric_run(
        service, source_run, metric_only=True
    )
    artifact, path = (mask, mask_path) if target == "mask" else (checkpoint, checkpoint_path)
    if drift == "hash":
        path.write_bytes(b"changed-content".ljust(artifact.size_bytes, b"!"))
    else:
        original_get = service.repository.get_artifact

        def changed_project(artifact_id):
            value = original_get(artifact_id)
            return (
                value.model_copy(update={"project_id": "other-project"})
                if artifact_id == artifact.artifact_id
                else value
            )

        monkeypatch.setattr(service.repository, "get_artifact", changed_project)

    def forbidden(*args, **kwargs):
        pytest.fail("Changed registered inputs must not reach MATLAB")

    monkeypatch.setattr(ControlledMatlabExecutor, "execute", forbidden)
    result = _executor(service).execute(_payload(service, run), is_cancelled=lambda: False)
    assert result.status == "failed_terminal"
    assert not result.artifacts


@pytest.mark.parametrize("mutation", ["missing", "duplicate", "subject", "volume_count"])
def test_invalid_output_evidence_is_not_registered(prepared, monkeypatch, mutation):
    service, run, _ = prepared

    def invalid_evidence(self, job, *, is_cancelled):
        result = _fake_execute(self, job, is_cancelled=is_cancelled)
        path = result.rendered.run_directory / "output/preprocessing-evidence.json"
        evidence = json.loads(path.read_text())
        rows = evidence["outputs"]
        if mutation == "missing":
            evidence["outputs"] = []
        elif mutation == "duplicate":
            evidence["outputs"] = rows + rows
        elif mutation == "subject":
            rows[0]["subject_id"] = "sub-other"
        else:
            rows[0]["volume_count"] = 1
        path.write_text(json.dumps(evidence))
        return result

    monkeypatch.setattr(ControlledMatlabExecutor, "execute", invalid_evidence)
    result = _executor(service).execute(_payload(service, run), is_cancelled=lambda: False)
    assert result.status == "failed_terminal"
    assert "evidence validation failed" in result.error
    assert not result.artifacts


def test_registered_artifact_changed_during_copy_is_rejected(prepared, monkeypatch):
    service, source_run, _ = prepared
    run, _, mask_path, _, _ = _metric_run(service, source_run)
    original_copy = input_staging.shutil.copy2
    changed = []

    def change_before_copy(source, destination, *args, **kwargs):
        if Path(source) == mask_path:
            data = mask_path.read_bytes()
            mask_path.write_bytes(bytes(value ^ 1 for value in data))
            changed.append(mask_path)
        return original_copy(source, destination, *args, **kwargs)

    def forbidden(*args, **kwargs):
        pytest.fail("Copy-time input mutation must not reach MATLAB")

    monkeypatch.setattr(input_staging.shutil, "copy2", change_before_copy)
    monkeypatch.setattr(ControlledMatlabExecutor, "execute", forbidden)
    result = _executor(service).execute(_payload(service, run), is_cancelled=lambda: False)
    assert changed
    assert result.status == "failed_terminal"
    assert not result.artifacts


@pytest.mark.parametrize("metric_only", [False, True])
def test_checkpoint_mask_and_metric_only_space_scrubbing_are_preserved(
    prepared,
    monkeypatch,
    metric_only,
):
    service, source_run, _ = prepared
    run, mask, _, _, _ = _metric_run(service, source_run, metric_only=metric_only)
    monkeypatch.setattr(ControlledMatlabExecutor, "execute", _fake_metric_execute)
    result = _executor(service).execute(_payload(service, run), is_cancelled=lambda: False)
    assert result.status == "succeeded", result.error
    lineages = [
        artifact["provenance"]["lineage"]
        for artifact in result.artifacts
        if "lineage" in artifact["provenance"]
    ]
    assert len(lineages) == 4
    for lineage in lineages:
        assert lineage["mask_artifact_id"] == mask.artifact_id
        assert lineage["grid_signature"] == lineage["mask_grid_signature"]
        assert len(lineage["grid_signature"]) == 64
        assert lineage["space"] == ("MNI152-test-template" if metric_only else "native")
        assert lineage["scrubbed"] is metric_only
        assert lineage["producer_step_hash"] != result.output["job_hash"]
    producers = {lineage["kind"]: lineage["producer_step_hash"] for lineage in lineages}
    assert producers["alff_map"] == producers["falff_map"]
    assert producers["alff_map"] != producers["reho_map"]
    assert producers["functional_timeseries"] != producers["reho_map"]
    checkpoint = next(lineage for lineage in lineages if lineage["kind"] == "functional_timeseries")
    assert checkpoint["temporally_filtered"] is False


def test_metric_result_smoothing_does_not_mark_its_checkpoint_smoothed(prepared, monkeypatch):
    service, source_run, _ = prepared
    run, _, _, _, _ = _metric_run(service, source_run, metric_only=True, smooth_results=True)
    monkeypatch.setattr(ControlledMatlabExecutor, "execute", _fake_metric_execute)
    result = _executor(service).execute(_payload(service, run), is_cancelled=lambda: False)
    assert result.status == "succeeded", result.error
    lineages = [
        artifact["provenance"]["lineage"]
        for artifact in result.artifacts
        if "lineage" in artifact["provenance"]
    ]
    for lineage in lineages:
        if lineage["kind"] == "functional_timeseries":
            assert lineage["spatially_smoothed"] is False
            assert lineage["smoothing_fwhm_mm"] is None
        else:
            assert lineage["spatially_smoothed"] is True
            assert lineage["smoothing_fwhm_mm"] == [6, 6, 6]


def test_unverified_mask_is_rejected_before_matlab(prepared, monkeypatch):
    service, source_run, _ = prepared
    run, mask, _, _, _ = _metric_run(service, source_run)
    original = service.repository.get_artifact

    def unverified(artifact_id):
        artifact = original(artifact_id)
        if artifact_id != mask.artifact_id:
            return artifact
        lineage = ArtifactLineage.model_validate(artifact.provenance["lineage"]).model_copy(
            update={"metadata_verified": False}
        )
        return artifact.model_copy(
            update={"provenance": {"lineage": lineage.model_dump(mode="json")}}
        )

    monkeypatch.setattr(service.repository, "get_artifact", unverified)
    monkeypatch.setattr(
        ControlledMatlabExecutor,
        "execute",
        lambda *args, **kwargs: pytest.fail("unverified mask must not reach MATLAB"),
    )
    result = _executor(service).execute(_payload(service, run), is_cancelled=lambda: False)
    assert result.status == "failed_terminal"
    assert not result.artifacts
