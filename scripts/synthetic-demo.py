"""Run a complete, synthetic-only backend demonstration.

The NIfTI-looking files are inert text placeholders. Typed Artifact metadata is
registered through an explicit test seam and must never be interpreted as real
image processing or statistical evidence.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import stat
from pathlib import Path
from typing import Any

from neuroagent.application.contracts import (
    ApprovalCreate,
    ApprovalDecision,
    DatasetCreate,
    ManifestScanRequest,
    PlanRevisionCreate,
    PlanValidationRequest,
    ProjectCreate,
    QcReviewApprove,
    QcReviewCreate,
    RunCreate,
    SkillPlanIntent,
    SkillPlanResolveRequest,
    StatisticalDesignCreate,
    StatisticalDesignValidationRequest,
    StatisticsRunCreate,
)
from neuroagent.application.hashing import content_hash
from neuroagent.application.reporting import build_statistical_reproducibility_report
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
    RegisteredArtifactMetadata,
    StatisticalArtifactRole,
    StatisticalDesignRevision,
    StatisticalMapType,
    StatisticalResultManifest,
    StatisticalResultMode,
    StatisticalTest,
    Tail,
    TemporalFilterTiming,
)
from neuroagent.domain.fmri.qc import QcCheck, QcSeverity
from neuroagent.infrastructure.persistence.repository import SqliteRepository

SUBJECT_IDS = ("sub-01", "sub-02", "sub-03")
SYNTHETIC_NOTICE = (
    "SYNTHETIC / NON-SCIENTIFIC DEMO: no real fMRI preprocessing, statistics, "
    "clinical inference, t values, p values, or significance claims are produced."
)


def _write_read_only_placeholder(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_bytes() != payload:
            raise RuntimeError(f"existing synthetic placeholder differs: {path.name}")
    else:
        path.write_bytes(payload)
    path.chmod(stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)


def _create_synthetic_bids(source: Path) -> tuple[Path, ...]:
    dataset = source / "synthetic_bids"
    description = json.dumps(
        {
            "Name": "SYNTHETIC NON-SCIENTIFIC rs-fMRI demo",
            "BIDSVersion": "1.9.0",
            "DatasetType": "raw",
            "GeneratedBy": [{"Name": "rs-fMRI Agent synthetic demo"}],
        },
        sort_keys=True,
    ).encode("utf-8")
    paths = [dataset / "dataset_description.json"]
    _write_read_only_placeholder(paths[0], description)
    for subject_id in SUBJECT_IDS:
        functional = dataset / subject_id / "func" / f"{subject_id}_task-rest_bold.nii.gz"
        anatomical = dataset / subject_id / "anat" / f"{subject_id}_T1w.nii.gz"
        _write_read_only_placeholder(
            functional,
            f"SYNTHETIC NON-SCIENTIFIC PLACEHOLDER {subject_id} functional".encode(),
        )
        _write_read_only_placeholder(
            anatomical,
            f"SYNTHETIC NON-SCIENTIFIC PLACEHOLDER {subject_id} anatomical".encode(),
        )
        paths.extend((functional, anatomical))
    return tuple(paths)


def _source_snapshot(paths: tuple[Path, ...]) -> dict[str, tuple[str, int, int]]:
    return {
        path.as_posix(): (
            hashlib.sha256(path.read_bytes()).hexdigest(),
            path.stat().st_mtime_ns,
            path.stat().st_size,
        )
        for path in paths
    }


def _synthetic_provenance_names(*names: str) -> tuple[ParameterProvenance, ...]:
    return tuple(
        ParameterProvenance(
            name=name,
            source=ParameterSource.STUDY_PROTOCOL,
            evidence_ref=f"synthetic-non-scientific://demo/{name}",
        )
        for name in names
    )


def _mask_lineage(manifest_hash: str, *, producer_label: str) -> ArtifactLineage:
    return ArtifactLineage(
        artifact_id="bound-by-test-only-registration",
        kind=ArtifactKind.BRAIN_MASK,
        metadata_verified=True,
        metadata_evidence_hash=content_hash(
            {"synthetic": True, "non_scientific": True, "producer": producer_label}
        ),
        subject_manifest_hash=manifest_hash,
        space="SYNTHETIC-MNI-LIKE-NON-SCIENTIFIC",
        grid_signature="synthetic-grid-3mm",
        voxel_size_mm=(3.0, 3.0, 3.0),
        mask_artifact_id=None,
        mask_grid_signature=None,
        temporally_filtered=False,
        frequency_band=None,
        spatially_smoothed=False,
        smoothing_fwhm_mm=None,
        scrubbed=False,
        producer_step_hash=content_hash(
            {"synthetic_step": producer_label, "scientific_use": "prohibited"}
        ),
    )


def _functional_lineage(manifest_hash: str, mask_artifact_id: str) -> ArtifactLineage:
    return ArtifactLineage(
        artifact_id="bound-by-test-only-registration",
        kind=ArtifactKind.FUNCTIONAL_TIMESERIES,
        metadata_verified=True,
        tr_seconds=2.0,
        volume_count=120,
        metadata_evidence_hash=content_hash(
            {"synthetic_header_check": True, "scientific_use": "prohibited"}
        ),
        subject_manifest_hash=manifest_hash,
        space="SYNTHETIC-MNI-LIKE-NON-SCIENTIFIC",
        grid_signature="synthetic-grid-3mm",
        voxel_size_mm=(3.0, 3.0, 3.0),
        mask_artifact_id=mask_artifact_id,
        mask_grid_signature="synthetic-grid-3mm",
        temporally_filtered=False,
        frequency_band=None,
        spatially_smoothed=False,
        smoothing_fwhm_mm=None,
        scrubbed=False,
        producer_step_hash=content_hash(
            {"synthetic_step": "verified-functional", "scientific_use": "prohibited"}
        ),
    )


def _metric_lineage(
    manifest_hash: str,
    subject_id: str,
    mask_artifact_id: str,
) -> ArtifactLineage:
    return ArtifactLineage(
        artifact_id="bound-by-test-only-registration",
        kind=ArtifactKind.ALFF_MAP,
        subject_id=subject_id,
        session_id=None,
        condition=None,
        metric_scaling=MetricScaling.Z_SCORE,
        metadata_verified=True,
        tr_seconds=2.0,
        volume_count=120,
        metadata_evidence_hash=content_hash(
            {
                "synthetic_metric_header": subject_id,
                "scientific_use": "prohibited",
            }
        ),
        subject_manifest_hash=manifest_hash,
        space="SYNTHETIC-MNI-LIKE-NON-SCIENTIFIC",
        grid_signature="synthetic-grid-3mm",
        voxel_size_mm=(3.0, 3.0, 3.0),
        mask_artifact_id=mask_artifact_id,
        mask_grid_signature="synthetic-grid-3mm",
        temporally_filtered=True,
        frequency_band=FrequencyBand(low_hz=0.01, high_hz=0.08),
        spatially_smoothed=False,
        smoothing_fwhm_mm=None,
        scrubbed=False,
        producer_step_hash=content_hash(
            {
                "synthetic_step": "alff-placeholder",
                "subject_id": subject_id,
                "scientific_use": "prohibited",
            }
        ),
    )


def _test_repository(service: NeuroAgentService) -> SqliteRepository:
    """Return the explicit internal test seam; no public Artifact write API is added."""

    if not isinstance(service.repository, SqliteRepository):
        raise RuntimeError("synthetic demo requires the local SQLite test repository")
    return service.repository


def _inject_verified_input_artifacts(
    service: NeuroAgentService,
    *,
    project_id: str,
    run_id: str,
    manifest_hash: str,
) -> tuple[str, str]:
    """Test-only injection of synthetic typed mask and functional metadata."""

    repository = _test_repository(service)
    repository.register_artifacts(
        project_id,
        run_id,
        (
            {
                "artifact_type": "synthetic.non_scientific.mask.verified",
                "relative_path": "synthetic_non_scientific/input_mask_placeholder.nii.gz",
                "checksum": content_hash({"synthetic": "input-mask"}),
                "size_bytes": 1,
                "provenance": {
                    "synthetic": True,
                    "scientific_use": "prohibited",
                    "notice": SYNTHETIC_NOTICE,
                    "lineage": _mask_lineage(
                        manifest_hash, producer_label="input-mask-placeholder"
                    ).model_dump(mode="json"),
                },
            },
        ),
    )
    mask = next(
        artifact
        for artifact in service.list_artifacts(run_id)
        if artifact.artifact_type == "synthetic.non_scientific.mask.verified"
    )
    repository.register_artifacts(
        project_id,
        run_id,
        (
            {
                "artifact_type": "synthetic.non_scientific.timeseries.verified",
                "relative_path": "synthetic_non_scientific/functional_placeholder.nii.gz",
                "checksum": content_hash({"synthetic": "verified-functional"}),
                "size_bytes": 1,
                "provenance": {
                    "synthetic": True,
                    "scientific_use": "prohibited",
                    "notice": SYNTHETIC_NOTICE,
                    "lineage": _functional_lineage(manifest_hash, mask.artifact_id).model_dump(
                        mode="json"
                    ),
                },
            },
        ),
    )
    functional = next(
        artifact
        for artifact in service.list_artifacts(run_id)
        if artifact.artifact_type == "synthetic.non_scientific.timeseries.verified"
    )
    return functional.artifact_id, mask.artifact_id


def _inject_synthetic_alff_artifacts(
    service: NeuroAgentService,
    *,
    project_id: str,
    run_id: str,
    manifest_hash: str,
) -> tuple[str, tuple[str, ...]]:
    """Test-only registration of an analysis mask and three non-scientific ALFF maps."""

    repository = _test_repository(service)
    repository.register_artifacts(
        project_id,
        run_id,
        (
            {
                "artifact_type": "synthetic.non_scientific.analysis_mask.verified",
                "relative_path": "synthetic_non_scientific/analysis_mask_placeholder.nii.gz",
                "checksum": content_hash({"synthetic": "analysis-mask"}),
                "size_bytes": 1,
                "provenance": {
                    "synthetic": True,
                    "scientific_use": "prohibited",
                    "notice": SYNTHETIC_NOTICE,
                    "lineage": _mask_lineage(
                        manifest_hash, producer_label="analysis-mask-placeholder"
                    ).model_dump(mode="json"),
                },
            },
        ),
    )
    mask = next(
        artifact
        for artifact in service.list_artifacts(run_id)
        if artifact.artifact_type == "synthetic.non_scientific.analysis_mask.verified"
    )
    repository.register_artifacts(
        project_id,
        run_id,
        tuple(
            {
                "artifact_type": "synthetic.non_scientific.alff",
                "relative_path": (f"synthetic_non_scientific/{subject_id}_alff_placeholder.nii.gz"),
                "checksum": content_hash(
                    {"synthetic_alff_placeholder": subject_id, "scientific_use": "prohibited"}
                ),
                "size_bytes": 1,
                "provenance": {
                    "synthetic": True,
                    "scientific_use": "prohibited",
                    "notice": SYNTHETIC_NOTICE,
                    "lineage": _metric_lineage(
                        manifest_hash,
                        subject_id,
                        mask.artifact_id,
                    ).model_dump(mode="json"),
                },
            }
            for subject_id in SUBJECT_IDS
        ),
    )
    metrics = tuple(
        artifact.artifact_id
        for artifact in service.list_artifacts(run_id)
        if artifact.artifact_type == "synthetic.non_scientific.alff"
    )
    return mask.artifact_id, metrics


def _approve_plan(
    service: NeuroAgentService,
    plan_revision_id: str,
    *,
    idempotency_key: str,
    reason: str,
) -> None:
    plan = service.get_plan(plan_revision_id)
    service.approve_plan(
        plan_revision_id,
        ApprovalCreate(
            expected_version=plan.version,
            plan_hash=plan.plan_hash,
            actor="synthetic-demo-reviewer",
            decision=ApprovalDecision.APPROVED,
            reason=reason,
        ),
        idempotency_key,
    )


def _alff_intent(
    *,
    project_id: str,
    dataset_id: str,
    manifest_hash: str,
    functional_artifact_id: str,
    mask_artifact_id: str,
) -> SkillPlanIntent:
    return SkillPlanIntent(
        project_id=project_id,
        dataset_ref=dataset_id,
        input_manifest_hash=manifest_hash,
        requested_metrics=(MetricKind.ALFF,),
        primary_outputs=("synthetic-zALFF-placeholder",),
        input_artifact_id=functional_artifact_id,
        alff_falff=AlffFalffParameters(
            tr_seconds=2.0,
            frequency_band=FrequencyBand(low_hz=0.01, high_hz=0.08),
            requested_metrics=(MetricKind.ALFF,),
            requested_scalings=(MetricScaling.Z_SCORE,),
            mask_artifact_id=mask_artifact_id,
            filter_timing=TemporalFilterTiming.AFTER_NORMALIZE,
            result_smoothing=False,
            result_smoothing_fwhm_mm=None,
            provenance=_synthetic_provenance_names(
                "tr_seconds",
                "frequency_band",
                "requested_metrics",
                "requested_scalings",
                "filter_timing",
                "result_smoothing",
            ),
        ),
        reho=None,
        study_protocol_ref="synthetic-non-scientific://demo/alff",
    )


def run_demo(root: Path) -> dict[str, Any]:
    """Execute the full synthetic backend flow and return a JSON-ready summary."""

    root = root.resolve()
    source = root / "source"
    work = root / "work"
    source.mkdir(parents=True, exist_ok=True)
    work.mkdir(parents=True, exist_ok=True)
    source_files = _create_synthetic_bids(source)
    source_before = _source_snapshot(source_files)
    dataset_path = source / "synthetic_bids"
    database_path = root / "synthetic-demo.sqlite"
    service = build_service(
        Settings(
            database_url=f"sqlite:///{database_path.as_posix()}",
            allowed_source_roots=[source],
            allowed_work_root=work,
            matlab_executable=root / "missing" / "matlab.exe",
            spm_dir=root / "missing" / "spm12",
            dpabi_dir=root / "missing" / "dpabi",
            worker_lease_seconds=1,
        ),
        providers={},
    )
    try:
        worker = build_worker(service, worker_id="synthetic-demo-mock-worker")
        project = service.create_project(
            ProjectCreate(
                name="SYNTHETIC NON-SCIENTIFIC demo",
                source_roots=[str(source)],
                work_root=str(work / "project"),
            ),
            "synthetic-demo-project",
        )
        dataset = service.create_dataset(
            project.project_id,
            DatasetCreate(
                name="SYNTHETIC BIDS placeholders",
                source_path=str(dataset_path),
                expected_project_version=project.version,
            ),
            "synthetic-demo-dataset",
        )
        manifest = service.inspect_dataset(
            dataset.dataset_id,
            ManifestScanRequest(expected_dataset_version=dataset.version),
            "synthetic-demo-manifest",
        )

        project = service.get_project(project.project_id)
        environment_hash = service.environment_provider.current().snapshot.environment_hash
        input_plan = service.create_plan(
            PlanRevisionCreate(
                project_id=project.project_id,
                expected_project_version=project.version,
                plan={
                    "synthetic_fixture": True,
                    "scientific_use": "prohibited",
                    "steps": [{"tool_id": "mock.safe.v1"}],
                },
                manifest_hash=manifest.content_hash,
                environment_hash=environment_hash,
            ),
            "synthetic-demo-input-plan",
        )
        input_plan = service.validate_plan(
            input_plan.plan_revision_id,
            PlanValidationRequest(expected_version=input_plan.version),
            "synthetic-demo-input-plan-validate",
        )
        _approve_plan(
            service,
            input_plan.plan_revision_id,
            idempotency_key="synthetic-demo-input-plan-approve",
            reason="Approve synthetic Mock input fixture; no scientific processing.",
        )
        input_plan = service.get_plan(input_plan.plan_revision_id)
        input_run = service.create_run(
            RunCreate(
                project_id=project.project_id,
                plan_revision_id=input_plan.plan_revision_id,
                expected_plan_hash=input_plan.plan_hash,
            ),
            "synthetic-demo-input-run",
        )
        if not worker.run_once():
            raise RuntimeError("synthetic input Mock run was not claimed")
        input_run = service.get_run(input_run.run_id)
        functional_artifact_id, input_mask_artifact_id = _inject_verified_input_artifacts(
            service,
            project_id=project.project_id,
            run_id=input_run.run_id,
            manifest_hash=manifest.content_hash,
        )

        project = service.get_project(project.project_id)
        alff_resolution = service.resolve_skill_plan(
            SkillPlanResolveRequest(
                request=_alff_intent(
                    project_id=project.project_id,
                    dataset_id=dataset.dataset_id,
                    manifest_hash=manifest.content_hash,
                    functional_artifact_id=functional_artifact_id,
                    mask_artifact_id=input_mask_artifact_id,
                ),
                expected_project_version=project.version,
            ),
            "synthetic-demo-alff-resolve",
        )
        _approve_plan(
            service,
            alff_resolution.plan_revision.plan_revision_id,
            idempotency_key="synthetic-demo-alff-approve",
            reason="Approve synthetic ALFF Skill plan for Mock execution only.",
        )
        alff_plan = service.get_plan(alff_resolution.plan_revision.plan_revision_id)
        alff_run = service.create_run(
            RunCreate(
                project_id=project.project_id,
                plan_revision_id=alff_plan.plan_revision_id,
                expected_plan_hash=alff_plan.plan_hash,
            ),
            "synthetic-demo-alff-run",
        )
        if not worker.run_once():
            raise RuntimeError("synthetic ALFF Mock run was not claimed")
        alff_run = service.get_run(alff_run.run_id)
        analysis_mask_id, metric_artifact_ids = _inject_synthetic_alff_artifacts(
            service,
            project_id=project.project_id,
            run_id=alff_run.run_id,
            manifest_hash=manifest.content_hash,
        )

        qc_review = service.create_qc_review(
            QcReviewCreate(
                run_id=alff_run.run_id,
                expected_run_version=alff_run.version,
                metric_artifact_ids=metric_artifact_ids,
                checks=(
                    QcCheck(
                        code="SYNTHETIC_TYPED_LINEAGE_ONLY",
                        severity=QcSeverity.BLOCKING,
                        passed=True,
                        evidence_artifact_ids=(analysis_mask_id,),
                        message=(
                            "Synthetic metadata contracts align; this is not image QC or "
                            "scientific evidence."
                        ),
                    ),
                ),
                included_subject_ids=SUBJECT_IDS,
                excluded_subject_ids=(),
                exclusion_reasons=(),
            ),
            "synthetic-demo-qc-create",
        )
        qc_review = service.approve_qc_review(
            qc_review.review.review_revision_id,
            QcReviewApprove(
                expected_review_version=qc_review.version,
                expected_run_version=alff_run.version,
                review_hash=qc_review.review.content_hash,
                actor="synthetic-demo-qc-reviewer",
                approved=True,
                reason="Synthetic contract-path approval only; no scientific QC performed.",
            ),
            "synthetic-demo-qc-approve",
        )
        alff_run = service.get_run(alff_run.run_id)

        images = tuple(
            AnalysisImage(
                subject_id=subject_id,
                artifact_id=artifact_id,
                group=None,
                condition=None,
            )
            for subject_id, artifact_id in zip(
                SUBJECT_IDS,
                metric_artifact_ids,
                strict=True,
            )
        )
        design = StatisticalDesignRevision(
            revision_id="synthetic-one-sample-design",
            test=StatisticalTest.ONE_SAMPLE_T,
            subject_order=SUBJECT_IDS,
            images=images,
            group_order=(),
            condition_order=(),
            covariates=(),
            contrast=(1.0,),
            one_sample_baseline=0.0,
            mask_artifact_id=analysis_mask_id,
            tail=Tail.TWO_SIDED,
            missing_value_policy=MissingValuePolicy.ERROR,
            qc_review_revision_id=qc_review.review.review_revision_id,
            qc_review_hash=qc_review.review.content_hash,
        )
        project = service.get_project(project.project_id)
        correction = FdrCorrection(
            method="fdr",
            q_threshold=0.05,
            mask_artifact_id=analysis_mask_id,
            statistic_type=StatisticalMapType.T,
            df1=2.0,
            df2=None,
        )
        statistical_design = service.create_statistical_design(
            StatisticalDesignCreate(
                project_id=project.project_id,
                expected_project_version=project.version,
                input_manifest_hash=manifest.content_hash,
                design=design,
                correction=correction,
            ),
            "synthetic-demo-statistics-create",
        )
        statistical_design = service.validate_statistical_design(
            statistical_design.plan_revision.plan_revision_id,
            StatisticalDesignValidationRequest(
                expected_version=statistical_design.plan_revision.version
            ),
            "synthetic-demo-statistics-validate",
        )
        _approve_plan(
            service,
            statistical_design.plan_revision.plan_revision_id,
            idempotency_key="synthetic-demo-statistics-approve",
            reason=(
                "Approve synthetic one-sample/FDR contract for Mock execution; "
                "no numerical inference."
            ),
        )
        statistical_plan = service.get_plan(statistical_design.plan_revision.plan_revision_id)
        statistics_run = service.create_statistics_run(
            StatisticsRunCreate(
                project_id=project.project_id,
                statistical_design_revision_id=statistical_plan.plan_revision_id,
                expected_plan_hash=statistical_plan.plan_hash,
            ),
            "synthetic-demo-statistics-run",
        )
        if not worker.run_once():
            raise RuntimeError("synthetic statistics Mock run was not claimed")
        statistics_run = service.get_run(statistics_run.run_id)

        result_roles = tuple(StatisticalArtifactRole)
        result_manifest = StatisticalResultManifest(
            result_id="synthetic-non-scientific-statistical-result",
            run_id=statistics_run.run_id,
            design_revision_id=design.revision_id,
            mode=StatisticalResultMode.SYNTHETIC_NON_SCIENTIFIC,
            non_scientific=True,
            non_scientific_reason=(
                "The demo exercises contracts with placeholders and produces no "
                "statistical values or scientific evidence."
            ),
            correction=correction,
            cluster_connectivity_definition=(
                "synthetic fixture only; no cluster inference or connectivity was applied"
            ),
            artifacts=tuple(
                RegisteredArtifactMetadata(
                    artifact_id=f"synthetic-placeholder-{role.value}",
                    role=role,
                    artifact_type=f"synthetic.non_scientific.{role.value}",
                    relative_path=f"synthetic_non_scientific/{role.value}.placeholder",
                    placeholder=True,
                )
                for role in result_roles
            ),
            clusters=(),
        )
        report = build_statistical_reproducibility_report(
            manifest=result_manifest,
            design=design,
            correction=correction,
            qc_review_hash=qc_review.review.content_hash,
            environment_hash=statistical_plan.environment_hash,
            plan_hash=statistical_plan.plan_hash,
        )
        report_directory = work / "project" / "reports" / statistics_run.run_id
        report_directory.mkdir(parents=True, exist_ok=True)
        report_json_path = report_directory / "synthetic-report.json"
        report_markdown_path = report_directory / "synthetic-report.md"
        report_json_path.write_text(report.json_text, encoding="utf-8", newline="\n")
        report_markdown_path.write_text(report.markdown, encoding="utf-8", newline="\n")
        report_payloads = (
            ("json", report_json_path, report.json_text),
            ("markdown", report_markdown_path, report.markdown),
        )
        _test_repository(service).register_artifacts(
            project.project_id,
            statistics_run.run_id,
            tuple(
                {
                    "artifact_type": f"synthetic.non_scientific.report.{format_name}",
                    "relative_path": (f"reports/{statistics_run.run_id}/{report_path.name}"),
                    "checksum": hashlib.sha256(payload.encode("utf-8")).hexdigest(),
                    "size_bytes": len(payload.encode("utf-8")),
                    "provenance": {
                        "synthetic": True,
                        "scientific_use": "prohibited",
                        "notice": SYNTHETIC_NOTICE,
                        "report_bundle_hash": report.bundle_hash,
                    },
                }
                for format_name, report_path, payload in report_payloads
            ),
        )
        registered_result = service.register_statistical_result(
            run_id=statistics_run.run_id,
            manifest=result_manifest,
            design=design,
            correction=correction,
            qc_review_hash=qc_review.review.content_hash,
            environment_hash=statistical_plan.environment_hash,
            plan_hash=statistical_plan.plan_hash,
            actor="synthetic-demo",
        )

        source_after = _source_snapshot(source_files)
        input_artifacts = service.list_artifacts(input_run.run_id)
        alff_artifacts = service.list_artifacts(alff_run.run_id)
        statistics_artifacts = service.list_artifacts(statistics_run.run_id)
        return {
            "synthetic": True,
            "scientific_use": "prohibited",
            "notice": SYNTHETIC_NOTICE,
            "source": {
                "read_only_placeholders": all(
                    path.stat().st_mode & stat.S_IWUSR == 0 for path in source_files
                ),
                "unchanged_during_pipeline": source_before == source_after,
                "file_count": len(source_files),
            },
            "project_id": project.project_id,
            "dataset": {
                "dataset_id": dataset.dataset_id,
                "manifest_id": manifest.manifest_id,
                "manifest_hash": manifest.content_hash,
                "subject_ids": [entry.subject_id for entry in manifest.subjects],
            },
            "plans": {
                "input": {
                    "plan_revision_id": input_plan.plan_revision_id,
                    "state": input_plan.state.value,
                },
                "alff": {
                    "plan_revision_id": alff_plan.plan_revision_id,
                    "state": alff_plan.state.value,
                    "skill_ids": [lock.skill_id for lock in alff_resolution.skill_plan.skill_locks],
                },
                "statistics": {
                    "plan_revision_id": statistical_plan.plan_revision_id,
                    "state": statistical_plan.state.value,
                    "test": StatisticalTest.ONE_SAMPLE_T.value,
                    "correction": "fdr",
                },
            },
            "runs": {
                "input": {
                    "run_id": input_run.run_id,
                    "state": input_run.state.value,
                    "executor": "mock",
                },
                "alff": {
                    "run_id": alff_run.run_id,
                    "state": alff_run.state.value,
                    "executor": "mock",
                },
                "statistics": {
                    "run_id": statistics_run.run_id,
                    "state": statistics_run.state.value,
                    "executor": "mock",
                },
            },
            "qc": {
                "review_revision_id": qc_review.review.review_revision_id,
                "state": qc_review.state.value,
                "included_subject_ids": list(qc_review.review.included_subject_ids),
            },
            "artifacts": {
                "injection_mode": "test-only typed metadata; no image calculation",
                "input_functional_artifact_id": functional_artifact_id,
                "input_mask_artifact_id": input_mask_artifact_id,
                "analysis_mask_artifact_id": analysis_mask_id,
                "alff_metric_artifact_ids": list(metric_artifact_ids),
                "input_types": [artifact.artifact_type for artifact in input_artifacts],
                "alff_types": [artifact.artifact_type for artifact in alff_artifacts],
                "statistics_types": [artifact.artifact_type for artifact in statistics_artifacts],
            },
            "report": {
                "mode": StatisticalResultMode.SYNTHETIC_NON_SCIENTIFIC.value,
                "scientific_use": "prohibited",
                "bundle_hash": report.bundle_hash,
                "formats": ["json", "markdown"],
                "evidence": "placeholder contracts only; no statistical values",
            },
            "statistical_result": {
                "result_id": registered_result.result_id,
                "run_id": registered_result.run_id,
                "mode": registered_result.mode,
                "bundle_hash": registered_result.bundle_hash,
                "non_scientific": registered_result.non_scientific,
            },
        }
    finally:
        service.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path("tmp/synthetic-demo"))
    args = parser.parse_args()
    print(json.dumps(run_demo(args.root), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
