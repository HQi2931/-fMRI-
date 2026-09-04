from __future__ import annotations

import pytest

from neuroagent.application.contracts import ExecutionBackend, RunCreate
from neuroagent.application.errors import ConflictError

from .conftest import make_approved_plan, make_project


def test_run_requests_default_to_mock_and_matlab_requires_confirmation() -> None:
    request = RunCreate(
        project_id="project",
        plan_revision_id="plan",
        expected_plan_hash="a" * 64,
    )
    assert request.execution_backend is ExecutionBackend.MOCK
    assert request.real_execution_confirmed is False

    with pytest.raises(ValueError, match="explicit confirmation"):
        RunCreate(
            project_id="project",
            plan_revision_id="plan",
            expected_plan_hash="a" * 64,
            execution_backend=ExecutionBackend.MATLAB,
        )


def test_real_run_is_blocked_by_default_even_after_confirmation(
    service, source_root, work_root
) -> None:
    project = make_project(service, source_root, work_root)
    plan = make_approved_plan(service, project.project_id)
    with pytest.raises(ConflictError, match="未启用"):
        service.create_run(
            RunCreate(
                project_id=project.project_id,
                plan_revision_id=plan.plan_revision_id,
                expected_plan_hash=plan.plan_hash,
                execution_backend=ExecutionBackend.MATLAB,
                real_execution_confirmed=True,
            ),
            "matlab-disabled",
        )
