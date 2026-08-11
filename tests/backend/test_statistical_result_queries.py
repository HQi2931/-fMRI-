from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from neuroagent.api import create_app
from neuroagent.application.contracts import RunCreate
from neuroagent.application.errors import ConflictError, NotFoundError
from neuroagent.application.services import NeuroAgentService
from neuroagent.bootstrap import build_worker
from neuroagent.domain.fmri import (
    AnalysisImage,
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
    service: NeuroAgentService,
    project_id: str,
    plan_revision_id: str,
    plan_hash: str,
) -> str:
    run = service.create_run(
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
    service: NeuroAgentService,
    source_root: Path,
    work_root: Path,
) -> None:
    project = make_project(service, source_root, work_root)
    plan = make_approved_plan(service, project.project_id)
    run_id = _completed_run(service, project.project_id, plan.plan_revision_id, plan.plan_hash)
    manifest = _synthetic_manifest(run_id)

    registered = service.register_statistical_result(
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

    listed = service.list_statistical_results(project_id=project.project_id)
    assert [item.result_id for item in listed] == ["synthetic-result-1"]
    filtered = service.list_statistical_results(project_id=project.project_id, run_id=run_id)
    assert [item.result_id for item in filtered] == ["synthetic-result-1"]
    assert (
        service.list_statistical_results(project_id=project.project_id, run_id="missing-run") == []
    )

    detail = service.get_statistical_result("synthetic-result-1")
    assert detail.manifest["mode"] == "synthetic_non_scientific"
    assert "SYNTHETIC / NON-SCIENTIFIC RESULT" in detail.report_markdown
    assert detail.report_json.startswith("{")

    # Identical re-registration is idempotent and returns the same record.
    repeated = service.register_statistical_result(
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
    service: NeuroAgentService,
    source_root: Path,
    work_root: Path,
) -> None:
    project = make_project(service, source_root, work_root)
    plan = make_approved_plan(service, project.project_id)
    run_id = _completed_run(service, project.project_id, plan.plan_revision_id, plan.plan_hash)
    manifest = _synthetic_manifest(run_id)

    with pytest.raises(ConflictError):
        service.register_statistical_result(
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
        service.get_statistical_result("missing-result")

    with pytest.raises(NotFoundError):
        service.list_statistical_results(project_id="missing-project")


def test_statistical_result_query_api_is_read_only(
    service: NeuroAgentService,
    source_root: Path,
    work_root: Path,
) -> None:
    project = make_project(service, source_root, work_root)
    plan = make_approved_plan(service, project.project_id)
    run_id = _completed_run(service, project.project_id, plan.plan_revision_id, plan.plan_hash)
    manifest = _synthetic_manifest(run_id)
    service.register_statistical_result(
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
