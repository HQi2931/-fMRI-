from __future__ import annotations

import hashlib
from pathlib import Path
from types import SimpleNamespace

import nibabel as nib
import numpy as np
import pytest
from fastapi.testclient import TestClient

from neuroagent.api import create_app
from neuroagent.application.contracts.runs import (
    RunCreate,
)
from neuroagent.application.errors import ConflictError, NotFoundError
from neuroagent.application.services import ApplicationServices
from neuroagent.bootstrap import build_worker
from neuroagent.domain.fmri import (
    AnalysisImage,
    ClusterRecord,
    FdrCorrection,
    MissingValuePolicy,
    RegisteredArtifactMetadata,
    StatisticalArtifactRole,
    StatisticalDesignRevision,
    StatisticalMapType,
    StatisticalResultManifest,
    StatisticalResultMode,
    StatisticalTest,
    Tail,
)

from .conftest import make_approved_plan, make_project


def _synthetic_manifest(run_id: str) -> StatisticalResultManifest:
    return StatisticalResultManifest(
        result_id="synthetic-result-1",
        run_id=run_id,
        design_revision_id="synthetic-design-1",
        mode=StatisticalResultMode.SYNTHETIC_NON_SCIENTIFIC,
        non_scientific=True,
        non_scientific_reason="test fixture only; no statistical values are claimed",
        correction=FdrCorrection(
            method="fdr",
            q_threshold=0.05,
            mask_artifact_id="mask-1",
            statistic_type=StatisticalMapType.T,
            df1=2,
            df2=None,
        ),
        cluster_connectivity_definition="synthetic fixture only; no connectivity was applied",
        artifacts=tuple(
            RegisteredArtifactMetadata(
                artifact_id=f"placeholder-{role.value}",
                role=role,
                artifact_type=f"synthetic.non_scientific.{role.value}",
                relative_path=f"synthetic_non_scientific/{role.value}.placeholder",
                placeholder=True,
            )
            for role in StatisticalArtifactRole
        ),
        clusters=(),
    )


def _synthetic_design() -> StatisticalDesignRevision:
    subjects = ("sub-01", "sub-02", "sub-03")
    return StatisticalDesignRevision(
        revision_id="synthetic-design-1",
        test=StatisticalTest.ONE_SAMPLE_T,
        subject_order=subjects,
        images=tuple(
            AnalysisImage(
                subject_id=subject_id,
                artifact_id=f"metric-{subject_id}",
                group=None,
                condition=None,
            )
            for subject_id in subjects
        ),
        group_order=(),
        condition_order=(),
        covariates=(),
        contrast=(1.0,),
        one_sample_baseline=0.0,
        mask_artifact_id="mask-1",
        tail=Tail.TWO_SIDED,
        missing_value_policy=MissingValuePolicy.ERROR,
        qc_review_revision_id="qc-1",
        qc_review_hash="a" * 64,
    )


def _completed_run(
    service: ApplicationServices,
    project_id: str,
    plan_revision_id: str,
    plan_hash: str,
) -> str:
    run = service.runs.create_run(
        RunCreate(
            project_id=project_id,
            plan_revision_id=plan_revision_id,
            expected_plan_hash=plan_hash,
        ),
        "statistical-result-run-key",
    )
    assert build_worker(service, worker_id="result-worker").run_once() is True
    return run.run_id


def test_statistical_results_are_registered_and_queryable(
    service: ApplicationServices,
    source_root: Path,
    work_root: Path,
) -> None:
    project = make_project(service, source_root, work_root)
    plan = make_approved_plan(service, project.project_id)
    run_id = _completed_run(service, project.project_id, plan.plan_revision_id, plan.plan_hash)
    manifest = _synthetic_manifest(run_id)

    registered = service.runs.register_statistical_result(
        run_id=run_id,
        manifest=manifest,
        design=_synthetic_design(),
        correction=manifest.correction,
        qc_review_hash="a" * 64,
        environment_hash=plan.environment_hash,
        plan_hash=plan.plan_hash,
        actor="test-researcher",
    )
    assert registered.result_id == "synthetic-result-1"
    assert registered.non_scientific is True
    assert registered.artifact_count == len(StatisticalArtifactRole)
    assert registered.cluster_count == 0
    assert len(registered.bundle_hash) == 64

    listed = service.runs.list_statistical_results(project_id=project.project_id)
    assert [item.result_id for item in listed] == ["synthetic-result-1"]
    filtered = service.runs.list_statistical_results(project_id=project.project_id, run_id=run_id)
    assert [item.result_id for item in filtered] == ["synthetic-result-1"]
    assert (
        service.runs.list_statistical_results(project_id=project.project_id, run_id="missing-run")
        == []
    )

    detail = service.runs.get_statistical_result("synthetic-result-1")
    assert detail.manifest["mode"] == "synthetic_non_scientific"
    assert "SYNTHETIC / NON-SCIENTIFIC RESULT" in detail.report_markdown
    assert detail.report_json.startswith("{")

    # Identical re-registration is idempotent and returns the same record.
    repeated = service.runs.register_statistical_result(
        run_id=run_id,
        manifest=manifest,
        design=_synthetic_design(),
        correction=manifest.correction,
        qc_review_hash="a" * 64,
        environment_hash=plan.environment_hash,
        plan_hash=plan.plan_hash,
        actor="test-researcher",
    )
    assert repeated.result_id == registered.result_id


def test_statistical_result_registration_fails_closed_on_binding_mismatch(
    service: ApplicationServices,
    source_root: Path,
    work_root: Path,
) -> None:
    project = make_project(service, source_root, work_root)
    plan = make_approved_plan(service, project.project_id)
    run_id = _completed_run(service, project.project_id, plan.plan_revision_id, plan.plan_hash)
    manifest = _synthetic_manifest(run_id)

    with pytest.raises(ConflictError):
        service.runs.register_statistical_result(
            run_id=run_id,
            manifest=manifest,
            design=_synthetic_design(),
            correction=manifest.correction,
            qc_review_hash="a" * 64,
            environment_hash=plan.environment_hash,
            plan_hash="f" * 64,
            actor="test-researcher",
        )

    with pytest.raises(NotFoundError):
        service.runs.get_statistical_result("missing-result")

    with pytest.raises(NotFoundError):
        service.runs.list_statistical_results(project_id="missing-project")


def test_statistical_result_query_api_is_read_only(
    service: ApplicationServices,
    source_root: Path,
    work_root: Path,
) -> None:
    project = make_project(service, source_root, work_root)
    plan = make_approved_plan(service, project.project_id)
    run_id = _completed_run(service, project.project_id, plan.plan_revision_id, plan.plan_hash)
    manifest = _synthetic_manifest(run_id)
    service.runs.register_statistical_result(
        run_id=run_id,
        manifest=manifest,
        design=_synthetic_design(),
        correction=manifest.correction,
        qc_review_hash="a" * 64,
        environment_hash=plan.environment_hash,
        plan_hash=plan.plan_hash,
        actor="test-researcher",
    )

    with TestClient(create_app(service=service)) as client:
        listed = client.get(f"/api/v1/statistics/results?project_id={project.project_id}")
        assert listed.status_code == 200
        assert listed.json()[0]["result_id"] == "synthetic-result-1"

        detail = client.get("/api/v1/statistics/results/synthetic-result-1")
        assert detail.status_code == 200
        assert detail.json()["report_markdown"].startswith("# Statistical reproducibility report")

        missing = client.get("/api/v1/statistics/results/not-there")
        assert missing.status_code == 404
        assert missing.json()["error"]["code"] == "not_found"

        invalid = client.get("/api/v1/statistics/results")
        assert invalid.status_code == 422

        roi = client.get("/api/v1/statistics/results/synthetic-result-1/roi")
        assert roi.status_code == 409
        assert roi.json()["error"]["code"] == "synthetic_result_roi_blocked"


def test_real_result_slice_preview_links_cluster_to_affine_slice(
    service: ApplicationServices,
    source_root: Path,
    work_root: Path,
) -> None:
    project = make_project(service, source_root, work_root)
    plan = make_approved_plan(service, project.project_id)
    run_id = _completed_run(service, project.project_id, plan.plan_revision_id, plan.plan_hash)
    run_root = Path(project.work_root) / run_id
    map_path = run_root / "output" / "statistic_corrected.nii"
    map_path.parent.mkdir(parents=True)
    affine = np.diag([2.0, 2.0, 3.0, 1.0])
    nib.save(nib.Nifti1Image(np.arange(120, dtype=np.float32).reshape(5, 6, 4), affine), map_path)
    payload = map_path.read_bytes()
    checksum = hashlib.sha256(payload).hexdigest()
    service.repository.register_artifacts(
        project.project_id,
        run_id,
        (
            {
                "artifact_type": "statistics.corrected_statistical_map",
                "relative_path": "output/statistic_corrected.nii",
                "checksum": checksum,
                "size_bytes": len(payload),
                "provenance": {},
            },
        ),
    )
    artifact = next(
        item
        for item in service.runs.list_artifacts(run_id)
        if item.relative_path == "output/statistic_corrected.nii"
    )
    other_artifacts = tuple(
        RegisteredArtifactMetadata(
            artifact_id=f"fixture-{role.value}",
            role=role,
            artifact_type=f"statistics.{role.value}",
            relative_path=f"output/{role.value}.fixture",
            checksum_sha256="b" * 64,
            size_bytes=128,
            provenance_hash="c" * 64,
        )
        for role in StatisticalArtifactRole
        if role is not StatisticalArtifactRole.CORRECTED_STATISTICAL_MAP
    )
    corrected = RegisteredArtifactMetadata(
        artifact_id=artifact.artifact_id,
        role=StatisticalArtifactRole.CORRECTED_STATISTICAL_MAP,
        artifact_type=artifact.artifact_type,
        relative_path=artifact.relative_path,
        checksum_sha256=checksum,
        size_bytes=len(payload),
        provenance_hash="c" * 64,
    )
    design = _synthetic_design()
    manifest = StatisticalResultManifest(
        result_id="real-preview-result",
        run_id=run_id,
        design_revision_id=design.revision_id,
        mode=StatisticalResultMode.REAL,
        non_scientific=False,
        correction=_synthetic_manifest(run_id).correction,
        cluster_connectivity_definition="26-neighbor fixture map",
        artifacts=(*other_artifacts, corrected),
        clusters=(
            ClusterRecord(
                cluster_id="cluster-1",
                source_map_artifact_id=artifact.artifact_id,
                extent_voxels=2,
                peak_statistic=4.0,
                peak_coordinate_mm=(4.0, 6.0, 6.0),
                coordinate_space="MNI",
            ),
        ),
    )
    service.runs.register_statistical_result(
        run_id=run_id,
        manifest=manifest,
        design=design,
        correction=manifest.correction,
        qc_review_hash=design.qc_review_hash,
        environment_hash=plan.environment_hash,
        plan_hash=plan.plan_hash,
        actor="test-researcher",
    )

    with TestClient(create_app(service=service)) as client:
        info = client.get("/api/v1/statistics/results/real-preview-result/slice-info")
        assert info.status_code == 200
        assert info.json()["slice_count"] == 4
        assert info.json()["cluster_slices"] == [{"cluster_id": "cluster-1", "slice_index": 2}]
        preview = client.get(
            "/api/v1/statistics/results/real-preview-result/slice.png?slice_index=2"
        )
        assert preview.status_code == 200
        assert preview.headers["content-type"] == "image/png"
        assert preview.content.startswith(b"\x89PNG\r\n\x1a\n")
        out_of_range = client.get(
            "/api/v1/statistics/results/real-preview-result/slice.png?slice_index=4"
        )
        assert out_of_range.status_code == 422


def test_roi_query_uses_frozen_subject_images_and_rejects_changed_artifacts(
    service: ApplicationServices, source_root: Path, work_root: Path
) -> None:
    project = make_project(service, source_root, work_root)
    plan = make_approved_plan(service, project.project_id)
    run_id = _completed_run(service, project.project_id, plan.plan_revision_id, plan.plan_hash)
    root = Path(project.work_root) / run_id / "output"
    root.mkdir(parents=True)
    map_values = np.zeros((4, 4, 4), dtype=np.float32)
    map_values[1, 1, 1], map_values[2, 2, 2] = 4, 3
    specifications = [("statistic.nii", "statistics.corrected_statistical_map", map_values)]
    specifications.extend(
        (f"metric-{index}.nii", "metric.alff", np.full((4, 4, 4), value, dtype=np.float32))
        for index, value in enumerate((2, 5, 8))
    )
    records = []
    for name, artifact_type, values in specifications:
        path = root / name
        nib.save(nib.Nifti1Image(values, np.eye(4)), path)
        payload = path.read_bytes()
        records.append(
            {
                "artifact_type": artifact_type,
                "relative_path": f"output/{name}",
                "checksum": hashlib.sha256(payload).hexdigest(),
                "size_bytes": len(payload),
                "provenance": {},
            }
        )
    service.repository.register_artifacts(project.project_id, run_id, tuple(records))
    artifacts = {item.relative_path: item for item in service.runs.list_artifacts(run_id)}
    corrected = artifacts["output/statistic.nii"]
    original_design = _synthetic_design()
    design = original_design.model_copy(
        update={
            "images": tuple(
                image.model_copy(
                    update={"artifact_id": artifacts[f"output/metric-{index}.nii"].artifact_id}
                )
                for index, image in enumerate(original_design.images)
            )
        }
    )
    values = _synthetic_manifest(run_id).model_dump(mode="python")
    values.update(
        {
            "result_id": "roi-fixture",
            "mode": StatisticalResultMode.REAL,
            "non_scientific": False,
            "non_scientific_reason": None,
            "inference_status": None,
            "significant_voxel_count": None,
        }
    )
    values["artifacts"] = tuple(
        {
            **item,
            "artifact_type": f"statistics.{item['role'].value}",
            "placeholder": False,
            "checksum_sha256": "b" * 64,
            "provenance_hash": "c" * 64,
            "size_bytes": 128,
        }
        for item in values["artifacts"]
    )
    values["artifacts"] = tuple(
        item
        for item in values["artifacts"]
        if item["role"] != StatisticalArtifactRole.CORRECTED_STATISTICAL_MAP
    )
    values["artifacts"] += (
        RegisteredArtifactMetadata(
            artifact_id=corrected.artifact_id,
            role=StatisticalArtifactRole.CORRECTED_STATISTICAL_MAP,
            artifact_type=corrected.artifact_type,
            relative_path=corrected.relative_path,
            checksum_sha256=corrected.checksum,
            size_bytes=corrected.size_bytes,
            provenance_hash="c" * 64,
        ).model_dump(mode="python"),
    )
    values["clusters"] = (
        ClusterRecord(
            cluster_id="roi-1",
            source_map_artifact_id=corrected.artifact_id,
            extent_voxels=2,
            peak_statistic=4,
            peak_coordinate_mm=(1, 1, 1),
            coordinate_space="MNI",
            direction=1,
        ),
    )
    manifest = StatisticalResultManifest.model_validate(values)
    service.runs.register_statistical_result(
        run_id=run_id,
        manifest=manifest,
        design=design,
        correction=manifest.correction,
        qc_review_hash="a" * 64,
        environment_hash=plan.environment_hash,
        plan_hash=plan.plan_hash,
        actor="test-researcher",
    )
    with TestClient(create_app(service=service)) as client:
        response = client.get("/api/v1/statistics/results/roi-fixture/roi?cluster_id=roi-1")
        assert response.status_code == 200, response.text
        assert [row["mean_value"] for row in response.json()["rows"]] == [2, 5, 8]
        assert response.json()["status"] == "descriptive"
        assert "独立验证" in response.json()["limitation"]
        missing = client.get("/api/v1/statistics/results/roi-fixture/roi?cluster_id=missing")
        assert missing.status_code == 422
        (root / "metric-0.nii").write_bytes(b"changed")
        changed = client.get("/api/v1/statistics/results/roi-fixture/roi?cluster_id=roi-1")
        assert changed.status_code == 422
        assert changed.json()["error"]["code"] == "artifact_integrity_changed"


def test_empty_corrected_result_skips_atlas_space_checks_and_reports_no_localization(
    service: ApplicationServices,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    roles = (
        StatisticalArtifactRole.DESIGN_MATRIX,
        StatisticalArtifactRole.CONTRAST,
        StatisticalArtifactRole.UNCORRECTED_STATISTICAL_MAP,
        StatisticalArtifactRole.CORRECTED_STATISTICAL_MAP,
        StatisticalArtifactRole.EFFECT_MAP,
        StatisticalArtifactRole.CLUSTER_TABLE,
        StatisticalArtifactRole.EXECUTION_LOG,
        StatisticalArtifactRole.SOFTWARE_VERSION_EVIDENCE,
    )
    manifest = StatisticalResultManifest(
        result_id="empty-corrected-result",
        run_id="run-empty",
        design_revision_id="design-empty",
        mode=StatisticalResultMode.REAL,
        non_scientific=False,
        correction=FdrCorrection(
            method="fdr",
            q_threshold=0.05,
            mask_artifact_id="mask",
            statistic_type=StatisticalMapType.T,
            df1=2,
            df2=None,
        ),
        cluster_connectivity_definition="26-neighbor fixture map",
        artifacts=tuple(
            RegisteredArtifactMetadata(
                artifact_id=f"artifact-{role.value}",
                role=role,
                artifact_type=f"statistics.{role.value}",
                relative_path=f"output/{role.value}.nii",
                checksum_sha256="a" * 64,
                size_bytes=128,
                provenance_hash="b" * 64,
            )
            for role in roles
        ),
        clusters=(),
    )
    detail = SimpleNamespace(
        manifest=manifest.model_dump(mode="json"),
        bundle_hash="c" * 64,
    )
    monkeypatch.setattr(service.repository, "get_statistical_result", lambda _result_id: detail)

    localized = service.runs.localize_statistical_result("empty-corrected-result", "missing-atlas")

    assert localized.status == "no_significant_voxels"
    assert localized.significant_voxel_count == 0
    assert localized.atlas_id is None
    assert localized.atlas_name is None
    assert localized.coordinate_space is None
    assert localized.clusters == ()

    roi = service.runs.statistical_cluster_roi("empty-corrected-result", None)
    assert roi.status == "no_significant_voxels"
    assert roi.cluster_id is None
    assert roi.voxel_count == 0
    assert roi.rows == ()
