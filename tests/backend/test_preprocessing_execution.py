from __future__ import annotations

import hashlib
import json
import math
import os
import random
import struct
import tempfile
from dataclasses import replace
from pathlib import Path

import pytest

from neuroagent.application.contracts import (
    ApprovalCreate,
    ApprovalDecision,
    DatasetCreate,
    ExecutionBackend,
    ManifestScanRequest,
    ProjectCreate,
    RunCreate,
    SkillPlanIntent,
    SkillPlanResolveRequest,
)
from neuroagent.application.settings import Settings
from neuroagent.bootstrap import build_service, build_worker
from neuroagent.execution.matlab import ControlledMatlabExecutor
from neuroagent.execution.models import MatlabJobResult, MatlabJobStatus, VerifiedArtifact
from neuroagent.infrastructure import matlab_executor

from .test_environment import _fake_stack
from .test_science_agent_api import _mask_lineage, _minimal_preprocessing_payload, _skill_request


def _write_synthetic_series(path: Path) -> None:
    """Small NIfTI-1 fixture: 9x9x9, 124 volumes, millimetres and seconds."""
    path.parent.mkdir(parents=True, exist_ok=True)
    header = bytearray(352)
    struct.pack_into("<i", header, 0, 348)
    struct.pack_into("<8h", header, 40, 4, 9, 9, 9, 124, 1, 1, 1)
    struct.pack_into("<2h", header, 70, 16, 32)
    struct.pack_into("<8f", header, 76, 1, 3, 3, 3, 2, 1, 1, 1)
    struct.pack_into("<2f", header, 108, 352, 1)
    header[123] = 10
    struct.pack_into("<h", header, 254, 1)
    struct.pack_into("<12f", header, 280, 3, 0, 0, -12, 0, 3, 0, -12, 0, 0, 3, -12)
    header[344:348] = b"n+1\0"
    rng = random.Random(31415)
    with path.open("wb") as stream:
        stream.write(header)
        for time in range(124):
            values = [
                100 + time * 0.04 + (1 + index / 729) * math.sin(time * 0.3) + rng.gauss(0, 0.2)
                for index in range(729)
            ]
            stream.write(struct.pack("<729f", *values))


def _prepare(root: Path, *, real: bool = False):
    source = root / "source"
    source.mkdir(parents=True)
    image = source / "sub-01" / "func" / "rest.nii"
    _write_synthetic_series(image)
    settings = (
        _fake_stack(root / "fake", "preprocessing")
        if not real
        else Settings(
            matlab_executable=Path(os.environ["RSFMRI_MATLAB_EXECUTABLE"]),
            spm_dir=Path(os.environ["RSFMRI_SPM_DIR"]),
            dpabi_dir=Path(os.environ["RSFMRI_DPABI_DIR"]),
        )
    )
    settings = settings.model_copy(
        update={
            "database_url": f"sqlite:///{(root / 'metadata.db').as_posix()}",
            "allowed_source_roots": [source],
            "allowed_work_root": root / "work",
            "enable_real_execution": True,
        }
    )
    service = build_service(settings)
    project = service.create_project(
        ProjectCreate(
            name="Synthetic smoke",
            source_roots=[str(source)],
            work_root=str(root / "work" / "project"),
        ),
        "project",
    )
    dataset = service.create_dataset(
        project.project_id,
        DatasetCreate(
            expected_project_version=project.version,
            name="Synthetic series",
            source_path=str(source),
        ),
        "dataset",
    )
    manifest = service.inspect_dataset(
        dataset.dataset_id, ManifestScanRequest(expected_dataset_version=dataset.version), "scan"
    )
    params = _minimal_preprocessing_payload()
    params.update(expected_time_points=124, dummy_scans=4, detrend=True)
    resolved = service.resolve_skill_plan(
        SkillPlanResolveRequest(
            request=SkillPlanIntent(
                project_id=project.project_id,
                dataset_ref=dataset.dataset_id,
                input_manifest_hash=manifest.content_hash,
                requested_metrics=(),
                primary_outputs=(),
                study_protocol_ref="synthetic-software-smoke-only",
                request_preprocessing=True,
                preprocessing=params,
            ),
            expected_project_version=service.get_project(project.project_id).version,
        ),
        "resolve",
    )
    plan = resolved.plan_revision
    service.approve_plan(
        plan.plan_revision_id,
        ApprovalCreate(
            expected_version=plan.version,
            plan_hash=plan.plan_hash,
            actor="synthetic-smoke",
            decision=ApprovalDecision.APPROVED,
            reason="Deterministic synthetic software validation",
        ),
        "approve",
    )
    run = service.create_run(
        RunCreate(
            project_id=project.project_id,
            plan_revision_id=plan.plan_revision_id,
            expected_plan_hash=plan.plan_hash,
            execution_backend=ExecutionBackend.MATLAB,
            real_execution_confirmed=True,
        ),
        "run",
    )
    return service, run, image


def _fake_execute(self, job, *, is_cancelled):
    rendered = self.dry_run(job).rendered
    root = rendered.run_directory
    assert root.name == job.run_id and root.parent.name == "attempts"
    assert root.parents[2].name == "project"
    assert (root / "staging/FunImg/sub-01/rest.nii").is_file()
    rows = []
    artifacts = []
    for output in job.payload.outputs:
        rows.append(
            {
                "relative_path": output.relative_path,
                "subject_id": output.subject_id,
                "metric": output.metric,
                "tr_seconds": 2,
                "volume_count": 120,
                "metadata": {
                    "dimensions": [9, 9, 9],
                    "affine": [[3, 0, 0, 0], [0, 3, 0, 0], [0, 0, 3, 0], [0, 0, 0, 1]],
                    "voxel_size_mm": [3, 3, 3],
                },
            }
        )
    for expected in job.expected_artifacts:
        path = root / expected.relative_pattern
        path.parent.mkdir(parents=True, exist_ok=True)
        data = (
            json.dumps({"outputs": rows}).encode() if path.suffix == ".json" else b"verified-test"
        )
        path.write_bytes(data)
        artifacts.append(
            VerifiedArtifact(
                artifact_type=expected.artifact_type,
                relative_path=expected.relative_pattern,
                size_bytes=len(data),
                sha256=hashlib.sha256(data).hexdigest(),
            )
        )
    return MatlabJobResult(
        status=MatlabJobStatus.SUCCEEDED,
        job_hash=job.job_hash,
        exit_code=0,
        stdout="",
        stderr="",
        rendered=rendered,
        registered_artifacts=tuple(artifacts),
    )


def test_public_preprocessing_stages_and_registers_actual_metadata(tmp_path, monkeypatch):
    service, run, source = _prepare(tmp_path)
    before = source.read_bytes()
    monkeypatch.setattr(ControlledMatlabExecutor, "execute", _fake_execute)
    try:
        assert build_worker(service).run_once()
        result = service.get_run(run.run_id)
        assert result.state.value == "qc_review", result.model_dump_json()
        outputs = service.list_artifacts(run.run_id)
        image = next(
            item for item in outputs if item.artifact_type.endswith("functional_timeseries")
        )
        lineage = image.provenance["lineage"]
        assert lineage["volume_count"] == 120 and lineage["metadata_verified"]
        assert lineage["subject_id"] == "sub-01"
        assert lineage["artifact_id"] == image.artifact_id
        assert source.read_bytes() == before
    finally:
        service.close()


def test_preprocessing_source_drift_never_starts_matlab(tmp_path, monkeypatch):
    service, run, source = _prepare(tmp_path)
    source.write_bytes(b"changed")

    def forbidden(*args, **kwargs):
        pytest.fail("MATLAB must not start with changed source data")

    monkeypatch.setattr(ControlledMatlabExecutor, "execute", forbidden)
    try:
        assert build_worker(service).run_once()
        assert service.get_run(run.run_id).state.value == "failed_terminal"
        assert not service.list_artifacts(run.run_id)
    finally:
        service.close()


@pytest.mark.matlab_smoke
@pytest.mark.skipif(
    os.environ.get("RSFMRI_RUN_PREPROCESSING_SMOKE") != "1",
    reason="requires explicit local synthetic MATLAB smoke opt-in",
)
def test_real_preprocessing_smoke(monkeypatch):
    compile_job = matlab_executor.compile_preprocessing

    def bounded_compile(*args, **kwargs):
        compiled = compile_job(*args, **kwargs)
        return replace(compiled, spec=compiled.spec.model_copy(update={"timeout_seconds": 180}))

    monkeypatch.setattr(matlab_executor, "compile_preprocessing", bounded_compile)
    root = Path(tempfile.mkdtemp(prefix="rsfmri-preprocessing-smoke-"))
    service, run, source = _prepare(root, real=True)
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    print(f"SYNTHETIC_SMOKE_DIRECTORY={root}")
    try:
        assert build_worker(service).run_once()
        result = service.get_run(run.run_id)
        assert result.state.value == "qc_review", result.model_dump_json()
        artifacts = service.list_artifacts(run.run_id)
        image = next(
            item for item in artifacts if item.artifact_type.endswith("functional_timeseries")
        )
        assert image.provenance["lineage"]["volume_count"] == 120
        assert hashlib.sha256(source.read_bytes()).hexdigest() == digest
        print(
            json.dumps(
                {
                    "status": "pass",
                    "artifact_count": len(artifacts),
                    "volume_count": 120,
                    "source_unchanged": True,
                }
            )
        )
        _run_combined_smoke(service, run, source)
    finally:
        service.close()


def _run_combined_smoke(service, source_run, source):
    project = service.get_project(source_run.project_id)
    source_plan = service.repository.get_plan(source_run.plan_revision_id)
    manifest = service.get_manifest(source_plan.plan["dataset_manifest_id"])
    registered = service.list_artifacts(source_run.run_id)
    timeseries = next(
        item for item in registered if item.artifact_type.endswith("functional_timeseries")
    )
    mask_path = Path(project.work_root) / source_run.run_id / "output" / "synthetic-mask.nii"
    mask_path.parent.mkdir(parents=True, exist_ok=True)
    header = bytearray(source.read_bytes()[:352])
    struct.pack_into("<8h", header, 40, 3, 9, 9, 9, 1, 1, 1, 1)
    # Keep the full grid; all data are synthetic and every voxel has finite variance.
    data = bytes(header) + struct.pack("<729f", *([1] * 729))
    mask_path.write_bytes(data)
    lineage = _mask_lineage(manifest.content_hash).model_copy(
        update={
            "grid_signature": timeseries.provenance["lineage"]["grid_signature"],
            "space": "native",
            "metadata_evidence_hash": hashlib.sha256(data).hexdigest(),
        }
    )
    service.repository.register_artifacts(
        project.project_id,
        source_run.run_id,
        (
            {
                "artifact_type": "brain_mask",
                "relative_path": "output/synthetic-mask.nii",
                "checksum": hashlib.sha256(data).hexdigest(),
                "size_bytes": len(data),
                "provenance": {"lineage": lineage.model_dump(mode="json")},
            },
        ),
    )
    mask = next(
        item
        for item in service.list_artifacts(source_run.run_id)
        if item.artifact_type == "brain_mask"
    )
    intent = _skill_request(
        project.project_id, manifest.dataset_id, manifest.content_hash, timeseries.artifact_id
    ).model_dump(mode="json")
    params = _minimal_preprocessing_payload()
    params.update(expected_time_points=124, dummy_scans=4, detrend=True)
    params["temporal_filter"] = {
        "timing": "after_normalize",
        "frequency_band": {"low_hz": 0.01, "high_hz": 0.08},
        "add_mean_back": True,
    }
    intent.update(request_preprocessing=True, preprocessing=params, input_artifact_id=None)
    intent["alff_falff"]["mask_artifact_id"] = mask.artifact_id
    intent["reho"]["mask_artifact_id"] = mask.artifact_id
    resolved = service.resolve_skill_plan(
        SkillPlanResolveRequest(
            request=SkillPlanIntent.model_validate(intent),
            expected_project_version=service.get_project(project.project_id).version,
        ),
        "combined-resolve",
    )
    plan = resolved.plan_revision
    service.approve_plan(
        plan.plan_revision_id,
        ApprovalCreate(
            expected_version=plan.version,
            plan_hash=plan.plan_hash,
            actor="synthetic-smoke",
            decision=ApprovalDecision.APPROVED,
            reason="Synthetic ALFF fALFF ReHo pipeline smoke",
        ),
        "combined-approve",
    )
    run = service.create_run(
        RunCreate(
            project_id=project.project_id,
            plan_revision_id=plan.plan_revision_id,
            expected_plan_hash=plan.plan_hash,
            execution_backend=ExecutionBackend.MATLAB,
            real_execution_confirmed=True,
        ),
        "combined-run",
    )
    assert build_worker(service).run_once()
    result = service.get_run(run.run_id)
    assert result.state.value == "qc_review", result.model_dump_json()
    artifacts = service.list_artifacts(run.run_id)
    for metric in ("alff", "falff", "reho"):
        artifact = next(
            item for item in artifacts if item.artifact_type == f"metric.{metric}.z_score"
        )
        assert artifact.provenance["lineage"]["volume_count"] == 120
        assert artifact.provenance["lineage"]["mask_artifact_id"] == mask.artifact_id
    print(json.dumps({"combined_metrics": "pass", "artifact_count": len(artifacts)}))
