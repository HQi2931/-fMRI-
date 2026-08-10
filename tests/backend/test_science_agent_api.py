from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from neuroagent.agent.models import (
    AgentSummaryPurpose,
    AgentTaskRequest,
    ModelCapability,
    ProviderResponse,
    TaskType,
)
from neuroagent.agent.providers import MockProvider, RetryableProviderError
from neuroagent.api import create_app
from neuroagent.application.contracts import (
    AgentTaskCreate,
    DatasetCreate,
    DatasetKind,
    DatasetProfile,
    ManifestRevisionView,
    ManifestScanRequest,
    ModelProfileInput,
    ProviderTestRequest,
    QcReviewApprove,
    QcReviewCreate,
    RunCreate,
    SkillPlanIntent,
    SkillPlanResolveRequest,
    StatisticalDesignCreate,
    SubjectManifestEntry,
)
from neuroagent.application.errors import ConflictError, InputValidationError
from neuroagent.application.services import NeuroAgentService
from neuroagent.application.settings import Settings
from neuroagent.bootstrap import build_service, build_worker
from neuroagent.domain.fmri import (
    AlffFalffParameters,
    AnalysisImage,
    ArtifactKind,
    ArtifactLineage,
    FdrCorrection,
    FrequencyBand,
    MetricKind,
    MetricScaling,
    MissingValuePolicy,
    ParameterProvenance,
    ParameterSource,
    QcCheck,
    QcSeverity,
    RehoParameters,
    StatisticalDesignRevision,
    StatisticalMapType,
    StatisticalTest,
    Tail,
    TemporalFilterTiming,
)

from .conftest import make_approved_plan, make_bids_dataset, make_project


def _provenance(*names: str) -> tuple[ParameterProvenance, ...]:
    return tuple(
        ParameterProvenance(
            name=name,
            source=ParameterSource.STUDY_PROTOCOL,
            evidence_ref=f"protocol://{name}",
        )
        for name in names
    )


def _lineage(
    *,
    manifest_hash: str = "a" * 64,
    artifact_id: str = "functional-001",
    smoothed: bool = False,
) -> ArtifactLineage:
    return ArtifactLineage(
        artifact_id=artifact_id,
        kind=ArtifactKind.FUNCTIONAL_TIMESERIES,
        metadata_verified=True,
        tr_seconds=2.0,
        volume_count=120,
        metadata_evidence_hash="e" * 64,
        subject_manifest_hash=manifest_hash,
        space="MNI152",
        grid_signature="grid-3mm",
        voxel_size_mm=(3.0, 3.0, 3.0),
        mask_artifact_id="mask-001",
        mask_grid_signature="grid-3mm",
        temporally_filtered=False,
        frequency_band=None,
        spatially_smoothed=smoothed,
        smoothing_fwhm_mm=(6.0, 6.0, 6.0) if smoothed else None,
        scrubbed=False,
        producer_step_hash="b" * 64,
    )


def _mask_lineage(manifest_hash: str) -> ArtifactLineage:
    return ArtifactLineage(
        artifact_id="server-binds-id",
        kind=ArtifactKind.BRAIN_MASK,
        metadata_verified=True,
        metadata_evidence_hash="f" * 64,
        subject_manifest_hash=manifest_hash,
        space="MNI152",
        grid_signature="grid-3mm",
        voxel_size_mm=(3.0, 3.0, 3.0),
        mask_artifact_id=None,
        mask_grid_signature=None,
        temporally_filtered=False,
        frequency_band=None,
        spatially_smoothed=False,
        smoothing_fwhm_mm=None,
        scrubbed=False,
        producer_step_hash="c" * 64,
    )


def _metric_lineage(
    manifest_hash: str,
    subject_id: str,
    mask_artifact_id: str,
    *,
    session_id: str = "ses-01",
    condition: str | None = None,
) -> ArtifactLineage:
    return ArtifactLineage(
        artifact_id="server-binds-id",
        kind=ArtifactKind.ALFF_MAP,
        subject_id=subject_id,
        session_id=session_id,
        condition=condition,
        metric_scaling=MetricScaling.Z_SCORE,
        metadata_verified=True,
        tr_seconds=2.0,
        volume_count=120,
        metadata_evidence_hash="1" * 64,
        subject_manifest_hash=manifest_hash,
        space="MNI152",
        grid_signature="grid-3mm",
        voxel_size_mm=(3.0, 3.0, 3.0),
        mask_artifact_id=mask_artifact_id,
        mask_grid_signature="grid-3mm",
        temporally_filtered=True,
        frequency_band=FrequencyBand(low_hz=0.01, high_hz=0.08),
        spatially_smoothed=False,
        smoothing_fwhm_mm=None,
        scrubbed=False,
        producer_step_hash="d" * 64,
    )


def _skill_request(
    project_id: str,
    dataset_id: str,
    manifest_hash: str,
    input_artifact_id: str,
) -> SkillPlanIntent:
    return SkillPlanIntent(
        project_id=project_id,
        dataset_ref=dataset_id,
        input_manifest_hash=manifest_hash,
        requested_metrics=(MetricKind.ALFF, MetricKind.FALFF, MetricKind.REHO),
        primary_outputs=("zALFF", "zfALFF", "zReHo"),
        input_artifact_id=input_artifact_id,
        alff_falff=AlffFalffParameters(
            tr_seconds=2.0,
            frequency_band=FrequencyBand(low_hz=0.01, high_hz=0.08),
            requested_metrics=(MetricKind.ALFF, MetricKind.FALFF),
            requested_scalings=(MetricScaling.Z_SCORE,),
            mask_artifact_id="mask-001",
            filter_timing=TemporalFilterTiming.AFTER_NORMALIZE,
            result_smoothing=False,
            result_smoothing_fwhm_mm=None,
            provenance=_provenance(
                "tr_seconds",
                "frequency_band",
                "requested_metrics",
                "requested_scalings",
                "filter_timing",
                "result_smoothing",
            ),
        ),
        reho=RehoParameters(
            tr_seconds=2.0,
            temporal_filter_band=FrequencyBand(low_hz=0.01, high_hz=0.08),
            temporal_filter_add_mean_back=True,
            cluster_voxels=27,
            mask_artifact_id="mask-001",
            requested_scalings=(MetricScaling.Z_SCORE,),
            smooth_reho=False,
            smooth_reho_fwhm_mm=None,
            global_result_smoothing=False,
            global_result_smoothing_fwhm_mm=None,
            provenance=_provenance(
                "tr_seconds",
                "temporal_filter_band",
                "temporal_filter_add_mean_back",
                "cluster_voxels",
                "requested_scalings",
                "smooth_reho",
                "global_result_smoothing",
            ),
        ),
        study_protocol_ref="protocol-001",
    )


def _one_sample_design(
    qc_review_revision_id: str,
    qc_review_hash: str,
    image_artifact_ids: tuple[str, str, str],
    mask_artifact_id: str,
) -> StatisticalDesignRevision:
    subjects = ("sub-01", "sub-02", "sub-03")
    return StatisticalDesignRevision(
        revision_id="statistics-001",
        test=StatisticalTest.ONE_SAMPLE_T,
        subject_order=subjects,
        images=tuple(
            AnalysisImage(
                subject_id=subject_id,
                artifact_id=artifact_id,
                group=None,
                condition=None,
            )
            for subject_id, artifact_id in zip(subjects, image_artifact_ids, strict=True)
        ),
        group_order=(),
        condition_order=(),
        covariates=(),
        contrast=(1.0,),
        one_sample_baseline=0.0,
        mask_artifact_id=mask_artifact_id,
        tail=Tail.TWO_SIDED,
        missing_value_policy=MissingValuePolicy.ERROR,
        qc_review_revision_id=qc_review_revision_id,
        qc_review_hash=qc_review_hash,
    )


def _recommendation(summary: str, proposed: dict[str, object] | None = None) -> str:
    return json.dumps(
        {
            "summary": summary,
            "proposed_skill_request": proposed,
            "warnings": [],
            "unresolved_questions": [],
            "requires_user_confirmation": True,
        }
    )


def _minimal_preprocessing_payload() -> dict[str, object]:
    provenance_names = (
        "tr_seconds",
        "expected_time_points",
        "dummy_scans",
        "slice_timing",
        "realignment",
        "nuisance",
        "normalization",
        "detrend",
        "temporal_filter",
        "scrubbing",
        "smoothing",
    )
    return {
        "tr_seconds": 2.0,
        "expected_time_points": 120,
        "dummy_scans": 0,
        "slice_timing": {
            "enabled": False,
            "slice_count": None,
            "slice_order": None,
            "reference_slice": None,
        },
        "realignment": {"enabled": False, "options_source": None},
        "nuisance": {
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
        },
        "normalization": {
            "mode": 0,
            "timing": None,
            "bounding_box_mm": None,
            "voxel_size_mm": None,
            "structural_artifact_id": None,
            "affine_regularization": None,
        },
        "detrend": False,
        "temporal_filter": {
            "timing": "disabled",
            "frequency_band": None,
            "add_mean_back": None,
        },
        "scrubbing": {
            "enabled": False,
            "timing": None,
            "censoring": None,
            "method": None,
        },
        "smoothing": {"timing": "disabled", "method": None, "fwhm_mm": None},
        "provenance": [
            {
                "name": name,
                "source": "study_protocol",
                "evidence_ref": f"protocol://{name}",
            }
            for name in provenance_names
        ],
    }


def test_skill_registry_resolution_and_blockers_are_api_enforced(
    service: NeuroAgentService,
    source_root: Path,
    work_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = make_project(service, source_root, work_root)
    dataset_path = make_bids_dataset(source_root)
    dataset = service.create_dataset(
        project.project_id,
        DatasetCreate(
            name="Synthetic BIDS",
            source_path=str(dataset_path),
            expected_project_version=project.version,
        ),
        "skill-source-dataset",
    )
    manifest = service.inspect_dataset(
        dataset.dataset_id,
        ManifestScanRequest(expected_dataset_version=dataset.version),
        "skill-source-manifest",
    )
    source_plan = make_approved_plan(
        service,
        project.project_id,
        manifest_hash=manifest.content_hash,
    )
    source_run = service.create_run(
        RunCreate(
            project_id=project.project_id,
            plan_revision_id=source_plan.plan_revision_id,
            expected_plan_hash=source_plan.plan_hash,
        ),
        "skill-source-run",
    )
    assert build_worker(service, worker_id="skill-source-worker").run_once() is True
    service.repository.register_artifacts(
        project.project_id,
        source_run.run_id,
        tuple(
            {
                "artifact_type": f"timeseries.synthetic.{label}",
                "relative_path": f"output/{label}.nii.gz",
                "checksum": checksum * 64,
                "size_bytes": 1,
                "provenance": {
                    "lineage": _lineage(
                        manifest_hash=manifest.content_hash,
                        artifact_id="server-binds-id",
                        smoothed=smoothed,
                    ).model_dump(mode="json")
                },
            }
            for label, checksum, smoothed in (
                ("unsmoothed", "7", False),
                ("smoothed", "8", True),
            )
        ),
    )
    registered = service.list_artifacts(source_run.run_id)
    unsmoothed = next(
        item for item in registered if item.artifact_type == "timeseries.synthetic.unsmoothed"
    )
    smoothed = next(
        item for item in registered if item.artifact_type == "timeseries.synthetic.smoothed"
    )
    project = service.get_project(project.project_id)
    request = SkillPlanResolveRequest(
        request=_skill_request(
            project.project_id,
            dataset.dataset_id,
            manifest.content_hash,
            unsmoothed.artifact_id,
        ),
        expected_project_version=project.version,
    )
    with TestClient(create_app(service=service)) as client:
        skills = client.get("/api/v1/skills")
        assert skills.status_code == 200
        assert "rsfmri.pipeline.alff_reho_combined" in {item["skill_id"] for item in skills.json()}

        initial_preprocessing = client.post(
            "/api/v1/skill-plans/resolve",
            json={
                "request": {
                    "project_id": project.project_id,
                    "dataset_ref": dataset.dataset_id,
                    "input_manifest_hash": manifest.content_hash,
                    "requested_metrics": [],
                    "primary_outputs": [],
                    "study_protocol_ref": "protocol-initial-preprocessing",
                    "request_preprocessing": True,
                    "preprocessing": _minimal_preprocessing_payload(),
                },
                "expected_project_version": project.version,
            },
            headers={"Idempotency-Key": "resolve-initial-preprocessing"},
        )
        assert initial_preprocessing.status_code == 201
        initial_plan = initial_preprocessing.json()["skill_plan"]
        assert initial_plan["input_artifact_id"].startswith("manifest:")
        assert initial_plan["base_cfg_artifact_id"].startswith("builtin:dpabi-v82-base-cfg:")

        preprocessing_metric_plan = request.model_dump(mode="json")
        preprocessing_metric_plan["request"]["input_artifact_id"] = None
        preprocessing_metric_plan["request"]["request_preprocessing"] = True
        preprocessing_payload = _minimal_preprocessing_payload()
        preprocessing_payload["temporal_filter"] = {
            "timing": "after_normalize",
            "frequency_band": {"low_hz": 0.01, "high_hz": 0.08},
            "add_mean_back": True,
        }
        preprocessing_metric_plan["request"]["preprocessing"] = preprocessing_payload
        preprocessing_metric = client.post(
            "/api/v1/skill-plans/resolve",
            json=preprocessing_metric_plan,
            headers={"Idempotency-Key": "resolve-verified-checkpoint-metric"},
        )
        assert preprocessing_metric.status_code == 201
        preprocessing_steps = preprocessing_metric.json()["skill_plan"]["steps"]
        step_ids = [step["step_id"] for step in preprocessing_steps]
        assert (
            step_ids.index("preprocess_common")
            < step_ids.index("verify_preprocessed_metadata")
            < step_ids.index("calculate_alff_falff")
        )
        alff_step = next(
            step for step in preprocessing_steps if step["step_id"] == "calculate_alff_falff"
        )
        assert alff_step["consumes"] == ["timeseries.verified.unfiltered.unsmoothed"]

        forged_environment = request.model_dump(mode="json")
        forged_environment["environment"] = {
            "matlab_version": "forged",
            "spm_version": "forged",
            "dpabi_version": "forged",
            "adapter_version": "forged",
            "environment_hash": "0" * 64,
        }
        rejected_environment = client.post(
            "/api/v1/skill-plans/resolve",
            json=forged_environment,
            headers={"Idempotency-Key": "reject-client-environment"},
        )
        assert rejected_environment.status_code == 422

        response = client.post(
            "/api/v1/skill-plans/resolve",
            json=request.model_dump(mode="json"),
            headers={"Idempotency-Key": "resolve-skill-001"},
        )
        assert response.status_code == 201
        payload = response.json()
        assert payload["plan_revision"]["state"] == "awaiting_approval"
        assert payload["skill_plan"]["plan_hash"] == payload["plan_revision"]["plan_hash"]

        repeated = client.post(
            "/api/v1/skill-plans/resolve",
            json=request.model_dump(mode="json"),
            headers={"Idempotency-Key": "resolve-skill-001"},
        )
        assert repeated.json() == payload

        with service.database.session_factory() as session:
            count_before = session.scalar(text("SELECT COUNT(*) FROM plan_revisions"))
        blocked = request.model_copy(
            update={
                "request": _skill_request(
                    project.project_id,
                    dataset.dataset_id,
                    manifest.content_hash,
                    smoothed.artifact_id,
                )
            }
        )
        rejected = client.post(
            "/api/v1/skill-plans/resolve",
            json=blocked.model_dump(mode="json"),
            headers={"Idempotency-Key": "resolve-skill-blocked"},
        )
        assert rejected.status_code == 422
        assert rejected.json()["error"]["code"] == "skill_plan_blocked"
        issue_codes = {item["code"] for item in rejected.json()["error"]["details"]["issues"]}
        assert "REHO_INPUT_SPATIALLY_SMOOTHED" in issue_codes
        with service.database.session_factory() as session:
            assert session.scalar(text("SELECT COUNT(*) FROM plan_revisions")) == count_before
        bypass = client.post(
            "/api/v1/plan-revisions",
            json={
                "project_id": project.project_id,
                "expected_project_version": project.version,
                "plan": {"steps": []},
                "manifest_hash": "a" * 64,
                "environment_hash": "e" * 64,
            },
            headers={"Idempotency-Key": "generic-plan-bypass"},
        )
        assert bypass.status_code == 404

        approval = client.post(
            f"/api/v1/plan-revisions/{payload['plan_revision']['plan_revision_id']}/approve",
            json={
                "expected_version": payload["plan_revision"]["version"],
                "plan_hash": payload["plan_revision"]["plan_hash"],
                "actor": "researcher",
                "decision": "approved",
                "reason": "Synthetic Skill plan reviewed",
            },
            headers={"Idempotency-Key": "approve-skill-before-stale"},
        )
        assert approval.status_code == 201

        with service.database.session_factory() as session:
            runs_before_race = session.scalar(text("SELECT COUNT(*) FROM workflow_runs"))
            jobs_before_race = session.scalar(text("SELECT COUNT(*) FROM jobs"))
        original_require_current = service._require_plan_current
        checked_once = False

        def advance_manifest_after_first_check(
            plan,
            **options,  # type: ignore[no-untyped-def]
        ):
            nonlocal checked_once
            original_require_current(plan, **options)
            if plan.plan_revision_id != payload["plan_revision"]["plan_revision_id"]:
                return
            if checked_once:
                return
            checked_once = True
            current_dataset = service.get_dataset(dataset.dataset_id)
            service.inspect_dataset(
                dataset.dataset_id,
                ManifestScanRequest(expected_dataset_version=current_dataset.version),
                "manifest-race-after-run-prepare",
            )

        with monkeypatch.context() as race_patch:
            race_patch.setattr(service, "_require_plan_current", advance_manifest_after_first_check)
            raced_run = client.post(
                "/api/v1/runs",
                json={
                    "project_id": project.project_id,
                    "plan_revision_id": payload["plan_revision"]["plan_revision_id"],
                    "expected_plan_hash": payload["plan_revision"]["plan_hash"],
                },
                headers={"Idempotency-Key": "manifest-race-run"},
            )
        assert checked_once is True
        assert raced_run.status_code == 409
        assert raced_run.json()["error"]["code"] == "plan_stale"
        assert {reason["code"] for reason in raced_run.json()["error"]["details"]["reasons"]} >= {
            "dataset_manifest_revision_changed"
        }
        with service.database.session_factory() as session:
            assert session.scalar(text("SELECT COUNT(*) FROM workflow_runs")) == runs_before_race
            assert session.scalar(text("SELECT COUNT(*) FROM jobs")) == jobs_before_race

        changed_environment_service = build_service(
            service.settings.model_copy(update={"adapter_version": "1.0.1"}),
            database=service.database,
            repository=service.repository,
            providers={},
        )
        with TestClient(create_app(service=changed_environment_service)) as changed_client:
            stale_environment = changed_client.post(
                "/api/v1/runs",
                json={
                    "project_id": project.project_id,
                    "plan_revision_id": payload["plan_revision"]["plan_revision_id"],
                    "expected_plan_hash": payload["plan_revision"]["plan_hash"],
                },
                headers={"Idempotency-Key": "stale-environment-run"},
            )
        assert stale_environment.status_code == 409
        assert stale_environment.json()["error"]["code"] == "plan_stale"
        assert {
            reason["code"] for reason in stale_environment.json()["error"]["details"]["reasons"]
        } >= {"environment_lock_changed"}

        (dataset_path / "dataset_description.json").write_text(
            '{"Name":"synthetic-changed","BIDSVersion":"1.9.0"}',
            encoding="utf-8",
        )
        source_changed = client.post(
            "/api/v1/runs",
            json={
                "project_id": project.project_id,
                "plan_revision_id": payload["plan_revision"]["plan_revision_id"],
                "expected_plan_hash": payload["plan_revision"]["plan_hash"],
            },
            headers={"Idempotency-Key": "stale-source-content-run"},
        )
        assert source_changed.status_code == 409
        assert source_changed.json()["error"]["code"] == "plan_stale"
        assert {
            reason["code"] for reason in source_changed.json()["error"]["details"]["reasons"]
        } >= {"source_manifest_content_changed"}

        current_dataset = service.get_dataset(dataset.dataset_id)
        service.inspect_dataset(
            dataset.dataset_id,
            ManifestScanRequest(expected_dataset_version=current_dataset.version),
            "skill-source-new-manifest",
        )
        stale_run = client.post(
            "/api/v1/runs",
            json={
                "project_id": project.project_id,
                "plan_revision_id": payload["plan_revision"]["plan_revision_id"],
                "expected_plan_hash": payload["plan_revision"]["plan_hash"],
            },
            headers={"Idempotency-Key": "stale-skill-run"},
        )
        assert stale_run.status_code == 409
        assert stale_run.json()["error"]["code"] == "plan_stale"
        assert {reason["code"] for reason in stale_run.json()["error"]["details"]["reasons"]} >= {
            "dataset_manifest_revision_changed"
        }


def test_skill_resolution_binds_required_manifest_inputs(
    service: NeuroAgentService, source_root: Path, work_root: Path
) -> None:
    project = make_project(service, source_root, work_root)
    dataset_path = make_bids_dataset(source_root)
    missing_functional_path = dataset_path / "sub-02" / "func" / "sub-02_task-rest_bold.nii.gz"
    missing_functional_path.unlink()
    dataset = service.create_dataset(
        project.project_id,
        DatasetCreate(
            name="Input contract BIDS",
            source_path=str(dataset_path),
            expected_project_version=project.version,
        ),
        "input-contract-dataset",
    )
    manifest = service.inspect_dataset(
        dataset.dataset_id,
        ManifestScanRequest(expected_dataset_version=dataset.version),
        "input-contract-missing-functional",
    )
    project = service.get_project(project.project_id)

    def resolve_payload(
        manifest_hash: str,
        preprocessing: dict[str, object],
    ) -> dict[str, object]:
        return {
            "request": {
                "project_id": project.project_id,
                "dataset_ref": dataset.dataset_id,
                "input_manifest_hash": manifest_hash,
                "requested_metrics": [],
                "primary_outputs": [],
                "study_protocol_ref": "protocol-input-contract",
                "request_preprocessing": True,
                "preprocessing": preprocessing,
            },
            "expected_project_version": project.version,
        }

    with TestClient(create_app(service=service)) as client:
        missing_functional = client.post(
            "/api/v1/skill-plans/resolve",
            json=resolve_payload(manifest.content_hash, _minimal_preprocessing_payload()),
            headers={"Idempotency-Key": "missing-functional-input"},
        )
        assert missing_functional.status_code == 422
        assert missing_functional.json()["error"]["code"] == "manifest_functional_input_missing"

        missing_functional_path.write_bytes(b"synthetic-sub-02-bold")
        missing_anatomical_path = dataset_path / "sub-02" / "anat" / "sub-02_T1w.nii.gz"
        missing_anatomical_path.unlink()
        current_dataset = service.get_dataset(dataset.dataset_id)
        anatomical_manifest = service.inspect_dataset(
            dataset.dataset_id,
            ManifestScanRequest(expected_dataset_version=current_dataset.version),
            "input-contract-missing-anatomical",
        )
        structural = _minimal_preprocessing_payload()
        structural["normalization"] = {
            "mode": 2,
            "timing": "on_functional_data",
            "bounding_box_mm": [[-90.0, -126.0, -72.0], [90.0, 90.0, 108.0]],
            "voxel_size_mm": [3.0, 3.0, 3.0],
            "structural_artifact_id": (
                f"manifest:{anatomical_manifest.manifest_id}:anatomical-source"
            ),
            "affine_regularization": "mni",
        }
        missing_anatomical = client.post(
            "/api/v1/skill-plans/resolve",
            json=resolve_payload(anatomical_manifest.content_hash, structural),
            headers={"Idempotency-Key": "missing-anatomical-input"},
        )
        assert missing_anatomical.status_code == 422
        assert missing_anatomical.json()["error"]["code"] == "manifest_anatomical_input_missing"
        missing_details = missing_anatomical.json()["error"]["details"]["missing"]
        assert missing_details == [
            {
                "subject_id": "sub-02",
                "session_id": None,
                "candidate_relative_paths": [],
            }
        ]

        missing_anatomical_path.write_bytes(b"synthetic-sub-02-t1")
        duplicate_anatomical_path = (
            dataset_path / "sub-01" / "anat" / "sub-01_acq-repeat_T1w.nii.gz"
        )
        duplicate_anatomical_path.write_bytes(b"synthetic-sub-01-repeat-t1")
        current_dataset = service.get_dataset(dataset.dataset_id)
        ambiguous_manifest = service.inspect_dataset(
            dataset.dataset_id,
            ManifestScanRequest(expected_dataset_version=current_dataset.version),
            "input-contract-ambiguous-anatomical",
        )
        ambiguous_structural = _minimal_preprocessing_payload()
        ambiguous_structural["normalization"] = {
            "mode": 3,
            "timing": "on_functional_data",
            "bounding_box_mm": [[-90.0, -126.0, -72.0], [90.0, 90.0, 108.0]],
            "voxel_size_mm": [3.0, 3.0, 3.0],
            "structural_artifact_id": (
                f"manifest:{ambiguous_manifest.manifest_id}:anatomical-source"
            ),
            "affine_regularization": "mni",
        }
        ambiguous = client.post(
            "/api/v1/skill-plans/resolve",
            json=resolve_payload(ambiguous_manifest.content_hash, ambiguous_structural),
            headers={"Idempotency-Key": "ambiguous-anatomical-input"},
        )
        assert ambiguous.status_code == 422
        assert ambiguous.json()["error"]["code"] == "manifest_anatomical_input_ambiguous"
        candidates = ambiguous.json()["error"]["details"]["ambiguous"][0][
            "candidate_relative_paths"
        ]
        assert candidates == [
            "sub-01/anat/sub-01_T1w.nii.gz",
            "sub-01/anat/sub-01_acq-repeat_T1w.nii.gz",
        ]

        duplicate_anatomical_path.unlink()
        current_dataset = service.get_dataset(dataset.dataset_id)
        complete_manifest = service.inspect_dataset(
            dataset.dataset_id,
            ManifestScanRequest(expected_dataset_version=current_dataset.version),
            "input-contract-complete",
        )
        forged_structural = _minimal_preprocessing_payload()
        forged_structural["normalization"] = {
            "mode": 2,
            "timing": "on_functional_data",
            "bounding_box_mm": [[-90.0, -126.0, -72.0], [90.0, 90.0, 108.0]],
            "voxel_size_mm": [3.0, 3.0, 3.0],
            "structural_artifact_id": "client-forged-t1",
            "affine_regularization": "mni",
        }
        forged = client.post(
            "/api/v1/skill-plans/resolve",
            json=resolve_payload(complete_manifest.content_hash, forged_structural),
            headers={"Idempotency-Key": "forged-structural-input"},
        )
        assert forged.status_code == 422
        assert forged.json()["error"]["code"] == "structural_artifact_binding_invalid"


def test_skill_resolution_rejects_ambiguous_bids_bold_runs(
    service: NeuroAgentService, source_root: Path, work_root: Path
) -> None:
    project = make_project(service, source_root, work_root, key="multi-run-project")
    dataset_path = make_bids_dataset(source_root)
    second_run = dataset_path / "sub-01" / "func" / "sub-01_task-rest_run-02_bold.nii.gz"
    second_run.write_bytes(b"synthetic-sub-01-run-02")
    dataset = service.create_dataset(
        project.project_id,
        DatasetCreate(
            name="Multi-run BIDS",
            source_path=str(dataset_path),
            expected_project_version=project.version,
        ),
        "multi-run-dataset",
    )
    manifest = service.inspect_dataset(
        dataset.dataset_id,
        ManifestScanRequest(expected_dataset_version=dataset.version),
        "multi-run-manifest",
    )
    assert any("多个 BOLD run" in warning for warning in manifest.profile.warnings)
    project = service.get_project(project.project_id)

    with TestClient(create_app(service=service)) as client:
        response = client.post(
            "/api/v1/skill-plans/resolve",
            json={
                "request": {
                    "project_id": project.project_id,
                    "dataset_ref": dataset.dataset_id,
                    "input_manifest_hash": manifest.content_hash,
                    "requested_metrics": [],
                    "primary_outputs": [],
                    "study_protocol_ref": "protocol-multi-run",
                    "request_preprocessing": True,
                    "preprocessing": _minimal_preprocessing_payload(),
                },
                "expected_project_version": project.version,
            },
            headers={"Idempotency-Key": "multi-run-plan"},
        )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "manifest_functional_input_ambiguous"
    assert response.json()["error"]["details"]["ambiguous"] == [
        {
            "subject_id": "sub-01",
            "session_id": None,
            "candidate_relative_paths": [
                "sub-01/func/sub-01_task-rest_bold.nii.gz",
                "sub-01/func/sub-01_task-rest_run-02_bold.nii.gz",
            ],
        }
    ]


def test_derivative_only_bids_cannot_fall_back_to_plain_functional_input(
    service: NeuroAgentService, source_root: Path, work_root: Path
) -> None:
    project = make_project(service, source_root, work_root, key="derivative-only-project")
    dataset_path = source_root / "derivative-only-bids"
    dataset_path.mkdir()
    (dataset_path / "dataset_description.json").write_text(
        '{"Name":"derivative-only","BIDSVersion":"1.9.0"}',
        encoding="utf-8",
    )
    derivative = (
        dataset_path
        / "derivatives"
        / "pipeline"
        / "sub-01"
        / "func"
        / "sub-01_task-rest_desc-preproc_bold.nii.gz"
    )
    derivative.parent.mkdir(parents=True)
    derivative.write_bytes(b"derived-bold")
    dataset = service.create_dataset(
        project.project_id,
        DatasetCreate(
            name="Derivative-only BIDS",
            source_path=str(dataset_path),
            expected_project_version=project.version,
        ),
        "derivative-only-dataset",
    )
    manifest = service.inspect_dataset(
        dataset.dataset_id,
        ManifestScanRequest(expected_dataset_version=dataset.version),
        "derivative-only-manifest",
    )
    assert manifest.profile.kind is DatasetKind.BIDS
    assert manifest.profile.nifti_count == 1
    assert manifest.subjects == []
    project = service.get_project(project.project_id)

    with TestClient(create_app(service=service)) as client:
        response = client.post(
            "/api/v1/skill-plans/resolve",
            json={
                "request": {
                    "project_id": project.project_id,
                    "dataset_ref": dataset.dataset_id,
                    "input_manifest_hash": manifest.content_hash,
                    "requested_metrics": [],
                    "primary_outputs": [],
                    "study_protocol_ref": "protocol-derivative-only",
                    "request_preprocessing": True,
                    "preprocessing": _minimal_preprocessing_payload(),
                },
                "expected_project_version": project.version,
            },
            headers={"Idempotency-Key": "derivative-only-plan"},
        )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "manifest_functional_input_missing"


def test_plain_nifti_multiple_functional_candidates_require_explicit_selection(
    service: NeuroAgentService, source_root: Path, work_root: Path
) -> None:
    project = make_project(service, source_root, work_root, key="plain-multi-project")
    dataset_path = source_root / "plain-multi-run"
    for run in ("run-01", "run-02"):
        path = dataset_path / "S001" / "func" / f"{run}.nii"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(run.encode())
    dataset = service.create_dataset(
        project.project_id,
        DatasetCreate(
            name="Plain multi-run NIfTI",
            source_path=str(dataset_path),
            expected_project_version=project.version,
        ),
        "plain-multi-dataset",
    )
    manifest = service.inspect_dataset(
        dataset.dataset_id,
        ManifestScanRequest(expected_dataset_version=dataset.version),
        "plain-multi-manifest",
    )
    assert any("多个普通 NIfTI" in warning for warning in manifest.profile.warnings)
    project = service.get_project(project.project_id)

    with TestClient(create_app(service=service)) as client:
        response = client.post(
            "/api/v1/skill-plans/resolve",
            json={
                "request": {
                    "project_id": project.project_id,
                    "dataset_ref": dataset.dataset_id,
                    "input_manifest_hash": manifest.content_hash,
                    "requested_metrics": [],
                    "primary_outputs": [],
                    "study_protocol_ref": "protocol-plain-multi",
                    "request_preprocessing": True,
                    "preprocessing": _minimal_preprocessing_payload(),
                },
                "expected_project_version": project.version,
            },
            headers={"Idempotency-Key": "plain-multi-plan"},
        )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "manifest_functional_input_ambiguous"


def test_dicom_manifest_is_inventory_only_for_skill_planning(
    service: NeuroAgentService, source_root: Path, work_root: Path
) -> None:
    project = make_project(service, source_root, work_root, key="dicom-plan-project")
    dataset_path = source_root / "t1-only-dicom"
    t1_dicom = dataset_path / "sub-01" / "anat" / "IM000001.dcm"
    t1_dicom.parent.mkdir(parents=True)
    t1_dicom.write_bytes(b"synthetic-t1-dicom")
    dataset = service.create_dataset(
        project.project_id,
        DatasetCreate(
            name="T1-only DICOM inventory",
            source_path=str(dataset_path),
            expected_project_version=project.version,
        ),
        "dicom-plan-dataset",
    )
    manifest = service.inspect_dataset(
        dataset.dataset_id,
        ManifestScanRequest(expected_dataset_version=dataset.version),
        "dicom-plan-manifest",
    )
    project = service.get_project(project.project_id)

    with pytest.raises(InputValidationError) as raised:
        service.resolve_skill_plan(
            SkillPlanResolveRequest.model_validate(
                {
                    "request": {
                        "project_id": project.project_id,
                        "dataset_ref": dataset.dataset_id,
                        "input_manifest_hash": manifest.content_hash,
                        "requested_metrics": [],
                        "primary_outputs": [],
                        "study_protocol_ref": "protocol-dicom-inventory",
                        "request_preprocessing": True,
                        "preprocessing": _minimal_preprocessing_payload(),
                    },
                    "expected_project_version": project.version,
                }
            ),
            "dicom-inventory-plan",
        )

    assert raised.value.code == "manifest_dicom_inventory_only"
    assert manifest.subjects[0].functional_files == []
    assert manifest.subjects[0].dicom_files == ["sub-01/anat/IM000001.dcm"]


def test_dpabi_manifest_plan_validation_rejects_checkpoint_mixing() -> None:
    intent = SkillPlanIntent.model_validate(
        {
            "project_id": "project-dpabi",
            "dataset_ref": "dataset-dpabi",
            "input_manifest_hash": "a" * 64,
            "requested_metrics": [],
            "primary_outputs": [],
            "study_protocol_ref": "protocol-dpabi-stage",
            "request_preprocessing": True,
            "preprocessing": _minimal_preprocessing_payload(),
        }
    )
    profile = DatasetProfile(
        kind=DatasetKind.DPABI_READY,
        file_count=2,
        nifti_count=2,
        dicom_count=0,
        subject_count=1,
        warnings=[],
    )

    mixed_functional = ManifestRevisionView.model_validate(
        {
            "manifest_id": "manifest-dpabi-mixed-functional",
            "dataset_id": "dataset-dpabi",
            "revision": 1,
            "content_hash": "a" * 64,
            "profile": profile,
            "subjects": [
                SubjectManifestEntry(
                    subject_id="S001",
                    functional_files=[
                        "FunRaw/S001/raw.nii",
                        "FunImgARW/S001/processed.nii",
                    ],
                )
            ],
            "created_at": "2026-08-07T00:00:00Z",
        }
    )
    with pytest.raises(InputValidationError) as functional_error:
        NeuroAgentService._validate_manifest_for_skill_intent(intent, mixed_functional)
    assert functional_error.value.code == "manifest_dpabi_input_stage_invalid"

    mixed_anatomical = ManifestRevisionView.model_validate(
        {
            "manifest_id": "manifest-dpabi-mixed-anatomical",
            "dataset_id": "dataset-dpabi",
            "revision": 1,
            "content_hash": "a" * 64,
            "profile": profile,
            "subjects": [
                SubjectManifestEntry(
                    subject_id="S001",
                    functional_files=["FunRaw/S001/raw.nii"],
                    anatomical_files=["T1Img/S001/checkpoint.nii"],
                )
            ],
            "created_at": "2026-08-07T00:00:00Z",
        }
    )
    with pytest.raises(InputValidationError) as anatomical_error:
        NeuroAgentService._validate_manifest_for_skill_intent(intent, mixed_anatomical)
    assert anatomical_error.value.code == "manifest_dpabi_anatomical_stage_mixed"


def test_statistical_design_approval_hash_and_run_filters(
    service: NeuroAgentService,
    source_root: Path,
    work_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = make_project(service, source_root, work_root)
    dataset_path = make_bids_dataset(source_root)
    for data_kind, suffix in (("func", "task-rest_bold.nii.gz"), ("anat", "T1w.nii.gz")):
        target = dataset_path / "sub-03" / data_kind
        target.mkdir(parents=True, exist_ok=True)
        (target / f"sub-03_{suffix}").write_bytes(f"synthetic-sub-03-{data_kind}".encode())
    dataset = service.create_dataset(
        project.project_id,
        DatasetCreate(
            name="Statistics BIDS",
            source_path=str(dataset_path),
            expected_project_version=project.version,
        ),
        "statistics-source-dataset",
    )
    manifest = service.inspect_dataset(
        dataset.dataset_id,
        ManifestScanRequest(expected_dataset_version=dataset.version),
        "statistics-source-manifest",
    )
    project = service.get_project(project.project_id)
    preprocessing_plan = make_approved_plan(
        service,
        project.project_id,
        manifest_hash=manifest.content_hash,
    )
    preprocessing_run = service.create_run(
        RunCreate(
            project_id=project.project_id,
            plan_revision_id=preprocessing_plan.plan_revision_id,
            expected_plan_hash=preprocessing_plan.plan_hash,
        ),
        "statistics-qc-input-run",
    )
    assert build_worker(service, worker_id="statistics-qc-worker").run_once() is True
    preprocessing_run = service.get_run(preprocessing_run.run_id)
    service.repository.register_artifacts(
        project.project_id,
        preprocessing_run.run_id,
        (
            {
                "artifact_type": "mask.synthetic",
                "relative_path": "output/mask.nii.gz",
                "checksum": "4" * 64,
                "size_bytes": 1,
                "provenance": {
                    "lineage": _mask_lineage(manifest.content_hash).model_dump(mode="json")
                },
            },
        ),
    )
    mask_artifact = next(
        item
        for item in service.list_artifacts(preprocessing_run.run_id)
        if item.artifact_type == "mask.synthetic"
    )
    service.repository.register_artifacts(
        project.project_id,
        preprocessing_run.run_id,
        tuple(
            {
                "artifact_type": "metric.synthetic.alff",
                "relative_path": f"output/{subject_id}_zalff.nii.gz",
                "checksum": str(index) * 64,
                "size_bytes": 1,
                "provenance": {
                    "lineage": _metric_lineage(
                        manifest.content_hash,
                        subject_id,
                        mask_artifact.artifact_id,
                    ).model_dump(mode="json")
                },
            }
            for index, subject_id in enumerate(("sub-01", "sub-02", "sub-03"), start=1)
        ),
    )
    image_artifacts = tuple(
        item
        for item in service.list_artifacts(preprocessing_run.run_id)
        if item.artifact_type == "metric.synthetic.alff"
    )
    untyped_artifact = next(
        item
        for item in service.list_artifacts(preprocessing_run.run_id)
        if item.artifact_type == "mock.result"
    )
    assert len(image_artifacts) == 3

    with TestClient(create_app(service=service)) as client:
        assert client.get(f"/api/v1/artifacts/{mask_artifact.artifact_id}").status_code == 200
        assert (
            client.post(
                f"/api/v1/runs/{preprocessing_run.run_id}/qc-review",
                json={
                    "expected_version": preprocessing_run.version,
                    "actor": "legacy-reviewer",
                    "approved": True,
                    "reason": "must not bypass immutable QC",
                },
                headers={"Idempotency-Key": "legacy-qc-bypass"},
            ).status_code
            == 404
        )
        ghost_subject = client.post(
            "/api/v1/qc-reviews",
            json={
                "run_id": preprocessing_run.run_id,
                "expected_run_version": preprocessing_run.version,
                "metric_artifact_ids": [item.artifact_id for item in image_artifacts],
                "checks": [
                    {
                        "code": "MASK_GRID_MATCH",
                        "severity": "blocking",
                        "passed": True,
                        "evidence_artifact_ids": [mask_artifact.artifact_id],
                        "message": "Synthetic grids match",
                    }
                ],
                "included_subject_ids": ["sub-ghost"],
                "excluded_subject_ids": [],
                "exclusion_reasons": [],
            },
            headers={"Idempotency-Key": "qc-ghost-subject"},
        )
        assert ghost_subject.status_code == 422
        assert ghost_subject.json()["error"]["code"] == "qc_subject_manifest_mismatch"

        untyped_metric = client.post(
            "/api/v1/qc-reviews",
            json={
                "run_id": preprocessing_run.run_id,
                "expected_run_version": preprocessing_run.version,
                "metric_artifact_ids": [untyped_artifact.artifact_id],
                "checks": [
                    {
                        "code": "OUTPUT_EXISTS",
                        "severity": "blocking",
                        "passed": True,
                        "evidence_artifact_ids": [],
                        "message": "Untyped mock artifact exists",
                    }
                ],
                "included_subject_ids": ["sub-01", "sub-02", "sub-03"],
                "excluded_subject_ids": [],
                "exclusion_reasons": [],
            },
            headers={"Idempotency-Key": "qc-untyped-artifact"},
        )
        assert untyped_metric.status_code == 422
        assert untyped_metric.json()["error"]["code"] == "artifact_lineage_missing"

        invalid_exclusion = client.post(
            "/api/v1/qc-reviews",
            json={
                "run_id": preprocessing_run.run_id,
                "expected_run_version": preprocessing_run.version,
                "metric_artifact_ids": [item.artifact_id for item in image_artifacts],
                "checks": [
                    {
                        "code": "MASK_GRID_MATCH",
                        "severity": "blocking",
                        "passed": True,
                        "evidence_artifact_ids": [mask_artifact.artifact_id],
                        "message": "Synthetic grids match",
                    }
                ],
                "included_subject_ids": ["sub-01", "sub-02"],
                "excluded_subject_ids": ["sub-03"],
                "exclusion_reasons": [],
            },
            headers={"Idempotency-Key": "qc-invalid-exclusion"},
        )
        assert invalid_exclusion.status_code == 422

        qc_created_response = client.post(
            "/api/v1/qc-reviews",
            json={
                "run_id": preprocessing_run.run_id,
                "expected_run_version": preprocessing_run.version,
                "metric_artifact_ids": [item.artifact_id for item in image_artifacts],
                "checks": [
                    {
                        "code": "MASK_GRID_MATCH",
                        "severity": "blocking",
                        "passed": True,
                        "evidence_artifact_ids": [mask_artifact.artifact_id],
                        "message": "Synthetic grids match",
                    }
                ],
                "included_subject_ids": ["sub-01", "sub-02", "sub-03"],
                "excluded_subject_ids": [],
                "exclusion_reasons": [],
            },
            headers={"Idempotency-Key": "qc-create-approved-input"},
        )
        assert qc_created_response.status_code == 201
        qc_created = qc_created_response.json()
        qc_approved_response = client.post(
            f"/api/v1/qc-reviews/{qc_created['review']['review_revision_id']}/approve",
            json={
                "expected_review_version": qc_created["version"],
                "expected_run_version": preprocessing_run.version,
                "review_hash": qc_created["review"]["content_hash"],
                "actor": "qc-reviewer",
                "approved": True,
                "reason": "Synthetic QC evidence reviewed",
            },
            headers={"Idempotency-Key": "qc-approve-statistics-input"},
        )
        assert qc_approved_response.status_code == 200
        qc_approved = qc_approved_response.json()
        assert qc_approved["state"] == "approved"
        assert service.get_run(preprocessing_run.run_id).state.value == "succeeded"

        design = _one_sample_design(
            qc_approved["review"]["review_revision_id"],
            qc_approved["review"]["content_hash"],
            tuple(item.artifact_id for item in image_artifacts),  # type: ignore[arg-type]
            mask_artifact.artifact_id,
        )
        forged_qc_design = design.model_copy(update={"qc_review_hash": "0" * 64})
        forged_qc = client.post(
            "/api/v1/statistical-designs",
            json={
                "project_id": project.project_id,
                "expected_project_version": project.version,
                "input_manifest_hash": manifest.content_hash,
                "design": forged_qc_design.model_dump(mode="json"),
            },
            headers={"Idempotency-Key": "statistics-forged-qc-hash"},
        )
        assert forged_qc.status_code == 409
        assert forged_qc.json()["error"]["code"] == "qc_review_hash_mismatch"

        reversed_design = design.model_copy(
            update={
                "subject_order": tuple(reversed(design.subject_order)),
                "images": tuple(reversed(design.images)),
            }
        )
        wrong_order = client.post(
            "/api/v1/statistical-designs",
            json={
                "project_id": project.project_id,
                "expected_project_version": project.version,
                "input_manifest_hash": manifest.content_hash,
                "design": reversed_design.model_dump(mode="json"),
            },
            headers={"Idempotency-Key": "statistics-wrong-qc-order"},
        )
        assert wrong_order.status_code == 422
        assert wrong_order.json()["error"]["code"] == "statistics_qc_alignment_failed"

        swapped_artifacts = design.model_copy(
            update={
                "images": (
                    design.images[0].model_copy(
                        update={"artifact_id": design.images[1].artifact_id}
                    ),
                    design.images[1].model_copy(
                        update={"artifact_id": design.images[0].artifact_id}
                    ),
                    design.images[2],
                )
            }
        )
        mismatched_subject = client.post(
            "/api/v1/statistical-designs",
            json={
                "project_id": project.project_id,
                "expected_project_version": project.version,
                "input_manifest_hash": manifest.content_hash,
                "design": swapped_artifacts.model_dump(mode="json"),
            },
            headers={"Idempotency-Key": "statistics-swapped-artifacts"},
        )
        assert mismatched_subject.status_code == 422
        assert mismatched_subject.json()["error"]["code"] == "statistics_artifact_subject_mismatch"

        request = StatisticalDesignCreate(
            project_id=project.project_id,
            expected_project_version=project.version,
            input_manifest_hash=manifest.content_hash,
            design=design,
            correction=FdrCorrection(
                method="fdr",
                q_threshold=0.05,
                mask_artifact_id=mask_artifact.artifact_id,
                statistic_type=StatisticalMapType.T,
                df1=2.0,
                df2=None,
            ),
        )
        forged_design_environment = request.model_dump(mode="json")
        forged_design_environment["environment_hash"] = "0" * 64
        rejected_environment = client.post(
            "/api/v1/statistical-designs",
            json=forged_design_environment,
            headers={"Idempotency-Key": "statistics-forged-environment"},
        )
        assert rejected_environment.status_code == 422
        created_response = client.post(
            "/api/v1/statistical-designs",
            json=request.model_dump(mode="json"),
            headers={"Idempotency-Key": "statistics-design-create"},
        )
        assert created_response.status_code == 201
        created = created_response.json()
        revision = created["plan_revision"]
        assert revision["state"] == "draft"
        assert created["design_matrix"] == [[1.0], [1.0], [1.0]]

        premature = client.post(
            "/api/v1/statistics/runs",
            json={
                "project_id": project.project_id,
                "statistical_design_revision_id": revision["plan_revision_id"],
                "expected_plan_hash": revision["plan_hash"],
                "max_attempts": 1,
            },
            headers={"Idempotency-Key": "statistics-premature-run"},
        )
        assert premature.status_code == 409
        assert premature.json()["error"]["code"] == "plan_not_approved"

        validated_response = client.post(
            f"/api/v1/statistical-designs/{revision['plan_revision_id']}/validate",
            json={"expected_version": revision["version"]},
            headers={"Idempotency-Key": "statistics-design-validate"},
        )
        assert validated_response.status_code == 200
        validated = validated_response.json()["plan_revision"]
        approval_body = {
            "expected_version": validated["version"],
            "plan_hash": validated["plan_hash"],
            "actor": "researcher",
            "decision": "approved",
            "reason": "Synthetic statistical design reviewed",
        }
        approved = client.post(
            f"/api/v1/statistical-designs/{revision['plan_revision_id']}/approve",
            json=approval_body,
            headers={"Idempotency-Key": "statistics-design-approve"},
        )
        assert approved.status_code == 201

        wrong_hash = client.post(
            "/api/v1/statistics/runs",
            json={
                "project_id": project.project_id,
                "statistical_design_revision_id": revision["plan_revision_id"],
                "expected_plan_hash": "0" * 64,
                "max_attempts": 1,
            },
            headers={"Idempotency-Key": "statistics-wrong-hash"},
        )
        assert wrong_hash.status_code == 409
        assert wrong_hash.json()["error"]["code"] == "run_plan_hash_mismatch"

        with service.database.session_factory() as session:
            runs_before_qc_race = session.scalar(text("SELECT COUNT(*) FROM workflow_runs"))
            jobs_before_qc_race = session.scalar(text("SELECT COUNT(*) FROM jobs"))
        original_require_current = service._require_plan_current
        qc_invalidated = False

        def invalidate_qc_after_first_check(
            plan,
            **options,  # type: ignore[no-untyped-def]
        ):
            nonlocal qc_invalidated
            original_require_current(plan, **options)
            if plan.plan_revision_id != revision["plan_revision_id"] or qc_invalidated:
                return
            qc_invalidated = True
            with service.database.session_factory.begin() as session:
                session.execute(
                    text(
                        "UPDATE qc_review_revisions SET state = 'rejected' "
                        "WHERE review_revision_id = :review_revision_id"
                    ),
                    {"review_revision_id": qc_approved["review"]["review_revision_id"]},
                )

        with monkeypatch.context() as race_patch:
            race_patch.setattr(service, "_require_plan_current", invalidate_qc_after_first_check)
            raced_statistics_run = client.post(
                "/api/v1/statistics/runs",
                json={
                    "project_id": project.project_id,
                    "statistical_design_revision_id": revision["plan_revision_id"],
                    "expected_plan_hash": revision["plan_hash"],
                    "max_attempts": 1,
                },
                headers={"Idempotency-Key": "statistics-qc-race"},
            )
        assert qc_invalidated is True
        assert raced_statistics_run.status_code == 409
        assert raced_statistics_run.json()["error"]["code"] == "qc_review_not_approved"
        with service.database.session_factory() as session:
            assert session.scalar(text("SELECT COUNT(*) FROM workflow_runs")) == runs_before_qc_race
            assert session.scalar(text("SELECT COUNT(*) FROM jobs")) == jobs_before_qc_race
        with service.database.session_factory.begin() as session:
            session.execute(
                text(
                    "UPDATE qc_review_revisions SET state = 'approved' "
                    "WHERE review_revision_id = :review_revision_id"
                ),
                {"review_revision_id": qc_approved["review"]["review_revision_id"]},
            )

        run_response = client.post(
            "/api/v1/statistics/runs",
            json={
                "project_id": project.project_id,
                "statistical_design_revision_id": revision["plan_revision_id"],
                "expected_plan_hash": revision["plan_hash"],
                "max_attempts": 1,
            },
            headers={"Idempotency-Key": "statistics-approved-run"},
        )
        assert run_response.status_code == 202
        run = run_response.json()
        listed = client.get(f"/api/v1/runs?project_id={project.project_id}&state=queued").json()
        assert [item["run_id"] for item in listed] == [run["run_id"]]
        corrections = client.get("/api/v1/corrections").json()
        assert {item["method"] for item in corrections} == {"fdr", "grf"}
        assert all("schema" in item for item in corrections)


def test_agent_api_redacts_falls_back_and_never_returns_secrets(
    service: NeuroAgentService,
    source_root: Path,
    work_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = make_project(service, source_root, work_root)
    first = MockProvider([RetryableProviderError("temporary rate limit")])
    second = MockProvider(
        [
            _recommendation("A safe reviewable recommendation."),
            _recommendation("Provider is available."),
            _recommendation("Unsafe proposal.", {"command": "run arbitrary matlab"}),
        ]
    )
    monkeypatch.setenv("FIRST_PROVIDER_API_KEY", "first-secret-value")
    monkeypatch.setenv("SECOND_PROVIDER_API_KEY", "second-secret-value")
    settings = service.settings.model_copy(
        update={"redaction_salt": "a-stable-redaction-salt-value"}
    )
    agent_service = build_service(
        settings,
        database=service.database,
        repository=service.repository,
        providers={"first-provider": first, "second-provider": second},
    )
    profiles = (
        {
            "id": "first-provider",
            "provider": "first-provider",
            "base_url": "https://first.example/v1",
            "model": "model-one",
            "api_key_env": "FIRST_PROVIDER_API_KEY",
            "priority": 1,
            "capabilities": ["json_object"],
            "timeout_seconds": 10,
        },
        {
            "id": "second-provider",
            "provider": "second-provider",
            "base_url": "https://second.example/v1",
            "model": "model-two",
            "api_key_env": "SECOND_PROVIDER_API_KEY",
            "priority": 2,
            "capabilities": ["json_object"],
            "timeout_seconds": 10,
        },
    )
    with TestClient(create_app(service=agent_service)) as client:
        for index, profile in enumerate(profiles):
            response = client.post(
                "/api/v1/model-profiles",
                json=profile,
                headers={"Idempotency-Key": f"model-profile-{index}"},
            )
            assert response.status_code == 201
            assert "secret-value" not in response.text

        rejected_secret = client.post(
            "/api/v1/model-profiles",
            json={**profiles[0], "id": "unsafe-profile", "api_key": "must-not-be-accepted"},
            headers={"Idempotency-Key": "unsafe-model-profile"},
        )
        assert rejected_secret.status_code == 422
        assert "must-not-be-accepted" not in rejected_secret.text
        unsafe_environment = client.post(
            "/api/v1/model-profiles",
            json={**profiles[0], "id": "unsafe-environment", "api_key_env": "PATH"},
            headers={"Idempotency-Key": "unsafe-environment-profile"},
        )
        assert unsafe_environment.status_code == 422
        credential_url = client.post(
            "/api/v1/model-profiles",
            json={
                **profiles[0],
                "id": "unsafe-base-url",
                "base_url": "https://user:password@provider.example/v1",
            },
            headers={"Idempotency-Key": "unsafe-base-url-profile"},
        )
        assert credential_url.status_code == 422

        rejected_context = client.post(
            "/api/v1/agent/tasks",
            json={
                "expected_project_version": project.version,
                "request": {
                    "task_type": "skill_planner",
                    "project_id": project.project_id,
                    "summary": {
                        "purpose": "explain_current_plan",
                        "subject_id": "sub-raw-001",
                        "source_path": r"D:\private\sub-raw-001\scan.nii",
                    },
                    "required_capabilities": ["json_object"],
                },
            },
            headers={"Idempotency-Key": "agent-task-rejected-context"},
        )
        assert rejected_context.status_code == 422
        assert first.requests == []
        assert second.requests == []

        task_response = client.post(
            "/api/v1/agent/tasks",
            json={
                "expected_project_version": project.version,
                "request": {
                    "task_type": "skill_planner",
                    "project_id": project.project_id,
                    "summary": {"purpose": "explain_current_plan"},
                    "required_capabilities": ["json_object"],
                },
            },
            headers={"Idempotency-Key": "agent-task-fallback"},
        )
        assert task_response.status_code == 201
        task = task_response.json()
        assert task["result"]["routing"]["selected_profile_id"] == "second-provider"
        assert task["result"]["attempted_profile_ids"] == [
            "first-provider",
            "second-provider",
        ]
        assert "first-secret-value" not in task_response.text
        assert "second-secret-value" not in task_response.text

        for provider in (first, second):
            user_payload = json.loads(provider.requests[0][1][1]["content"])
            assert user_payload["purpose"] == "explain_current_plan"
            assert "subject_id" not in user_payload
            assert "source_path" not in user_payload
            assert project.project_id not in json.dumps(user_payload)

        provider_test = client.post(
            "/api/v1/providers/test",
            json={"profile_id": "second-provider", "expected_profile_version": 1},
            headers={"Idempotency-Key": "provider-test-second"},
        )
        assert provider_test.status_code == 200
        assert provider_test.json()["available"] is True

        unsafe = client.post(
            "/api/v1/agent/tasks",
            json={
                "expected_project_version": project.version,
                "request": {
                    "task_type": "skill_planner",
                    "project_id": project.project_id,
                    "summary": {"purpose": "explain_current_plan"},
                    "required_capabilities": ["json_object"],
                    "preferred_profile_id": "second-provider",
                },
            },
            headers={"Idempotency-Key": "agent-task-unsafe-output"},
        )
        assert unsafe.status_code == 422
        assert unsafe.json()["error"]["code"] == "agent_skill_request_invalid"

        persisted = client.get(f"/api/v1/agent/tasks/{task['task_id']}")
        assert persisted.status_code == 200
        assert persisted.json() == task


@pytest.mark.asyncio
async def test_agent_task_rechecks_project_revision_after_provider_wait(
    service: NeuroAgentService,
    source_root: Path,
    work_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class BlockingProvider:
        def __init__(self) -> None:
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def generate(self, profile, api_key, messages):  # type: ignore[no-untyped-def]
            del profile, api_key, messages
            self.started.set()
            await self.release.wait()
            return ProviderResponse(content=_recommendation("Safe but now stale."), model="mock")

    project = make_project(service, source_root, work_root, key="agent-revision-project")
    provider = BlockingProvider()
    settings = service.settings.model_copy(
        update={"redaction_salt": "a-stable-redaction-salt-value"}
    )
    agent_service = build_service(
        settings,
        database=service.database,
        repository=service.repository,
        providers={"blocking-provider": provider},
    )
    profile = ModelProfileInput(
        id="blocking-profile",
        provider="blocking-provider",
        base_url="https://provider.example/v1",
        model="mock-model",
        api_key_env="BLOCKING_PROVIDER_API_KEY",
        priority=1,
        capabilities=frozenset({ModelCapability.JSON_OBJECT}),
        timeout_seconds=10,
    )
    agent_service.create_model_profile(profile, "agent-revision-profile")
    monkeypatch.setenv("BLOCKING_PROVIDER_API_KEY", "test-secret-value")
    pending = asyncio.create_task(
        agent_service.create_agent_task(
            AgentTaskCreate(
                expected_project_version=project.version,
                request=AgentTaskRequest(
                    task_type=TaskType.PLAN_EXPLAINER,
                    project_id=project.project_id,
                    summary={"purpose": AgentSummaryPurpose.EXPLAIN_CURRENT_PLAN},
                    preferred_profile_id=profile.id,
                ),
            ),
            "agent-revision-task",
        )
    )
    await asyncio.wait_for(provider.started.wait(), timeout=2)
    agent_service.create_dataset(
        project.project_id,
        DatasetCreate(
            name="revision change",
            source_path=str(source_root),
            expected_project_version=project.version,
        ),
        "agent-revision-dataset",
    )
    provider.release.set()

    with pytest.raises(ConflictError) as caught:
        await pending
    assert caught.value.code == "revision_conflict"
    with service.database.session_factory() as session:
        assert session.scalar(text("SELECT count(*) FROM agent_tasks")) == 0


@pytest.mark.asyncio
async def test_slow_provider_request_renews_idempotency_lease(
    service: NeuroAgentService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class BlockingProvider:
        def __init__(self) -> None:
            self.started = asyncio.Event()
            self.release = asyncio.Event()
            self.calls = 0

        async def generate(self, profile, api_key, messages):  # type: ignore[no-untyped-def]
            del profile, api_key, messages
            self.calls += 1
            self.started.set()
            await self.release.wait()
            return ProviderResponse(content=_recommendation("Lease renewed."), model="mock")

    provider = BlockingProvider()
    settings = service.settings.model_copy(
        update={
            "redaction_salt": "a-stable-redaction-salt-value",
            "idempotency_lease_seconds": 1,
        }
    )
    agent_service = build_service(
        settings,
        database=service.database,
        repository=service.repository,
        providers={"slow-provider": provider},
    )
    profile = ModelProfileInput(
        id="slow-profile",
        provider="slow-provider",
        base_url="https://provider.example/v1",
        model="mock-model",
        api_key_env="SLOW_PROVIDER_API_KEY",
        priority=1,
        capabilities=frozenset({ModelCapability.JSON_OBJECT}),
        timeout_seconds=10,
    )
    agent_service.create_model_profile(profile, "slow-provider-profile")
    monkeypatch.setenv("SLOW_PROVIDER_API_KEY", "test-secret-value")
    request = ProviderTestRequest(profile_id=profile.id, expected_profile_version=1)
    pending = asyncio.create_task(agent_service.test_provider(request, "slow-provider-test"))
    await asyncio.wait_for(provider.started.wait(), timeout=2)
    await asyncio.sleep(1.2)

    with pytest.raises(ConflictError) as duplicate:
        await agent_service.test_provider(request, "slow-provider-test")
    assert duplicate.value.code == "idempotency_request_in_progress"
    assert provider.calls == 1

    provider.release.set()
    first = await asyncio.wait_for(pending, timeout=2)
    repeated = await agent_service.test_provider(request, "slow-provider-test")
    assert repeated == first
    assert provider.calls == 1


def test_settings_load_explicit_dotenv_and_have_no_machine_path_defaults(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in (
        "RSFMRI_MATLAB_EXECUTABLE",
        "RSFMRI_SPM_DIR",
        "RSFMRI_DPABI_DIR",
        "RSFMRI_ALLOWED_SOURCE_ROOTS",
    ):
        monkeypatch.delenv(name, raising=False)
    defaults = Settings(_env_file=None)
    assert defaults.matlab_executable is None
    assert defaults.spm_dir is None
    assert defaults.dpabi_dir is None

    first_root = tmp_path / "source-one"
    second_root = tmp_path / "source-two"
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            (
                f"RSFMRI_ALLOWED_SOURCE_ROOTS={first_root}{os.pathsep}{second_root}",
                f"RSFMRI_ALLOWED_WORK_ROOT={tmp_path / 'work'}",
                f"RSFMRI_MATLAB_EXECUTABLE={tmp_path / 'MATLAB' / 'matlab.exe'}",
                f"RSFMRI_SPM_DIR={tmp_path / 'spm12'}",
                f"RSFMRI_DPABI_DIR={tmp_path / 'DPABI'}",
            )
        ),
        encoding="utf-8",
    )
    configured = Settings(_env_file=env_file)
    assert configured.allowed_source_roots == [first_root, second_root]
    assert configured.matlab_executable == tmp_path / "MATLAB" / "matlab.exe"


def test_multisession_manifest_reaches_qc_and_paired_design(
    service: NeuroAgentService, source_root: Path, work_root: Path
) -> None:
    project = make_project(service, source_root, work_root, key="multisession-project")
    dataset_path = source_root / "multisession-bids"
    dataset_path.mkdir()
    (dataset_path / "dataset_description.json").write_text(
        '{"Name":"synthetic-multisession","BIDSVersion":"1.9.0"}',
        encoding="utf-8",
    )
    subjects = ("sub-01", "sub-02", "sub-03")
    sessions = (("ses-01", "pre"), ("ses-02", "post"))
    for subject_id in subjects:
        for session_id, _condition in sessions:
            functional_dir = dataset_path / subject_id / session_id / "func"
            anatomical_dir = dataset_path / subject_id / session_id / "anat"
            functional_dir.mkdir(parents=True)
            anatomical_dir.mkdir(parents=True)
            (functional_dir / f"{subject_id}_{session_id}_task-rest_bold.nii.gz").write_bytes(
                f"synthetic-{subject_id}-{session_id}-bold".encode()
            )
            (anatomical_dir / f"{subject_id}_{session_id}_T1w.nii.gz").write_bytes(
                f"synthetic-{subject_id}-{session_id}-t1".encode()
            )

    dataset = service.create_dataset(
        project.project_id,
        DatasetCreate(
            name="Multi-session BIDS",
            source_path=str(dataset_path),
            expected_project_version=project.version,
        ),
        "multisession-dataset",
    )
    manifest = service.inspect_dataset(
        dataset.dataset_id,
        ManifestScanRequest(expected_dataset_version=dataset.version),
        "multisession-manifest",
    )
    assert [(entry.subject_id, entry.session_id) for entry in manifest.subjects] == [
        (subject_id, session_id) for subject_id in subjects for session_id, _condition in sessions
    ]

    plan = make_approved_plan(
        service,
        project.project_id,
        manifest_hash=manifest.content_hash,
    )
    run = service.create_run(
        RunCreate(
            project_id=project.project_id,
            plan_revision_id=plan.plan_revision_id,
            expected_plan_hash=plan.plan_hash,
        ),
        "multisession-run",
    )
    assert build_worker(service, worker_id="multisession-worker").run_once() is True
    run = service.get_run(run.run_id)

    service.repository.register_artifacts(
        project.project_id,
        run.run_id,
        (
            {
                "artifact_type": "mask.synthetic",
                "relative_path": "output/group-mask.nii.gz",
                "checksum": "9" * 64,
                "size_bytes": 1,
                "provenance": {
                    "lineage": _mask_lineage(manifest.content_hash).model_dump(mode="json")
                },
            },
        ),
    )
    mask = next(
        artifact
        for artifact in service.list_artifacts(run.run_id)
        if artifact.artifact_type == "mask.synthetic"
    )
    metric_specs = [
        (subject_id, session_id, condition)
        for session_id, condition in sessions
        for subject_id in subjects
    ]
    service.repository.register_artifacts(
        project.project_id,
        run.run_id,
        tuple(
            {
                "artifact_type": "metric.synthetic.alff",
                "relative_path": f"output/{subject_id}_{session_id}_zalff.nii.gz",
                "checksum": f"{index:064x}",
                "size_bytes": 1,
                "provenance": {
                    "lineage": _metric_lineage(
                        manifest.content_hash,
                        subject_id,
                        mask.artifact_id,
                        session_id=session_id,
                        condition=condition,
                    ).model_dump(mode="json")
                },
            }
            for index, (subject_id, session_id, condition) in enumerate(metric_specs, start=1)
        ),
    )
    metrics = tuple(
        artifact
        for artifact in service.list_artifacts(run.run_id)
        if artifact.artifact_type == "metric.synthetic.alff"
    )
    review = service.create_qc_review(
        QcReviewCreate(
            run_id=run.run_id,
            expected_run_version=run.version,
            metric_artifact_ids=tuple(artifact.artifact_id for artifact in metrics),
            checks=(
                QcCheck(
                    code="MASK_GRID_MATCH",
                    severity=QcSeverity.BLOCKING,
                    passed=True,
                    evidence_artifact_ids=(mask.artifact_id,),
                    message="Synthetic multi-session grids match",
                ),
            ),
            included_subject_ids=subjects,
            excluded_subject_ids=(),
            exclusion_reasons=(),
        ),
        "multisession-qc-create",
    )
    approved = service.approve_qc_review(
        review.review.review_revision_id,
        QcReviewApprove(
            expected_review_version=review.version,
            expected_run_version=run.version,
            review_hash=review.review.content_hash,
            actor="qc-reviewer",
            approved=True,
            reason="Synthetic multi-session QC reviewed",
        ),
        "multisession-qc-approve",
    )

    artifacts_by_pair = {
        (
            str(artifact.provenance["lineage"]["subject_id"]),
            str(artifact.provenance["lineage"]["condition"]),
        ): artifact.artifact_id
        for artifact in metrics
    }
    design = StatisticalDesignRevision(
        revision_id="paired-multisession",
        test=StatisticalTest.PAIRED_T,
        subject_order=subjects,
        images=tuple(
            AnalysisImage(
                subject_id=subject_id,
                artifact_id=artifacts_by_pair[(subject_id, condition)],
                group=None,
                condition=condition,
            )
            for condition in ("pre", "post")
            for subject_id in subjects
        ),
        group_order=(),
        condition_order=("pre", "post"),
        covariates=(),
        contrast=(1.0, 0.0, 0.0, 0.0),
        one_sample_baseline=None,
        mask_artifact_id=mask.artifact_id,
        tail=Tail.TWO_SIDED,
        missing_value_policy=MissingValuePolicy.ERROR,
        qc_review_revision_id=approved.review.review_revision_id,
        qc_review_hash=approved.review.content_hash,
    )
    result = service.create_statistical_design(
        StatisticalDesignCreate(
            project_id=project.project_id,
            expected_project_version=service.get_project(project.project_id).version,
            input_manifest_hash=manifest.content_hash,
            design=design,
            correction=FdrCorrection(
                method="fdr",
                q_threshold=0.05,
                mask_artifact_id=mask.artifact_id,
                statistic_type=StatisticalMapType.T,
                df1=2.0,
                df2=None,
            ),
        ),
        "multisession-paired-design",
    )

    assert result.plan_revision.state.value == "draft"
    assert len(result.design_matrix) == 6
    assert result.design.condition_order == ("pre", "post")


def test_production_static_app_preserves_api_namespace(
    service: NeuroAgentService, tmp_path: Path
) -> None:
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<html>rs-fMRI UI</html>", encoding="utf-8")
    settings = service.settings.model_copy(update={"serve_frontend": True, "frontend_dist": dist})
    static_service = build_service(
        settings,
        database=service.database,
        repository=service.repository,
        providers={},
    )
    with TestClient(create_app(settings=settings, service=static_service)) as client:
        assert "rs-fMRI UI" in client.get("/").text
        assert "rs-fMRI UI" in client.get("/projects/example").text
        missing_api = client.get("/api/v1/does-not-exist")
        assert missing_api.status_code == 404
        assert missing_api.json()["error"]["code"] == "not_found"
