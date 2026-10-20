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
from tests.backend.conftest import make_bids_dataset, make_project
from tests.backend.test_science_agent_api import _minimal_preprocessing_payload


def _complete_preprocessing_draft() -> WorkDraft:
    values = _minimal_preprocessing_payload()
    values.pop("provenance")
    return WorkDraft(
        preprocessing=values,
        primary_outputs=["preprocessed_bold"],
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


def test_metric_only_intent_does_not_apply_preprocessing_session_scope(
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
