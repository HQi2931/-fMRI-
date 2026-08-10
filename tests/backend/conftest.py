from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from neuroagent.application.contracts import (
    ApprovalCreate,
    ApprovalDecision,
    PlanRevisionCreate,
    PlanValidationRequest,
    ProjectCreate,
)
from neuroagent.application.services import NeuroAgentService
from neuroagent.application.settings import Settings
from neuroagent.bootstrap import build_service


@pytest.fixture
def source_root(tmp_path: Path) -> Path:
    root = tmp_path / "source"
    root.mkdir()
    return root


@pytest.fixture
def work_root(tmp_path: Path) -> Path:
    root = tmp_path / "work"
    root.mkdir()
    return root


@pytest.fixture
def service(tmp_path: Path, source_root: Path, work_root: Path) -> Iterator[NeuroAgentService]:
    settings = Settings(
        database_url=f"sqlite:///{(tmp_path / 'metadata.sqlite').as_posix()}",
        allowed_source_roots=[source_root],
        allowed_work_root=work_root,
        matlab_executable=tmp_path / "missing" / "matlab.exe",
        spm_dir=tmp_path / "missing" / "spm12",
        dpabi_dir=tmp_path / "missing" / "dpabi",
        worker_lease_seconds=1,
    )
    app_service = build_service(settings)
    yield app_service
    app_service.close()


def make_project(
    service: NeuroAgentService, source_root: Path, work_root: Path, *, key: str = "project-key"
):
    return service.create_project(
        ProjectCreate(
            name="Synthetic project",
            source_roots=[str(source_root)],
            work_root=str(work_root / "project"),
        ),
        key,
    )


def make_approved_plan(
    service: NeuroAgentService,
    project_id: str,
    *,
    manifest_hash: str = "a" * 64,
):
    plan = service.create_plan(
        PlanRevisionCreate(
            project_id=project_id,
            expected_project_version=service.get_project(project_id).version,
            plan={"skill_id": "test.mock", "steps": [{"capability": "mock.execute"}]},
            manifest_hash=manifest_hash,
            environment_hash="b" * 64,
        ),
        "plan-create-key",
    )
    plan = service.validate_plan(
        plan.plan_revision_id,
        PlanValidationRequest(expected_version=plan.version, issues=[]),
        "plan-validate-key",
    )
    service.approve_plan(
        plan.plan_revision_id,
        ApprovalCreate(
            expected_version=plan.version,
            plan_hash=plan.plan_hash,
            actor="researcher",
            decision=ApprovalDecision.APPROVED,
            reason="synthetic test plan reviewed",
        ),
        "plan-approve-key",
    )
    return service.get_plan(plan.plan_revision_id)


def make_bids_dataset(root: Path) -> Path:
    dataset = root / "bids"
    (dataset / "sub-01" / "func").mkdir(parents=True)
    (dataset / "sub-01" / "anat").mkdir(parents=True)
    (dataset / "sub-02" / "func").mkdir(parents=True)
    (dataset / "sub-02" / "anat").mkdir(parents=True)
    (dataset / "dataset_description.json").write_text(
        '{"Name":"synthetic","BIDSVersion":"1.9.0"}', encoding="utf-8"
    )
    for subject in ("sub-01", "sub-02"):
        (dataset / subject / "func" / f"{subject}_task-rest_bold.nii.gz").write_bytes(
            f"synthetic-{subject}-bold".encode()
        )
        (dataset / subject / "anat" / f"{subject}_T1w.nii.gz").write_bytes(
            f"synthetic-{subject}-t1".encode()
        )
    return dataset
