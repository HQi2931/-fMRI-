from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import inspect, text

from neuroagent.application.contracts.plans import SkillPlanResolveRequest
from neuroagent.application.contracts.projects import DatasetCreate, ManifestScanRequest
from neuroagent.application.errors import ConflictError
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


def _scanned_dataset(service: ApplicationServices, source_root: Path, work_root: Path):
    project = make_project(service, source_root, work_root, key="dry-run-project")
    bids = make_bids_dataset(source_root)
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
