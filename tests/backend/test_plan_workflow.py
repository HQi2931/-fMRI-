from __future__ import annotations

import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from neuroagent.application.contracts import (
    ApprovalCreate,
    ApprovalDecision,
    PlanRevisionCreate,
    PlanState,
    PlanValidationRequest,
    RunAction,
    RunCreate,
    ValidationIssue,
    WorkflowState,
)
from neuroagent.application.errors import ConflictError
from neuroagent.application.ports import ExecutionResult
from neuroagent.application.services import NeuroAgentService
from neuroagent.bootstrap import build_service, build_worker
from neuroagent.workflow.state import validate_workflow_transition
from neuroagent.workflow.worker import SQLiteWorker

from .conftest import make_approved_plan, make_project


class CoordinatedExecutor:
    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()
        self.cancel_seen = threading.Event()
        self.calls = 0

    def execute(
        self,
        payload: dict[str, Any],
        *,
        is_cancelled: Callable[[], bool],
    ) -> ExecutionResult:
        del payload
        self.calls += 1
        self.started.set()
        while not self.release.wait(0.02):
            if is_cancelled():
                self.cancel_seen.set()
                return ExecutionResult(status="cancelled", error="lease or user cancellation")
        return ExecutionResult(
            status="succeeded",
            output={"executor": "coordinated"},
            artifacts=(
                {
                    "artifact_type": "mock.result",
                    "relative_path": "output/coordinated.json",
                    "checksum": "c" * 64,
                    "size_bytes": 1,
                    "provenance": {"executor": "coordinated"},
                },
            ),
        )


class ImmediateExecutor:
    def __init__(self) -> None:
        self.calls = 0

    def execute(
        self,
        payload: dict[str, Any],
        *,
        is_cancelled: Callable[[], bool],
    ) -> ExecutionResult:
        del payload, is_cancelled
        self.calls += 1
        return ExecutionResult(status="failed_terminal", error="must not execute")


class InvalidArtifactExecutor:
    def execute(
        self,
        payload: dict[str, Any],
        *,
        is_cancelled: Callable[[], bool],
    ) -> ExecutionResult:
        del payload, is_cancelled
        return ExecutionResult(
            status="succeeded",
            output={"executor": "invalid-artifact"},
            artifacts=(
                {
                    "artifact_type": "mock.result",
                    "relative_path": "../escape.json",
                    "checksum": "d" * 64,
                    "size_bytes": 1,
                    "provenance": {"executor": "invalid-artifact"},
                },
            ),
        )


def test_plan_approval_gate_and_hash_binding(
    service: NeuroAgentService, source_root: Path, work_root: Path
) -> None:
    project = make_project(service, source_root, work_root)
    plan = service.create_plan(
        PlanRevisionCreate(
            project_id=project.project_id,
            expected_project_version=project.version,
            plan={"skill_id": "test", "parameters": {"tr": {"value": 2, "source": "user"}}},
            manifest_hash="a" * 64,
            environment_hash="b" * 64,
        ),
        "create-plan",
    )
    with pytest.raises(ConflictError, match="已批准"):
        service.create_run(
            RunCreate(
                project_id=project.project_id,
                plan_revision_id=plan.plan_revision_id,
                expected_plan_hash=plan.plan_hash,
            ),
            "premature-run",
        )

    blocked = service.validate_plan(
        plan.plan_revision_id,
        PlanValidationRequest(
            expected_version=plan.version,
            issues=[
                ValidationIssue(code="missing_tr", message="TR is required", severity="blocking")
            ],
        ),
        "blocked-validation",
    )
    assert blocked.state is PlanState.DRAFT
    ready = service.validate_plan(
        plan.plan_revision_id,
        PlanValidationRequest(expected_version=blocked.version, issues=[]),
        "valid-validation",
    )
    assert ready.state is PlanState.AWAITING_APPROVAL
    with pytest.raises(ConflictError, match="哈希"):
        service.approve_plan(
            plan.plan_revision_id,
            ApprovalCreate(
                expected_version=ready.version,
                plan_hash="0" * 64,
                actor="researcher",
                decision=ApprovalDecision.APPROVED,
                reason="wrong object",
            ),
            "wrong-hash",
        )
    approval = service.approve_plan(
        plan.plan_revision_id,
        ApprovalCreate(
            expected_version=ready.version,
            plan_hash=ready.plan_hash,
            actor="researcher",
            decision=ApprovalDecision.APPROVED,
            reason="reviewed",
        ),
        "right-hash",
    )
    assert approval.plan_hash == ready.plan_hash
    assert service.get_plan(plan.plan_revision_id).state is PlanState.APPROVED


def test_illegal_workflow_transition_is_rejected() -> None:
    with pytest.raises(ConflictError):
        validate_workflow_transition(WorkflowState.QUEUED, WorkflowState.SUCCEEDED)


def test_nested_free_command_is_rejected() -> None:
    with pytest.raises(ValueError, match="free executable text"):
        PlanRevisionCreate(
            project_id="project",
            expected_project_version=1,
            plan={"steps": [{"parameters": {"command": "unsafe"}}]},
            manifest_hash="a" * 64,
            environment_hash="b" * 64,
        )


def test_worker_success_requires_manual_qc(
    service: NeuroAgentService, source_root: Path, work_root: Path
) -> None:
    project = make_project(service, source_root, work_root)
    plan = make_approved_plan(service, project.project_id)
    run = service.create_run(
        RunCreate(
            project_id=project.project_id,
            plan_revision_id=plan.plan_revision_id,
            expected_plan_hash=plan.plan_hash,
        ),
        "run-key",
    )
    assert build_worker(service, worker_id="worker-a").run_once() is True
    run = service.get_run(run.run_id)
    assert run.state is WorkflowState.QC_REVIEW
    assert len(service.list_artifacts(run.run_id)) == 1
    assert not hasattr(service, "approve_qc")
    transitions = [
        event
        for event in service.list_run_events(run.run_id)
        if event.event_type == "WorkflowTransitioned"
    ]
    assert [event.payload["to_state"] for event in transitions] == ["running", "qc_review"]


def test_queued_run_can_be_cancelled_without_execution(
    service: NeuroAgentService, source_root: Path, work_root: Path
) -> None:
    project = make_project(service, source_root, work_root)
    plan = make_approved_plan(service, project.project_id)
    run = service.create_run(
        RunCreate(
            project_id=project.project_id,
            plan_revision_id=plan.plan_revision_id,
            expected_plan_hash=plan.plan_hash,
        ),
        "cancel-run",
    )
    cancelled = service.cancel_run(
        run.run_id,
        RunAction(expected_version=run.version, reason="user stopped test"),
        "cancel-key",
    )
    assert cancelled.state is WorkflowState.CANCELLED
    assert build_worker(service, worker_id="worker-a").run_once() is False


def test_retry_budget_terminates_repeated_failure(
    service: NeuroAgentService, source_root: Path, work_root: Path
) -> None:
    project = make_project(service, source_root, work_root)
    plan = make_approved_plan(service, project.project_id)
    run = service.create_run(
        RunCreate(
            project_id=project.project_id,
            plan_revision_id=plan.plan_revision_id,
            expected_plan_hash=plan.plan_hash,
            max_attempts=2,
            mock_outcome="fail_retryable",
        ),
        "retry-run",
    )
    worker = build_worker(service, worker_id="worker-a")
    assert worker.run_once() is True
    assert service.get_run(run.run_id).state is WorkflowState.FAILED_RETRYABLE
    assert worker.run_once() is True
    final = service.get_run(run.run_id)
    assert final.state is WorkflowState.FAILED_TERMINAL
    assert final.attempt == 2


def test_manual_retry_uses_remaining_approved_budget(
    service: NeuroAgentService, source_root: Path, work_root: Path
) -> None:
    project = make_project(service, source_root, work_root)
    plan = make_approved_plan(service, project.project_id)
    run = service.create_run(
        RunCreate(
            project_id=project.project_id,
            plan_revision_id=plan.plan_revision_id,
            expected_plan_hash=plan.plan_hash,
            max_attempts=2,
            mock_outcome="fail_retryable",
        ),
        "manual-retry-run",
    )
    worker = build_worker(service, worker_id="worker-a")
    assert worker.run_once() is True
    failed = service.get_run(run.run_id)
    retried = service.retry_run(
        run.run_id,
        RunAction(expected_version=failed.version, reason="retry after inspection"),
        "manual-retry-key",
    )
    assert retried.state is WorkflowState.QUEUED
    assert worker.run_once() is True
    assert service.get_run(run.run_id).state is WorkflowState.FAILED_TERMINAL


def test_expired_job_lease_is_reclaimed_after_worker_restart(
    service: NeuroAgentService, source_root: Path, work_root: Path
) -> None:
    project = make_project(service, source_root, work_root)
    plan = make_approved_plan(service, project.project_id)
    run = service.create_run(
        RunCreate(
            project_id=project.project_id,
            plan_revision_id=plan.plan_revision_id,
            expected_plan_hash=plan.plan_hash,
            max_attempts=2,
        ),
        "restart-run",
    )
    claimed = service.repository.claim_next_job("crashed-worker", lease_seconds=60)
    assert claimed is not None
    service.repository.expire_job_lease_for_test(run.run_id)
    assert build_worker(service, worker_id="replacement-worker").run_once() is True
    assert service.get_run(run.run_id).state is WorkflowState.QC_REVIEW


def test_expired_lease_reclaim_advances_run_version(
    service: NeuroAgentService, source_root: Path, work_root: Path
) -> None:
    project = make_project(service, source_root, work_root)
    plan = make_approved_plan(service, project.project_id)
    run = service.create_run(
        RunCreate(
            project_id=project.project_id,
            plan_revision_id=plan.plan_revision_id,
            expected_plan_hash=plan.plan_hash,
            max_attempts=2,
        ),
        "versioned-reclaim-run",
    )
    assert service.repository.claim_next_job("crashed-worker", lease_seconds=60) is not None
    before_reclaim = service.get_run(run.run_id)
    service.repository.expire_job_lease_for_test(run.run_id)

    reclaimed = service.repository.claim_next_job("replacement-worker", lease_seconds=60)

    assert reclaimed is not None
    after_reclaim = service.get_run(run.run_id)
    assert after_reclaim.version == before_reclaim.version + 1
    assert after_reclaim.attempt == before_reclaim.attempt + 1


def test_expired_job_lease_exhaustion_terminalizes_run_without_reexecution(
    service: NeuroAgentService, source_root: Path, work_root: Path
) -> None:
    project = make_project(service, source_root, work_root)
    plan = make_approved_plan(service, project.project_id)
    run = service.create_run(
        RunCreate(
            project_id=project.project_id,
            plan_revision_id=plan.plan_revision_id,
            expected_plan_hash=plan.plan_hash,
            max_attempts=1,
        ),
        "exhausted-restart-run",
    )
    claimed = service.repository.claim_next_job("crashed-worker", lease_seconds=60)
    assert claimed is not None
    service.repository.expire_job_lease_for_test(run.run_id)
    replacement = ImmediateExecutor()
    assert (
        SQLiteWorker(
            service.repository,
            replacement,
            worker_id="replacement-worker",
        ).run_once()
        is False
    )
    terminal = service.get_run(run.run_id)
    assert terminal.state is WorkflowState.FAILED_TERMINAL
    assert terminal.attempt == 1
    assert replacement.calls == 0
    assert any(
        event.event_type == "JobRetryBudgetExhausted"
        for event in service.list_run_events(run.run_id)
    )


def test_database_restart_preserves_queued_run(
    service: NeuroAgentService, source_root: Path, work_root: Path
) -> None:
    project = make_project(service, source_root, work_root)
    plan = make_approved_plan(service, project.project_id)
    run = service.create_run(
        RunCreate(
            project_id=project.project_id,
            plan_revision_id=plan.plan_revision_id,
            expected_plan_hash=plan.plan_hash,
        ),
        "persistent-run",
    )
    settings = service.settings
    service.close()
    restarted = build_service(settings)
    try:
        assert restarted.get_run(run.run_id).state is WorkflowState.QUEUED
        assert build_worker(restarted, worker_id="restarted-worker").run_once() is True
        assert restarted.get_run(run.run_id).state is WorkflowState.QC_REVIEW
    finally:
        restarted.close()


def test_job_lease_owner_is_required_for_renew_finish_and_requeue(
    service: NeuroAgentService, source_root: Path, work_root: Path
) -> None:
    project = make_project(service, source_root, work_root)
    plan = make_approved_plan(service, project.project_id)
    run = service.create_run(
        RunCreate(
            project_id=project.project_id,
            plan_revision_id=plan.plan_revision_id,
            expected_plan_hash=plan.plan_hash,
            max_attempts=2,
        ),
        "owner-bound-run",
    )
    claimed = service.repository.claim_next_job("worker-owner", lease_seconds=30)
    assert claimed is not None
    job_id = claimed["job_id"]
    assert service.repository.renew_job_lease(job_id, "wrong-worker", 30) is False
    assert (
        service.repository.finalize_job_failure(
            job_id,
            "wrong-worker",
            requested_state=WorkflowState.FAILED_RETRYABLE,
            error="wrong",
            actor="wrong-worker",
        )
        is False
    )
    assert service.repository.renew_job_lease(job_id, "worker-owner", 30) is True
    assert (
        service.repository.finalize_job_failure(
            job_id,
            "worker-owner",
            requested_state=WorkflowState.FAILED_RETRYABLE,
            error="recover",
            actor="worker-owner",
        )
        is True
    )
    assert service.get_run(run.run_id).state is WorkflowState.FAILED_RETRYABLE


def test_invalid_artifact_rolls_back_success_and_terminalizes_job(
    service: NeuroAgentService, source_root: Path, work_root: Path
) -> None:
    project = make_project(service, source_root, work_root)
    plan = make_approved_plan(service, project.project_id)
    run = service.create_run(
        RunCreate(
            project_id=project.project_id,
            plan_revision_id=plan.plan_revision_id,
            expected_plan_hash=plan.plan_hash,
        ),
        "invalid-artifact-run",
    )
    worker = SQLiteWorker(
        service.repository,
        InvalidArtifactExecutor(),
        worker_id="invalid-artifact-worker",
    )
    assert worker.run_once() is True
    terminal = service.get_run(run.run_id)
    assert terminal.state is WorkflowState.FAILED_TERMINAL
    assert terminal.error == "artifact finalization failed: InputValidationError"
    assert service.list_artifacts(run.run_id) == []
    assert worker.run_once() is False


def test_heartbeat_prevents_second_worker_from_reclaiming_long_job(
    service: NeuroAgentService, source_root: Path, work_root: Path
) -> None:
    project = make_project(service, source_root, work_root)
    plan = make_approved_plan(service, project.project_id)
    run = service.create_run(
        RunCreate(
            project_id=project.project_id,
            plan_revision_id=plan.plan_revision_id,
            expected_plan_hash=plan.plan_hash,
        ),
        "heartbeat-run",
    )
    first_executor = CoordinatedExecutor()
    second_executor = ImmediateExecutor()
    first_worker = SQLiteWorker(
        service.repository, first_executor, worker_id="worker-one", lease_seconds=1
    )
    second_worker = SQLiteWorker(
        service.repository, second_executor, worker_id="worker-two", lease_seconds=1
    )
    handled: list[bool] = []
    thread = threading.Thread(target=lambda: handled.append(first_worker.run_once()))
    thread.start()
    assert first_executor.started.wait(timeout=2)
    time.sleep(1.2)
    assert second_worker.run_once() is False
    assert second_executor.calls == 0
    first_executor.release.set()
    thread.join(timeout=3)
    assert not thread.is_alive()
    assert handled == [True]
    assert first_executor.calls == 1
    assert service.get_run(run.run_id).state is WorkflowState.QC_REVIEW
    assert len(service.list_artifacts(run.run_id)) == 1


def test_heartbeat_loss_cancels_executor_and_registers_no_artifacts(
    service: NeuroAgentService,
    source_root: Path,
    work_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = make_project(service, source_root, work_root)
    plan = make_approved_plan(service, project.project_id)
    run = service.create_run(
        RunCreate(
            project_id=project.project_id,
            plan_revision_id=plan.plan_revision_id,
            expected_plan_hash=plan.plan_hash,
        ),
        "lease-loss-run",
    )
    executor = CoordinatedExecutor()
    monkeypatch.setattr(service.repository, "renew_job_lease", lambda *_args: False)
    worker = SQLiteWorker(service.repository, executor, worker_id="lease-loser", lease_seconds=1)
    assert worker.run_once() is True
    assert executor.cancel_seen.is_set()
    assert service.list_artifacts(run.run_id) == []
    assert service.get_run(run.run_id).state is WorkflowState.RUNNING
    assert any(event.event_type == "JobLeaseLost" for event in service.list_run_events(run.run_id))
