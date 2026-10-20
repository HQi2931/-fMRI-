from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import inspect, text

from neuroagent.application.contracts.plans import SkillPlanIntent, SkillPlanResolveRequest
from neuroagent.application.contracts.projects import DatasetCreate, ManifestScanRequest
from neuroagent.application.errors import ConflictError, InputValidationError
from neuroagent.application.services import ApplicationServices
from neuroagent.application.work_draft import (
    WorkDraft,
    draft_hash,
    preflight_skill_intent,
)
from neuroagent.domain.fmri.metrics import MetricKind
from tests.backend.conftest import make_bids_dataset, make_project
from tests.backend.test_science_agent_api import (
    _lineage,
    _minimal_preprocessing_payload,
    _skill_request,
)


def _complete_preprocessing_draft() -> WorkDraft:
    values = _minimal_preprocessing_payload()
    values.pop("provenance")
    return WorkDraft(
        preprocessing=values,
        primary_outputs=["preprocessed_bold"],
        qc_mode="skip",
    )


def _on_results_metric_draft() -> WorkDraft:
    preprocessing = _minimal_preprocessing_payload()
    preprocessing.pop("provenance")
    normalization = preprocessing["normalization"]
    assert isinstance(normalization, dict)
    normalization.update(
        {
            "mode": 1,
            "timing": "on_results",
            "bounding_box_mm": [[-90.0, -126.0, -72.0], [90.0, 90.0, 108.0]],
            "voxel_size_mm": [3.0, 3.0, 3.0],
        }
    )
    return WorkDraft(
        preprocessing=preprocessing,
        alff_falff={
            "tr_seconds": 2.0,
            "frequency_band": {"low_hz": 0.01, "high_hz": 0.08},
            "requested_metrics": ["alff"],
            "requested_scalings": ["z_score"],
            "mask_source": "registered_artifact",
            "mask_artifact_id": "mask-001",
            "filter_timing": "disabled",
            "result_smoothing": False,
            "result_smoothing_fwhm_mm": None,
        },
        requested_metrics=[MetricKind.ALFF],
        primary_outputs=["zALFF"],
        qc_mode="skip",
    )


def _result_stage_without_metric_draft(*, normalization: bool, smoothing: bool) -> WorkDraft:
    preprocessing = _minimal_preprocessing_payload()
    preprocessing.pop("provenance")
    if normalization:
        normalization_values = preprocessing["normalization"]
        assert isinstance(normalization_values, dict)
        normalization_values.update(
            {
                "mode": 1,
                "timing": "on_results",
                "bounding_box_mm": [[-90.0, -126.0, -72.0], [90.0, 90.0, 108.0]],
                "voxel_size_mm": [3.0, 3.0, 3.0],
            }
        )
    if smoothing:
        preprocessing["smoothing"] = {
            "timing": "on_results",
            "method": 1,
            "fwhm_mm": [6.0, 6.0, 6.0],
        }
    return WorkDraft(
        preprocessing=preprocessing,
        primary_outputs=["preprocessed_bold"],
        qc_mode="skip",
    )


def _reho_cut_draft() -> WorkDraft:
    preprocessing = _minimal_preprocessing_payload()
    preprocessing.pop("provenance")
    preprocessing["realignment"] = {
        "enabled": True,
        "options_source": "dpabi_v82_jobmat",
    }
    preprocessing["temporal_filter"] = {
        "timing": "after_normalize",
        "frequency_band": {"low_hz": 0.01, "high_hz": 0.08},
        "add_mean_back": True,
    }
    preprocessing["scrubbing"] = {
        "enabled": True,
        "timing": "after_preprocessing",
        "censoring": {
            "fd_type": "fd_jenkinson",
            "fd_threshold_mm": 0.2,
            "previous_points": 1,
            "later_points": 2,
        },
        "method": "cut",
    }
    return WorkDraft(
        preprocessing=preprocessing,
        reho={
            "tr_seconds": 2.0,
            "temporal_filter_band": {"low_hz": 0.01, "high_hz": 0.08},
            "temporal_filter_add_mean_back": True,
            "cluster_voxels": 27,
            "mask_source": "registered_artifact",
            "mask_artifact_id": "mask-001",
            "requested_scalings": ["z_score"],
            "smooth_reho": False,
            "smooth_reho_fwhm_mm": None,
            "global_result_smoothing": False,
            "global_result_smoothing_fwhm_mm": None,
        },
        requested_metrics=[MetricKind.REHO],
        primary_outputs=["zReHo"],
        qc_mode="skip",
    )


def _scanned_dataset(
    service: ApplicationServices,
    source_root: Path,
    work_root: Path,
    *,
    dataset_path: Path | None = None,
):
    project = make_project(service, source_root, work_root, key="dry-run-project")
    bids = dataset_path or make_bids_dataset(source_root)
    dataset = service.projects.create_dataset(
        project.project_id,
        DatasetCreate(
            name="Dry-run BIDS",
            source_path=str(bids),
            expected_project_version=project.version,
        ),
        "dry-run-dataset",
    )
    manifest = service.projects.inspect_dataset(
        dataset.dataset_id,
        ManifestScanRequest(expected_dataset_version=dataset.version),
        "dry-run-manifest",
    )
    return (
        service.projects.get_project(project.project_id),
        service.projects.get_dataset(dataset.dataset_id),
        manifest,
    )


def _two_session_bids_dataset(root: Path) -> Path:
    dataset = root / "bids-two-sessions"
    dataset.mkdir()
    (dataset / "dataset_description.json").write_text(
        '{"Name":"synthetic","BIDSVersion":"1.9.0"}', encoding="utf-8"
    )
    for session in ("ses-01", "ses-02"):
        subject = "sub-01"
        functional = dataset / subject / session / "func"
        anatomical = dataset / subject / "anat"
        functional.mkdir(parents=True)
        anatomical.mkdir(parents=True, exist_ok=True)
        (functional / f"{subject}_{session}_task-rest_bold.nii.gz").write_bytes(
            f"synthetic-{subject}-{session}-bold".encode()
        )
        (anatomical / f"{subject}_T1w.nii.gz").write_bytes(f"synthetic-{subject}-t1".encode())
    return dataset


def _single_subject_bids_dataset(root: Path) -> Path:
    dataset = root / "bids-single-subject"
    functional = dataset / "sub-01" / "func"
    anatomical = dataset / "sub-01" / "anat"
    functional.mkdir(parents=True)
    anatomical.mkdir(parents=True)
    (dataset / "dataset_description.json").write_text(
        '{"Name":"synthetic","BIDSVersion":"1.9.0"}', encoding="utf-8"
    )
    (functional / "sub-01_task-rest_bold.nii.gz").write_bytes(b"synthetic-sub-01-bold")
    (anatomical / "sub-01_T1w.nii.gz").write_bytes(b"synthetic-sub-01-t1")
    return dataset


def _metric_only_request(project, dataset, manifest, artifact_id: str):
    intent = _skill_request(
        project.project_id,
        dataset.dataset_id,
        manifest.content_hash,
        artifact_id,
    ).model_copy(
        update={
            "requested_metrics": (MetricKind.ALFF,),
            "primary_outputs": ("zALFF",),
            "reho": None,
        }
    )
    return SkillPlanResolveRequest(
        request=intent,
        expected_project_version=project.version,
    )


def _request(project, dataset, manifest, draft: WorkDraft) -> SkillPlanResolveRequest:
    return SkillPlanResolveRequest(
        request=preflight_skill_intent(
            draft,
            project_id=project.project_id,
            dataset_ref=dataset.dataset_id,
            input_manifest_hash=manifest.content_hash,
            structural_artifact_id=f"manifest:{manifest.manifest_id}:anatomical-source",
        ),
        expected_project_version=project.version,
        validation_mode="strict",
    )


def _table_counts(service: ApplicationServices) -> dict[str, int]:
    engine = service.database.engine
    tables = inspect(engine).get_table_names()
    with engine.connect() as connection:
        return {
            table: int(connection.execute(text(f'SELECT COUNT(*) FROM "{table}"')).scalar_one())
            for table in tables
        }


def test_work_draft_dry_run_uses_resolver_validator_and_compiler_without_writes(
    service: ApplicationServices,
    source_root: Path,
    work_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, dataset, manifest = _scanned_dataset(service, source_root, work_root)
    assert len(manifest.subjects) == 2
    assert {subject.session_id for subject in manifest.subjects} == {None}
    draft = _complete_preprocessing_draft()
    request = _request(project, dataset, manifest, draft)
    before = _table_counts(service)
    work_files_before = sorted(path.relative_to(work_root) for path in work_root.rglob("*"))
    calls = {"resolve": 0, "validate": 0, "compile": 0}

    resolver = service.skills.skill_resolver
    validator = service.skills.skill_validator
    compiler = service.skills.skill_compiler
    original_resolve = resolver.resolve
    original_validate = validator.validate_resolution
    original_compile = compiler.compile

    def resolve(*args, **kwargs):
        calls["resolve"] += 1
        return original_resolve(*args, **kwargs)

    def validate(*args, **kwargs):
        calls["validate"] += 1
        return original_validate(*args, **kwargs)

    def compile(*args, **kwargs):
        calls["compile"] += 1
        return original_compile(*args, **kwargs)

    monkeypatch.setattr(resolver, "resolve", resolve)
    monkeypatch.setattr(validator, "validate_resolution", validate)
    monkeypatch.setattr(compiler, "compile", compile)
    monkeypatch.setattr(
        service.skills.environment_provider,
        "current",
        lambda: pytest.fail("dry-run must not probe MATLAB/SPM/DPABI files"),
    )

    result = service.skills.dry_run_skill_plan(request)

    assert result.scope == "skill_contract_compile_only"
    assert result.environment_probed is False
    assert result.manifest_id == manifest.manifest_id
    assert result.manifest_hash == manifest.content_hash
    assert result.ready is True, [issue.model_dump() for issue in result.issues]
    assert result.plan_hash is not None
    assert calls["resolve"] == 1
    assert calls["validate"] >= 1
    assert calls["compile"] == 1
    assert _table_counts(service) == before
    assert sorted(path.relative_to(work_root) for path in work_root.rglob("*")) == work_files_before
    assert draft_hash(draft)  # The submitted draft remains an unapproved proposal.


def test_two_session_bids_preflight_blocks_before_plan_persistence_and_without_writes(
    service: ApplicationServices,
    source_root: Path,
    work_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, dataset, manifest = _scanned_dataset(
        service,
        source_root,
        work_root,
        dataset_path=_two_session_bids_dataset(source_root),
    )
    assert {subject.session_id for subject in manifest.subjects} == {"ses-01", "ses-02"}
    request = _request(project, dataset, manifest, _complete_preprocessing_draft())
    before = _table_counts(service)
    work_files_before = sorted(path.relative_to(work_root) for path in work_root.rglob("*"))

    result = service.skills.dry_run_skill_plan(request)

    assert result.ready is False
    assert result.plan_hash is None
    issue = next(
        item for item in result.issues if item.code == "MANIFEST_PREPROCESSING_SESSION_UNSUPPORTED"
    )
    assert issue.severity == "blocking"
    assert issue.path == "manifest.subjects.session_id"
    assert "请将每个 session 分别整理为独立输入" in issue.message
    assert _table_counts(service) == before
    assert sorted(path.relative_to(work_root) for path in work_root.rglob("*")) == work_files_before

    monkeypatch.setattr(
        service.skills.environment_provider,
        "current",
        lambda: pytest.fail(
            "unsupported session scope must be rejected before environment probing"
        ),
    )
    with pytest.raises(InputValidationError) as raised:
        service.skills.resolve_skill_plan(request, "two-session-preprocessing")

    assert raised.value.code == "manifest_preprocessing_session_unsupported"
    assert raised.value.details["path"] == "manifest.subjects.session_id"
    assert "remediation" in raised.value.details
    assert _table_counts(service)["plan_revisions"] == before["plan_revisions"]


@pytest.mark.parametrize(
    ("scope", "expected_path"),
    [
        ("multiple_subjects", "manifest.subjects"),
        ("multiple_sessions", "manifest.subjects.session_id"),
    ],
)
def test_metric_only_checkpoint_scope_blocks_resolve_before_plan_persistence(
    service: ApplicationServices,
    source_root: Path,
    work_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    scope: str,
    expected_path: str,
) -> None:
    dataset_path = _two_session_bids_dataset(source_root) if scope == "multiple_sessions" else None
    project, dataset, manifest = _scanned_dataset(
        service,
        source_root,
        work_root,
        dataset_path=dataset_path,
    )
    request = _metric_only_request(project, dataset, manifest, "unread-checkpoint")
    before = _table_counts(service)
    monkeypatch.setattr(
        service.skills,
        "get_artifact",
        lambda _artifact_id: pytest.fail("manifest scope must be checked before Artifact lookup"),
    )
    monkeypatch.setattr(
        service.skills.environment_provider,
        "current",
        lambda: pytest.fail("metric-only scope must be checked before environment probing"),
    )

    with pytest.raises(InputValidationError) as raised:
        service.skills.resolve_skill_plan(request, f"metric-only-scope-{scope}")

    assert raised.value.code == "metric_only_checkpoint_scope_unsupported"
    assert raised.value.details["path"] == expected_path
    assert raised.value.details["remediation"]
    assert _table_counts(service)["plan_revisions"] == before["plan_revisions"]


def test_metric_only_checkpoint_subject_mismatch_blocks_before_plan_persistence(
    service: ApplicationServices,
    source_root: Path,
    work_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    project, dataset, manifest = _scanned_dataset(
        service,
        source_root,
        work_root,
        dataset_path=_single_subject_bids_dataset(source_root),
    )
    artifact_id = "registered-checkpoint"
    lineage = _lineage(
        manifest_hash=manifest.content_hash,
        artifact_id=artifact_id,
        subject_id="sub-99",
    )
    artifact = SimpleNamespace(
        artifact_id=artifact_id,
        project_id=project.project_id,
        run_id="checkpoint-run",
        provenance={"lineage": lineage.model_dump(mode="json")},
    )
    monkeypatch.setattr(service.skills, "get_artifact", lambda _artifact_id: artifact)
    monkeypatch.setattr(
        service.skills,
        "get_run",
        lambda _run_id: SimpleNamespace(plan_revision_id="checkpoint-plan"),
    )
    monkeypatch.setattr(
        service.skills.repository,
        "get_plan",
        lambda _plan_revision_id: SimpleNamespace(manifest_hash=manifest.content_hash),
    )
    monkeypatch.setattr(
        service.skills.environment_provider,
        "current",
        lambda: pytest.fail("subject mismatch must be checked before environment probing"),
    )
    request = _metric_only_request(project, dataset, manifest, artifact_id)
    before = _table_counts(service)

    with pytest.raises(InputValidationError) as raised:
        service.skills.resolve_skill_plan(request, "metric-only-subject-mismatch")

    assert raised.value.code == "metric_only_checkpoint_scope_unsupported"
    assert raised.value.details["path"] == "input_artifact.lineage.subject_id"
    assert raised.value.details["manifest_subject_id"] == "sub-01"
    assert raised.value.details["artifact_subject_id"] == "sub-99"
    assert raised.value.details["remediation"]
    assert _table_counts(service)["plan_revisions"] == before["plan_revisions"]


def test_result_normalization_with_single_metric_blocks_dry_run_and_resolve_before_plan_write(
    service: ApplicationServices,
    source_root: Path,
    work_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    project, dataset, manifest = _scanned_dataset(service, source_root, work_root)
    request = _request(project, dataset, manifest, _on_results_metric_draft())
    before = _table_counts(service)
    work_files_before = sorted(path.relative_to(work_root) for path in work_root.rglob("*"))

    result = service.skills.dry_run_skill_plan(request)

    assert result.ready is False
    assert result.plan_hash is None
    issue = next(
        item
        for item in result.issues
        if item.code == "RESULT_NORMALIZATION_METRIC_MASK_SPACES_UNSUPPORTED"
    )
    assert issue.severity == "blocking"
    assert issue.path == "preprocessing.normalization.timing"
    assert "不判断真实图像或掩膜空间" in issue.message
    assert issue.remediation is not None
    assert "研究者决定" in issue.remediation
    assert _table_counts(service) == before
    assert sorted(path.relative_to(work_root) for path in work_root.rglob("*")) == work_files_before

    snapshot = service.skills._configured_environment_snapshot()
    monkeypatch.setattr(
        service.skills.environment_provider,
        "current",
        lambda: SimpleNamespace(snapshot=snapshot),
    )
    with pytest.raises(InputValidationError) as raised:
        service.skills.resolve_skill_plan(request, "on-results-metric-blocked")

    assert raised.value.code == "skill_plan_blocked"
    assert any(
        item["code"] == "RESULT_NORMALIZATION_METRIC_MASK_SPACES_UNSUPPORTED"
        and item["path"] == "preprocessing.normalization.timing"
        and item["remediation"]
        for item in raised.value.details["issues"]
    )
    assert _table_counts(service)["plan_revisions"] == before["plan_revisions"]


@pytest.mark.parametrize(
    ("normalization", "smoothing", "expected_paths"),
    [
        (True, False, {"preprocessing.normalization.timing"}),
        (False, True, {"preprocessing.smoothing.timing"}),
        (
            True,
            True,
            {
                "preprocessing.normalization.timing",
                "preprocessing.smoothing.timing",
            },
        ),
    ],
)
def test_result_stage_without_metric_blocks_dry_run_and_resolve_before_plan_write(
    service: ApplicationServices,
    source_root: Path,
    work_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    normalization: bool,
    smoothing: bool,
    expected_paths: set[str],
) -> None:
    from types import SimpleNamespace

    project, dataset, manifest = _scanned_dataset(service, source_root, work_root)
    draft = _result_stage_without_metric_draft(
        normalization=normalization,
        smoothing=smoothing,
    )
    request = _request(project, dataset, manifest, draft)
    before = _table_counts(service)
    result = service.skills.dry_run_skill_plan(request)

    assert result.ready is False
    assert result.plan_hash is None
    blockers = [
        issue
        for issue in result.issues
        if issue.code == "RESULT_STAGE_OPERATION_WITHOUT_METRIC_UNSUPPORTED"
    ]
    assert {issue.path for issue in blockers} == expected_paths
    assert all(issue.severity == "blocking" for issue in blockers)
    assert all(issue.remediation is not None for issue in blockers)
    assert all("指标输出" in issue.message for issue in blockers)
    assert _table_counts(service) == before

    snapshot = service.skills._configured_environment_snapshot()
    monkeypatch.setattr(
        service.skills.environment_provider,
        "current",
        lambda: SimpleNamespace(snapshot=snapshot),
    )
    with pytest.raises(InputValidationError) as raised:
        service.skills.resolve_skill_plan(
            request, f"result-stage-no-metric-{normalization}-{smoothing}"
        )

    assert raised.value.code == "skill_plan_blocked"
    resolve_issues = [
        item
        for item in raised.value.details["issues"]
        if item["code"] == "RESULT_STAGE_OPERATION_WITHOUT_METRIC_UNSUPPORTED"
    ]
    assert {item["path"] for item in resolve_issues} == expected_paths
    assert all(item["remediation"] for item in resolve_issues)
    assert _table_counts(service)["plan_revisions"] == before["plan_revisions"]


def test_reho_cut_checkpoint_gate_blocks_dry_run_and_resolve_before_plan_write(
    service: ApplicationServices,
    source_root: Path,
    work_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    project, dataset, manifest = _scanned_dataset(service, source_root, work_root)
    request = _request(project, dataset, manifest, _reho_cut_draft())
    before = _table_counts(service)
    result = service.skills.dry_run_skill_plan(request)

    assert result.ready is False
    assert result.plan_hash is None
    blockers = [issue for issue in result.issues if issue.code == "REHO_CUT_CHECKPOINT_UNSUPPORTED"]
    assert len(blockers) == 1
    assert blockers[0].path == "preprocessing.scrubbing.method"
    assert blockers[0].severity == "blocking"
    assert blockers[0].remediation
    assert any(issue.code == "EFFECTIVE_VOLUME_COUNT_UNKNOWN" for issue in result.issues)
    assert _table_counts(service) == before

    snapshot = service.skills._configured_environment_snapshot()
    monkeypatch.setattr(
        service.skills.environment_provider,
        "current",
        lambda: SimpleNamespace(snapshot=snapshot),
    )
    with pytest.raises(InputValidationError) as raised:
        service.skills.resolve_skill_plan(request, "reho-cut-checkpoint-preflight")

    assert raised.value.code == "skill_plan_blocked"
    resolve_issues = [
        item
        for item in raised.value.details["issues"]
        if item["code"] == "REHO_CUT_CHECKPOINT_UNSUPPORTED"
    ]
    assert len(resolve_issues) == 1
    assert resolve_issues[0]["path"] == "preprocessing.scrubbing.method"
    assert resolve_issues[0]["remediation"]
    assert _table_counts(service)["plan_revisions"] == before["plan_revisions"]


def test_non_metric_intent_does_not_apply_metric_only_checkpoint_scope(
    service: ApplicationServices,
    source_root: Path,
    work_root: Path,
) -> None:
    project, dataset, manifest = _scanned_dataset(
        service,
        source_root,
        work_root,
        dataset_path=_two_session_bids_dataset(source_root),
    )
    intent = SkillPlanIntent(
        project_id=project.project_id,
        dataset_ref=dataset.dataset_id,
        input_manifest_hash=manifest.content_hash,
        requested_metrics=(),
        primary_outputs=(),
        input_artifact_id="checkpoint-artifact",
        request_preprocessing=False,
        preprocessing=None,
    )

    service.skills._validate_manifest_for_skill_intent(intent, manifest)


def test_dry_run_rejects_manifest_change_during_compile_without_writes(
    service: ApplicationServices,
    source_root: Path,
    work_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, dataset, manifest = _scanned_dataset(service, source_root, work_root)
    request = _request(project, dataset, manifest, _complete_preprocessing_draft())
    before = _table_counts(service)
    original_get_dataset = service.skills.get_dataset
    calls = 0

    def changed_manifest_id(dataset_ref: str):
        nonlocal calls
        calls += 1
        current = original_get_dataset(dataset_ref)
        if calls == 2:
            return current.model_copy(update={"current_manifest_id": "changed-manifest"})
        return current

    monkeypatch.setattr(service.skills, "get_dataset", changed_manifest_id)

    with pytest.raises(ConflictError, match="manifest"):
        service.skills.dry_run_skill_plan(request)

    assert calls == 2
    assert _table_counts(service) == before


def test_resolve_checks_manifest_before_probing_environment(
    service: ApplicationServices,
    source_root: Path,
    work_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, dataset, manifest = _scanned_dataset(service, source_root, work_root)
    request = _request(project, dataset, manifest, _complete_preprocessing_draft())
    invalid_request = request.model_copy(
        update={"request": request.request.model_copy(update={"input_manifest_hash": "0" * 64})}
    )
    monkeypatch.setattr(
        service.skills.environment_provider,
        "current",
        lambda: pytest.fail("invalid manifest must be rejected before probing the environment"),
    )

    with pytest.raises(ConflictError, match="manifest"):
        service.skills.resolve_skill_plan(invalid_request, "manifest-before-environment")


def test_dry_run_rejects_project_version_change_during_compile(
    service: ApplicationServices,
    source_root: Path,
    work_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, dataset, manifest = _scanned_dataset(service, source_root, work_root)
    request = _request(project, dataset, manifest, _complete_preprocessing_draft())
    original_compile = service.skills.skill_compiler.compile
    plans_before = _table_counts(service)["plan_revisions"]

    def compile_then_change_project(resolution):
        compiled = original_compile(resolution)
        current_project = service.projects.get_project(project.project_id)
        service.projects.create_dataset(
            project.project_id,
            DatasetCreate(
                name="Concurrent dataset change",
                source_path=dataset.source_path,
                expected_project_version=current_project.version,
            ),
            "concurrent-dataset-change",
        )
        return compiled

    monkeypatch.setattr(service.skills.skill_compiler, "compile", compile_then_change_project)

    with pytest.raises(ConflictError, match="项目版本"):
        service.skills.dry_run_skill_plan(request)

    assert _table_counts(service)["plan_revisions"] == plans_before
    assert service.projects.get_project(project.project_id).version > project.version
