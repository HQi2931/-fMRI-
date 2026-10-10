"""Opt-in real preprocessing, QC, Work handoff and statistics on synthetic data."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any
from uuid import uuid4

import nibabel as nib
import numpy as np

from neuroagent.application.contracts import (
    ApprovalCreate,
    ConversationCreate,
    DatasetCreate,
    DemographicsImportRequest,
    ManifestScanRequest,
    ProjectCreate,
    ProjectView,
    QcReviewApprove,
    RunCreate,
    RunView,
    SkillPlanIntent,
    SkillPlanResolveRequest,
    StatisticalDesignCreate,
    StatisticalDesignValidationRequest,
    StatisticalWorkContext,
    StatisticsRunCreate,
)
from neuroagent.application.services import NeuroAgentService
from neuroagent.application.settings import Settings
from neuroagent.application.statistics_draft import StatisticsDraft, statistics_draft_hash
from neuroagent.application.work_handoffs import require_same_handoff
from neuroagent.bootstrap import build_service, build_worker
from neuroagent.infrastructure.environment import SettingsEnvironmentLockProvider


def _hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _parameters() -> dict[str, Any]:
    disabled_nuisance = dict.fromkeys(
        (
            "timing",
            "polynomial_trend",
            "head_motion_model",
            "head_motion_scrubbing",
            "white_matter",
            "csf",
            "global_signal",
            "warp_masks_to_individual_space",
            "add_mean_back",
        )
    )
    parameters = {
        "tr_seconds": 2.0,
        "expected_time_points": 84,
        "dummy_scans": 4,
        "slice_timing": {
            "enabled": False,
            "slice_count": None,
            "slice_order": None,
            "reference_slice": None,
        },
        "realignment": {"enabled": True, "options_source": "dpabi_v82_jobmat"},
        "nuisance": {"enabled": False, **disabled_nuisance},
        "normalization": {
            "mode": 0,
            "timing": None,
            "bounding_box_mm": None,
            "voxel_size_mm": None,
            "structural_artifact_id": None,
            "affine_regularization": None,
        },
        "detrend": True,
        "temporal_filter": {"timing": "disabled", "frequency_band": None, "add_mean_back": None},
        "scrubbing": {"enabled": False, "timing": None, "censoring": None, "method": None},
        "smoothing": {"timing": "disabled", "method": None, "fwhm_mm": None},
    }
    parameters["provenance"] = [
        {"name": name, "source": "study_protocol", "evidence_ref": "synthetic-acceptance://" + name}
        for name in parameters
    ]
    return parameters


def _settings(root: Path) -> Settings:
    source, work = root / "source", root / "work"
    configuration = SettingsEnvironmentLockProvider(Settings.from_env()).configuration()
    settings = Settings(
        _env_file=None,
        database_url=f"sqlite:///{(root / 'acceptance.sqlite').as_posix()}",
        allowed_source_roots=[source],
        allowed_work_root=work,
        matlab_executable=configuration.matlab_executable,
        spm_dir=configuration.spm_dir,
        dpabi_dir=configuration.dpabi_dir,
        secrets_file=root / "acceptance-secrets.env",
        enable_real_execution=True,
        serve_frontend=False,
        rag_db_dir=None,
    )
    return settings


def _analyze(
    service: NeuroAgentService,
    root: Path,
    source_hashes: dict[str, str],
    project: ProjectView,
    pre: RunView,
) -> None:
    source = root / "source"
    key = uuid4().hex
    worker = build_worker(service, worker_id="internal-real-acceptance")
    review = service.repository.get_latest_qc_review_for_run(pre.run_id)
    assert review is not None and review.state.value == "approved"
    proof = service.prepare_work_analysis_handoff(pre.run_id, project.project_id)
    assert proof.status == "ready", proof.model_dump_json()
    assert len(proof.data_references["metrics"]) == 6
    assert len(proof.data_references["masks"]) == 7
    draft = StatisticsDraft(
        test="independent_two_sample_t",
        grouping_column="group",
        group_order=["ASD", "HC"],
        metric_artifact_type="metric.alff.z_score",
        tail="two_sided",
        missing_value_policy="error",
        correction_method="fdr",
        correction_parameters={"q_threshold": 0.05},
    )
    proposal, issues = service.prepare_work_statistical_design_proposal(
        project_id=project.project_id,
        run_id=pre.run_id,
        draft=draft,
    )
    assert proposal is not None and not issues, issues
    conversation = service.create_conversation(
        ConversationCreate(
            mode="work",
            project_id=project.project_id,
            workspace_path=str(source),
        ),
        f"internal-work-conversation-{key}",
    )
    service.repository.append_conversation_exchange(
        conversation.conversation_id,
        user_content="Synthetic software acceptance",
        assistant_content="Deterministic server proposal; no model quality claim.",
        assistant_payload={
            "agent_role": "preprocessing",
            "run": service.get_run(pre.run_id).model_dump(mode="json"),
        },
        tool=None,
        workspace_path=str(source),
        preferred_profile_id=None,
        project_id=project.project_id,
        active_run_id=pre.run_id,
    )
    service.repository.append_conversation_exchange(
        conversation.conversation_id,
        user_content="Prepare the confirmed two-sample analysis",
        assistant_content="Review the frozen statistical proposal.",
        assistant_payload={
            "agent_role": "analysis",
            "analysis_draft": {
                "draft": draft.model_dump(mode="json"),
                "hash": statistics_draft_hash(draft),
            },
            "statistical_design_proposal": proposal.model_dump(mode="json"),
            "agent_handoff": require_same_handoff(proof, proof).model_dump(mode="json"),
        },
        tool=None,
        workspace_path=str(source),
        preferred_profile_id=None,
        project_id=project.project_id,
        active_run_id=None,
    )
    created = service.create_statistical_design(
        StatisticalDesignCreate(
            project_id=project.project_id,
            expected_project_version=proposal.project_version,
            input_manifest_hash=proposal.input_manifest_hash,
            design=proposal.design,
            correction=proposal.correction,
            work_context=StatisticalWorkContext(
                conversation_id=conversation.conversation_id,
                proposal_hash=proposal.proposal_hash,
            ),
        ),
        f"internal-statistical-design-{key}",
    )
    validated = service.validate_statistical_design(
        created.plan_revision.plan_revision_id,
        StatisticalDesignValidationRequest(expected_version=created.plan_revision.version),
        f"internal-statistical-validation-{key}",
    )
    plan = validated.plan_revision
    service.approve_plan(
        plan.plan_revision_id,
        ApprovalCreate(
            expected_version=plan.version,
            plan_hash=plan.plan_hash,
            actor="authorized_synthetic_acceptance",
            decision="approved",
            reason="Separate user-authorized statistics acceptance after synthetic QC.",
        ),
        f"internal-statistical-approval-{key}",
    )
    run = service.create_statistics_run(
        StatisticsRunCreate(
            project_id=project.project_id,
            statistical_design_revision_id=plan.plan_revision_id,
            expected_plan_hash=plan.plan_hash,
            execution_backend="matlab",
            real_execution_confirmed=True,
        ),
        f"internal-statistical-run-{key}",
    )
    print(f"STATISTICS_QUEUED {run.run_id}", flush=True)
    assert worker.run_once()
    run = service.get_run(run.run_id)
    assert run.state.value == "succeeded", run.model_dump_json()
    result = service.repository.list_statistical_results(
        project_id=project.project_id, run_id=run.run_id
    )[0]
    detail = service.repository.get_statistical_result(result.result_id)
    metrics = {item.artifact_id: item for item in service.list_artifacts(pre.run_id)}
    base_path = Path(project.work_root) / pre.run_id
    images = [
        nib.as_closest_canonical(nib.load(str(base_path / metrics[row.artifact_id].relative_path)))
        for row in proposal.design.images
    ]
    mask_image = nib.as_closest_canonical(
        nib.load(str(base_path / metrics[proposal.design.mask_artifact_id].relative_path))
    )
    mask = mask_image.get_fdata() != 0
    values = np.asarray([image.get_fdata() for image in images])
    first, second = values[:3], values[3:]
    variance = (first.var(axis=0, ddof=1) + second.var(axis=0, ddof=1)) / 2
    expected = (first.mean(axis=0) - second.mean(axis=0))[mask] / np.sqrt(variance[mask] * 2 / 3)
    maps = service.list_artifacts(run.run_id)
    statistic = next(
        item for item in maps if item.artifact_type == "statistics.uncorrected_statistical_map"
    )
    actual = nib.as_closest_canonical(
        nib.load(str(Path(project.work_root) / run.run_id / statistic.relative_path))
    )
    assert np.allclose(actual.affine, mask_image.affine, atol=1e-5, rtol=0)
    error = float(np.max(np.abs(actual.get_fdata()[mask] - expected)))
    assert error < 1e-5, error
    assert source_hashes == {
        path.relative_to(source).as_posix(): _hash(path)
        for path in source.rglob("*")
        if path.is_file()
    }
    report = json.loads(detail.report_json)
    (root / "handoff.json").write_text(proof.model_dump_json(indent=2), encoding="utf-8")
    (root / "qc-review.json").write_text(review.model_dump_json(indent=2), encoding="utf-8")
    (root / "report.md").write_text(detail.report_markdown, encoding="utf-8")
    (root / "report.json").write_text(detail.report_json, encoding="utf-8")
    summary = {
        "status": "pass",
        "mode": "real_matlab_synthetic_data_service_integration",
        "project_id": project.project_id,
        "conversation_id": conversation.conversation_id,
        "preprocessing_run_id": pre.run_id,
        "statistical_run_id": run.run_id,
        "result_id": result.result_id,
        "handoff_hash": proof.evidence_hash,
        "subject_count": 6,
        "retained_volumes": 80,
        "source_unchanged": True,
        "t_map_max_absolute_error": error,
        "inference_summary": report["inference_summary"],
        "limitations": (
            "Synthetic native-space inputs; deterministic proposal; "
            "no browser or real-model planning claim."
        ),
    }
    (root / "verification.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print(json.dumps(summary, ensure_ascii=False), flush=True)


def _resume_source(root: Path) -> None:
    descriptor = root / "preprocessing-run.json"
    if not descriptor.is_file() or not (root / "acceptance.sqlite").is_file():
        raise ValueError("Resume requires this script's existing acceptance directory")
    service = build_service(_settings(root))
    try:
        pre = service.get_run(json.loads(descriptor.read_text(encoding="utf-8"))["run_id"])
        project = service.get_project(pre.project_id)
        assert pre.state.value == "succeeded", "The source run must have approved QC"
        for run in service.list_runs(project_id=project.project_id):
            assert run.state.value in {"succeeded", "failed_terminal", "cancelled", "timed_out"}, (
                "A run is still active"
            )
            if run.run_id != pre.run_id and run.state.value == "succeeded":
                raise ValueError(
                    "Statistics already succeeded; inspect the saved result instead of rerunning"
                )
        source = root / "source"
        source_hashes = {
            p.relative_to(source).as_posix(): _hash(p) for p in source.rglob("*") if p.is_file()
        }
        _analyze(service, root, source_hashes, project, pre)
    finally:
        service.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--real-matlab", action="store_true")
    parser.add_argument("--resume-source", action="store_true")
    args = parser.parse_args()
    if not args.real_matlab:
        parser.error("--real-matlab is required for this authorized synthetic acceptance")
    root = args.output_root.resolve()
    if args.resume_source:
        _resume_source(root)
        return
    root.mkdir(parents=True, exist_ok=False)
    source, work = root / "source", root / "work"
    source.mkdir()
    work.mkdir()
    rng = np.random.default_rng(20261010)
    # Full synthetic field of view with a margin around the effective DPABI
    # mask. Realignment's boundary NaNs must remain outside that mask.
    shape = (36, 36, 36)
    affine = np.diag([6.0, 6.0, 6.0, 1.0])
    affine[:3, 3] = (-108, -126, -84)
    positions = np.indices(shape).astype(float)
    texture = 100 + 200 * np.exp(-np.sum((positions - 17.5) ** 2, axis=0) / 270)
    texture += rng.uniform(0, 15, shape)
    amplitude = rng.uniform(1, 3, shape)
    temporal = np.sin(np.arange(84) * 0.3)
    subjects = tuple(f"sub-{index:02d}" for index in range(1, 7))
    for subject in subjects:
        values = texture[..., None] + amplitude[..., None] * temporal
        values += rng.normal(0, 0.1, (*shape, 84))
        path = source / "FunImg" / subject / "rest.nii"
        path.parent.mkdir(parents=True)
        image = nib.Nifti1Image(values.astype(np.float32), affine)
        image.header.set_zooms((6, 6, 6, 2))
        image.header.set_xyzt_units("mm", "sec")
        nib.save(image, path)
    table = source / "demographics.csv"
    table.write_text(
        "ID,group\n"
        + "".join(
            f"{subject},{'ASD' if subject in subjects[:3] else 'HC'}\n"
            for subject in reversed(subjects)
        ),
        encoding="utf-8",
    )
    source_hashes = {
        path.relative_to(source).as_posix(): _hash(path)
        for path in source.rglob("*")
        if path.is_file()
    }
    settings = _settings(root)
    service = build_service(settings)
    try:
        project = service.create_project(
            ProjectCreate(
                name="Synthetic internal handoff acceptance",
                source_roots=[str(source)],
                work_root=str(work / "project"),
            ),
            "internal-project",
        )
        dataset = service.create_dataset(
            project.project_id,
            DatasetCreate(
                name="Six synthetic functional series",
                source_path=str(source),
                expected_project_version=project.version,
            ),
            "internal-dataset",
        )
        manifest = service.inspect_dataset(
            dataset.dataset_id,
            ManifestScanRequest(
                expected_dataset_version=dataset.version,
                selected_input_stage="FunImg",
            ),
            "internal-scan",
        )
        service.import_demographics(
            dataset.dataset_id,
            DemographicsImportRequest(
                source_path=str(table),
                subject_id_column="ID",
                column_mapping={"group": "group"},
                expected_dataset_version=service.repository.get_dataset(dataset.dataset_id).version,
            ),
            "internal-demographics",
        )
        alff = {
            "tr_seconds": 2,
            "frequency_band": {"low_hz": 0.01, "high_hz": 0.08},
            "requested_metrics": ["alff"],
            "requested_scalings": ["z_score"],
            "mask_source": "dpabi_auto",
            "mask_artifact_id": None,
            "filter_timing": "disabled",
            "result_smoothing": False,
            "result_smoothing_fwhm_mm": None,
            "provenance": [
                {
                    "name": name,
                    "source": "study_protocol",
                    "evidence_ref": "synthetic-acceptance://" + name,
                }
                for name in (
                    "tr_seconds",
                    "frequency_band",
                    "requested_metrics",
                    "requested_scalings",
                    "filter_timing",
                    "result_smoothing",
                )
            ],
        }
        resolved = service.resolve_skill_plan(
            SkillPlanResolveRequest(
                request=SkillPlanIntent(
                    project_id=project.project_id,
                    dataset_ref=dataset.dataset_id,
                    input_manifest_hash=manifest.content_hash,
                    requested_metrics=("alff",),
                    primary_outputs=("zALFF",),
                    request_preprocessing=True,
                    preprocessing=_parameters(),
                    alff_falff=alff,
                ),
                expected_project_version=service.get_project(project.project_id).version,
            ),
            "internal-resolve",
        )
        plan = resolved.plan_revision
        service.approve_plan(
            plan.plan_revision_id,
            ApprovalCreate(
                expected_version=plan.version,
                plan_hash=plan.plan_hash,
                actor="authorized_synthetic_acceptance",
                decision="approved",
                reason="User-authorized synthetic software validation.",
            ),
            "internal-preprocessing-approval",
        )
        pre = service.create_run(
            RunCreate(
                project_id=project.project_id,
                plan_revision_id=plan.plan_revision_id,
                expected_plan_hash=plan.plan_hash,
                execution_backend="matlab",
                real_execution_confirmed=True,
            ),
            "internal-preprocessing-run",
        )
        (root / "preprocessing-run.json").write_text(
            pre.model_dump_json(indent=2), encoding="utf-8"
        )
        print(f"PREPROCESSING_QUEUED {pre.run_id}", flush=True)
        worker = build_worker(service, worker_id="internal-real-acceptance")
        assert worker.run_once()
        pre = service.get_run(pre.run_id)
        assert pre.state.value == "qc_review", pre.model_dump_json()
        review = service.repository.get_latest_qc_review_for_run(pre.run_id)
        assert review is not None and review.review.review_scope.value == "pre_statistics"
        assert review.review.included_subject_ids == subjects
        assert len(review.review.metric_artifact_ids) == 6
        # Only this script's controlled synthetic fixture is approved here.
        # Existing research runs and their human QC decisions are never touched.
        service.approve_qc_review(
            review.review.review_revision_id,
            QcReviewApprove(
                expected_review_version=review.version,
                expected_run_version=pre.version,
                review_hash=review.review.content_hash,
                actor="authorized_synthetic_acceptance",
                approved=True,
                reason="Reviewed controlled synthetic fixture and actual motion/metric evidence.",
            ),
            "internal-qc-approval",
        )
        _analyze(service, root, source_hashes, project, pre)
    finally:
        service.close()


if __name__ == "__main__":
    main()
