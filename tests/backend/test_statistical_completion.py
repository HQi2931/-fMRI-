from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from unittest.mock import MagicMock

import nibabel as nib
import numpy as np
import pytest

from neuroagent.domain.fmri.statistics import FdrCorrection, GrfCorrection, StatisticalTest
from neuroagent.infrastructure.persistence.models import (
    ArtifactRow,
    PlanRevisionRow,
    ProjectRow,
    WorkflowRunRow,
)
from neuroagent.infrastructure.persistence.statistical_completion import (
    register_real_statistical_result,
)


def _design() -> dict[str, object]:
    subjects = ("sub-01", "sub-02", "sub-03")
    return {
        "revision_id": "design-1",
        "test": StatisticalTest.ONE_SAMPLE_T.value,
        "subject_order": subjects,
        "images": [
            {
                "subject_id": subject,
                "artifact_id": f"image-{index}",
                "group": None,
                "condition": None,
            }
            for index, subject in enumerate(subjects, start=1)
        ],
        "group_order": [],
        "condition_order": [],
        "covariates": [],
        "contrast": [1.0],
        "one_sample_baseline": 0.0,
        "mask_artifact_id": "mask-1",
        "tail": "two_sided",
        "missing_value_policy": "error",
        "qc_review_revision_id": "qc-1",
        "qc_review_hash": "a" * 64,
    }


def _artifacts(work_root, *, corrected_values: np.ndarray | None = None) -> list[ArtifactRow]:
    names = (
        "design_matrix",
        "contrast",
        "uncorrected_statistical_map",
        "effect_map",
        "cluster_table",
        "execution_log",
        "software_version_evidence",
    )
    if corrected_values is not None:
        names = (*names, "corrected_statistical_map", "correction_evidence")
    run_root = work_root / "run-1"
    run_root.mkdir(parents=True, exist_ok=True)
    rows = []
    for name in names:
        extension = ".nii" if name == "corrected_statistical_map" else ".dat"
        relative_path = f"output/{name}{extension}"
        target = run_root / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        content = (
            b"cluster\tpeak_x\tpeak_y\tpeak_z\tvoxels\tstatistic\n"
            if name == "cluster_table"
            else name.encode()
        )
        if name == "corrected_statistical_map":
            nib.save(nib.Nifti1Image(corrected_values, np.eye(4)), str(target))
            content = target.read_bytes()
        else:
            target.write_bytes(content)
        rows.append(
            ArtifactRow(
                artifact_id=f"artifact-{name}",
                project_id="project-1",
                run_id="run-1",
                artifact_type=f"statistics.{name}",
                relative_path=relative_path,
                checksum=hashlib.sha256(content).hexdigest(),
                size_bytes=len(content),
                provenance_json=json.dumps({"executor": "controlled_matlab"}),
            )
        )
    return rows


@pytest.mark.parametrize("space", [None, "native", "MNI152"])
@pytest.mark.parametrize("execution_files", ["legacy", "complete", "partial", "changed"])
def test_real_completion_stages_result_and_event_atomically(
    tmp_path, space, execution_files
) -> None:
    session = MagicMock()
    project = ProjectRow(
        project_id="project-1",
        name="test",
        source_roots_json="[]",
        work_root=str(tmp_path),
    )
    input_artifact = (
        ArtifactRow(
            artifact_id="image-1",
            project_id="project-1",
            run_id="source-run",
            artifact_type="metric.alff.z_score",
            relative_path="output/alff.nii",
            checksum="a" * 64,
            size_bytes=99,
            provenance_json=json.dumps({"lineage": {"space": space}}),
        )
        if space is not None
        else None
    )
    session.get.side_effect = lambda model, key: (
        project if model is ProjectRow else input_artifact if model is ArtifactRow else None
    )
    design = _design()
    run = WorkflowRunRow(
        run_id="run-1", project_id="project-1", plan_revision_id="plan-1", state="running"
    )
    plan = PlanRevisionRow(
        plan_revision_id="plan-1",
        project_id="project-1",
        revision=1,
        state="approved",
        plan_hash="c" * 64,
        manifest_hash="d" * 64,
        environment_hash="e" * 64,
        plan_json=json.dumps({"kind": "statistical_design", "design": design}),
    )
    artifacts = _artifacts(tmp_path)
    if execution_files != "legacy":
        for name in (
            "execution_script",
            "execution_bootstrap",
            "job_configuration",
            "input_manifest",
        ):
            relative_path = f"config/{name}.dat"
            target = tmp_path / "run-1" / relative_path
            target.parent.mkdir(exist_ok=True)
            content = name.encode()
            target.write_bytes(content)
            artifacts.append(
                ArtifactRow(
                    artifact_id=f"artifact-{name}",
                    project_id="project-1",
                    run_id="run-1",
                    artifact_type=f"statistics.{name}",
                    relative_path=relative_path,
                    checksum=hashlib.sha256(content).hexdigest(),
                    size_bytes=len(content),
                    provenance_json=json.dumps({"executor": "controlled_matlab"}),
                )
            )
        if execution_files == "partial":
            artifacts.pop()
        elif execution_files == "changed":
            (tmp_path / "run-1" / "config/execution_script.dat").write_bytes(b"modified")
    # A nonempty table proves the nested lineage supplies actual cluster space.
    table = next(row for row in artifacts if row.artifact_type == "statistics.cluster_table")
    table_path = tmp_path / "run-1" / table.relative_path
    table_path.write_bytes(
        b"cluster\tpeak_x\tpeak_y\tpeak_z\tvoxels\tstatistic\n1\t0\t0\t0\t1\t4\n"
    )
    table.checksum = hashlib.sha256(table_path.read_bytes()).hexdigest()
    table.size_bytes = table_path.stat().st_size
    process_log = tmp_path / "run-1" / "output" / "matlab.stdout.log"
    process_log.write_bytes(b"STATISTICS_COMPLETED")
    artifacts.append(
        ArtifactRow(
            artifact_id="artifact-process-log",
            project_id="project-1",
            run_id="run-1",
            artifact_type="execution.matlab_log",
            relative_path="output/matlab.stdout.log",
            checksum=hashlib.sha256(process_log.read_bytes()).hexdigest(),
            size_bytes=process_log.stat().st_size,
            provenance_json=json.dumps({"executor": "controlled_matlab"}),
        )
    )
    payload = {
        "plan_hash": plan.plan_hash,
        "input_manifest_hash": plan.manifest_hash,
        "statistical_design": design,
        "correction": None,
    }
    if execution_files in {"partial", "changed"}:
        with pytest.raises(ValueError):
            register_real_statistical_result(
                session,
                run=run,
                plan=plan,
                payload=payload,
                artifact_rows=artifacts,
                actor="worker-1",
                created_at=datetime.now(UTC),
            )
        session.add.assert_not_called()
        return
    result, event = register_real_statistical_result(
        session,
        run=run,
        plan=plan,
        payload=payload,
        artifact_rows=artifacts,
        actor="worker-1",
        created_at=datetime.now(UTC),
    )

    assert result.mode == "real"
    assert result.non_scientific is False
    assert json.loads(result.manifest_json)["mode"] == "real"
    roles = {row["role"] for row in json.loads(result.manifest_json)["artifacts"]}
    assert ("execution_script" in roles) == (execution_files == "complete")
    assert json.loads(result.manifest_json)["clusters"][0]["coordinate_space"] == (
        space or "unknown"
    )
    assert event.event_type == "StatisticalResultRegistered"
    assert session.add.call_count == 2


def test_corrected_empty_map_is_registered_as_no_significant_voxels(tmp_path) -> None:
    session = MagicMock()
    project = ProjectRow(
        project_id="project-1",
        name="test",
        source_roots_json="[]",
        work_root=str(tmp_path),
    )
    session.get.side_effect = lambda model, key: project if model is ProjectRow else None
    design = _design()
    run = WorkflowRunRow(
        run_id="run-1", project_id="project-1", plan_revision_id="plan-1", state="running"
    )
    plan = PlanRevisionRow(
        plan_revision_id="plan-1",
        project_id="project-1",
        revision=1,
        state="approved",
        plan_hash="c" * 64,
        manifest_hash="d" * 64,
        environment_hash="e" * 64,
        plan_json=json.dumps({"kind": "statistical_design", "design": design}),
    )

    result, _event = register_real_statistical_result(
        session,
        run=run,
        plan=plan,
        payload={
            "plan_hash": plan.plan_hash,
            "input_manifest_hash": plan.manifest_hash,
            "statistical_design": design,
            "correction": FdrCorrection(
                method="fdr",
                q_threshold=0.05,
                mask_artifact_id="mask-1",
                statistic_type="T",
                df1=2,
                df2=None,
            ).model_dump(mode="json"),
        },
        artifact_rows=_artifacts(tmp_path, corrected_values=np.zeros((2, 2, 2), dtype=np.float32)),
        actor="worker-1",
        created_at=datetime.now(UTC),
    )

    manifest = json.loads(result.manifest_json)
    assert manifest["inference_status"] == "no_significant_voxels"
    assert manifest["significant_voxel_count"] == 0
    assert manifest["clusters"] == []


def test_real_completion_fails_closed_when_a_required_role_is_missing(tmp_path) -> None:
    session = MagicMock()
    design = _design()
    run = WorkflowRunRow(
        run_id="run-1", project_id="project-1", plan_revision_id="plan-1", state="running"
    )
    plan = PlanRevisionRow(
        plan_revision_id="plan-1",
        project_id="project-1",
        revision=1,
        state="approved",
        plan_hash="c" * 64,
        manifest_hash="d" * 64,
        environment_hash="e" * 64,
        plan_json=json.dumps({"kind": "statistical_design", "design": design}),
    )
    with pytest.raises(ValueError, match="incomplete"):
        register_real_statistical_result(
            session,
            run=run,
            plan=plan,
            payload={
                "plan_hash": plan.plan_hash,
                "input_manifest_hash": plan.manifest_hash,
                "statistical_design": design,
                "correction": None,
            },
            artifact_rows=_artifacts(tmp_path)[:-1],
            actor="worker-1",
            created_at=datetime.now(UTC),
        )
    session.add.assert_not_called()


@pytest.mark.parametrize("missing", ["fdr_evidence", "grf_evidence", "grf_z"])
def test_partial_correction_cannot_be_registered_as_a_success(tmp_path, missing):
    session = MagicMock()
    design = _design()
    correction = (
        FdrCorrection(
            method="fdr",
            q_threshold=0.05,
            mask_artifact_id="mask-1",
            statistic_type="T",
            df1=2,
            df2=None,
        )
        if missing == "fdr_evidence"
        else GrfCorrection(
            method="grf",
            voxel_p_threshold=0.001,
            cluster_p_threshold=0.05,
            two_tailed=True,
            mask_artifact_id="mask-1",
            statistic_type="T",
            df1=2,
            df2=None,
            smoothness_mode="provided_dlh",
            smoothness_dlh=0.05,
        )
    )
    run = WorkflowRunRow(
        run_id="run-1", project_id="project-1", plan_revision_id="plan-1", state="running"
    )
    plan = PlanRevisionRow(
        plan_revision_id="plan-1",
        project_id="project-1",
        revision=1,
        state="approved",
        plan_hash="c" * 64,
        manifest_hash="d" * 64,
        environment_hash="e" * 64,
        plan_json=json.dumps({"kind": "statistical_design", "design": design}),
    )
    rows = _artifacts(tmp_path, corrected_values=np.zeros((2, 2, 2), dtype=np.float32))
    if missing.endswith("evidence"):
        rows = [row for row in rows if row.artifact_type != "statistics.correction_evidence"]
    with pytest.raises(ValueError, match="incomplete statistical correction"):
        register_real_statistical_result(
            session,
            run=run,
            plan=plan,
            payload={
                "plan_hash": plan.plan_hash,
                "input_manifest_hash": plan.manifest_hash,
                "statistical_design": design,
                "correction": correction.model_dump(mode="json"),
            },
            artifact_rows=rows,
            actor="worker",
            created_at=datetime.now(UTC),
        )
    session.add.assert_not_called()
