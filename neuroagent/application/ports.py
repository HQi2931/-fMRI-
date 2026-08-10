"""Ports consumed by the application and worker layers."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, TypedDict

from pydantic import BaseModel

from neuroagent.agent.models import GatewayResult
from neuroagent.application.contracts import (
    AgentTaskView,
    ApprovalCreate,
    ApprovalView,
    ArtifactView,
    DatasetSplitView,
    DatasetView,
    DemographicsRevisionView,
    ManifestRevisionView,
    ModelProfileInput,
    ModelProfileView,
    PlanRevisionView,
    ProjectView,
    QcReviewApprove,
    QcReviewCreate,
    QcReviewView,
    RuntimeEventView,
    RunView,
    ValidationIssue,
    WorkflowState,
)


@dataclass(frozen=True, slots=True)
class ExecutionResult:
    status: str
    output: dict[str, Any] = field(default_factory=dict)
    artifacts: tuple[dict[str, Any], ...] = ()
    error: str | None = None


class ClaimedJob(TypedDict):
    job_id: str
    run_id: str
    payload: dict[str, Any]
    attempt: int
    max_attempts: int


class JobExecutor(Protocol):
    def execute(
        self,
        payload: dict[str, Any],
        *,
        is_cancelled: Callable[[], bool],
    ) -> ExecutionResult: ...


class EventReader(Protocol):
    def list_events(self, run_id: str, after_event_id: int = 0) -> list[dict[str, Any]]: ...


class DatabaseLifecyclePort(Protocol):
    """Database liveness and lifecycle without exposing an ORM or SQL API."""

    def ping(self) -> None: ...

    def dispose(self) -> None: ...


class PathPolicyPort(Protocol):
    def validate_project_source_root(self, path: str | Path) -> Path: ...

    def validate_read_path(
        self,
        path: str | Path,
        *,
        project_roots: Iterable[str | Path],
        expect_directory: bool | None = None,
    ) -> Path: ...

    def validate_work_root(self, path: str | Path) -> Path: ...


class DatasetInspectorPort(Protocol):
    def inspect(self, source_path: Path) -> dict[str, Any]: ...


class DemographicsReaderPort(Protocol):
    def __call__(
        self,
        path: Path,
        *,
        subject_id_column: str,
        column_mapping: dict[str, str],
        encoding: str,
        manifest_subject_ids: set[str],
    ) -> dict[str, Any]: ...


class RepositoryPort(Protocol):
    """Typed persistence operations required by application use cases and the worker."""

    def atomic(self) -> AbstractContextManager[None]: ...

    def begin_idempotent_request(
        self,
        scope: str,
        key: str,
        request_hash: str,
        owner_token: str,
        lease_seconds: int,
    ) -> dict[str, Any] | None: ...

    def complete_idempotent_request(
        self,
        scope: str,
        key: str,
        request_hash: str,
        owner_token: str,
        response: BaseModel,
    ) -> None: ...

    def renew_idempotent_request(
        self,
        scope: str,
        key: str,
        request_hash: str,
        owner_token: str,
        lease_seconds: int,
    ) -> bool: ...

    def release_idempotent_request(
        self, scope: str, key: str, request_hash: str, owner_token: str
    ) -> None: ...

    def create_project(self, name: str, source_roots: list[str], work_root: str) -> ProjectView: ...

    def get_project(self, project_id: str) -> ProjectView: ...

    def list_projects(self) -> list[ProjectView]: ...

    def create_dataset(
        self,
        project_id: str,
        *,
        name: str,
        source_path: str,
        expected_project_version: int,
    ) -> DatasetView: ...

    def get_dataset(self, dataset_id: str) -> DatasetView: ...

    def create_manifest(
        self,
        dataset_id: str,
        *,
        expected_version: int,
        content: dict[str, Any],
    ) -> ManifestRevisionView: ...

    def get_manifest(self, manifest_id: str) -> ManifestRevisionView: ...

    def create_demographics(
        self,
        dataset_id: str,
        *,
        expected_version: int,
        content: dict[str, Any],
    ) -> DemographicsRevisionView: ...

    def get_demographics_content(self, demographics_id: str) -> tuple[str, dict[str, Any]]: ...

    def get_demographics(self, demographics_id: str) -> DemographicsRevisionView: ...

    def create_split(
        self,
        dataset_id: str,
        *,
        expected_version: int,
        content: dict[str, Any],
    ) -> DatasetSplitView: ...

    def get_split(self, split_id: str) -> DatasetSplitView: ...

    def create_plan(
        self,
        *,
        project_id: str,
        expected_project_version: int,
        plan: dict[str, Any],
        plan_hash: str,
        manifest_hash: str,
        environment_hash: str,
        supersedes_id: str | None,
    ) -> PlanRevisionView: ...

    def get_plan(self, plan_revision_id: str) -> PlanRevisionView: ...

    def validate_plan(
        self,
        plan_revision_id: str,
        *,
        expected_version: int,
        issues: list[ValidationIssue],
    ) -> PlanRevisionView: ...

    def record_approval(
        self, plan_revision_id: str, request: ApprovalCreate
    ) -> tuple[ApprovalView, PlanRevisionView]: ...

    def create_run(
        self,
        *,
        project_id: str,
        plan_revision_id: str,
        expected_plan_hash: str,
        max_attempts: int,
        payload: dict[str, Any],
    ) -> RunView: ...

    def get_run(self, run_id: str) -> RunView: ...

    def list_runs(
        self,
        *,
        project_id: str | None = None,
        state: WorkflowState | None = None,
    ) -> list[RunView]: ...

    def request_cancel(self, run_id: str, expected_version: int) -> RunView: ...

    def retry_run(self, run_id: str, *, expected_version: int) -> RunView: ...

    def create_qc_review(self, request: QcReviewCreate) -> QcReviewView: ...

    def get_qc_review(self, review_revision_id: str) -> QcReviewView: ...

    def approve_qc_review(
        self, review_revision_id: str, request: QcReviewApprove
    ) -> tuple[QcReviewView, RunView]: ...

    def get_approved_qc_review(self, review_revision_id: str) -> QcReviewView: ...

    def list_artifacts(self, run_id: str) -> list[ArtifactView]: ...

    def get_artifact(self, artifact_id: str) -> ArtifactView: ...

    def assert_artifacts_belong_to_run(
        self, artifact_ids: tuple[str, ...], run_id: str
    ) -> None: ...

    def append_event(
        self,
        *,
        project_id: str | None,
        run_id: str | None,
        event_type: str,
        severity: str,
        payload: dict[str, object],
    ) -> None: ...

    def list_events(
        self,
        run_id: str | None = None,
        after_event_id: int = 0,
        *,
        project_id: str | None = None,
    ) -> list[RuntimeEventView]: ...

    def create_model_profile(self, profile: ModelProfileInput) -> ModelProfileView: ...

    def get_model_profile(self, profile_id: str) -> ModelProfileView: ...

    def list_model_profiles(self) -> list[ModelProfileView]: ...

    def create_agent_task(
        self,
        *,
        project_id: str,
        expected_project_version: int,
        task_type: str,
        result: GatewayResult,
    ) -> AgentTaskView: ...

    def get_agent_task(self, task_id: str) -> AgentTaskView: ...

    def claim_next_job(self, worker_id: str, lease_seconds: int) -> ClaimedJob | None: ...

    def is_cancel_requested(self, run_id: str) -> bool: ...

    def renew_job_lease(self, job_id: str, worker_id: str, lease_seconds: int) -> bool: ...

    def finalize_job_success(
        self,
        job_id: str,
        worker_id: str,
        *,
        result: dict[str, object],
        artifacts: tuple[dict[str, Any], ...],
        actor: str,
    ) -> bool: ...

    def finalize_job_failure(
        self,
        job_id: str,
        worker_id: str,
        *,
        requested_state: WorkflowState,
        error: str,
        actor: str,
    ) -> bool: ...

    def finalize_job_cancel(
        self,
        job_id: str,
        worker_id: str,
        *,
        error: str | None,
        actor: str,
    ) -> bool: ...
