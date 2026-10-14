"""Capability-scoped repository ports with a legacy aggregate protocol."""

from __future__ import annotations

from collections.abc import Sequence
from contextlib import AbstractContextManager
from typing import Any, Protocol

from pydantic import BaseModel

from neuroagent.agent.models import GatewayResult
from neuroagent.application.contracts.agent import (
    AgentTaskView,
    ModelProfileInput,
    ModelProfileView,
)
from neuroagent.application.contracts.conversations import (
    ConfirmationChallengeView,
    ContextSummaryView,
    ConversationContextView,
    ConversationMessageView,
    ConversationMode,
    ConversationToolCallView,
    ConversationView,
    MemoryCreate,
    MemoryKind,
    MemoryScope,
    MemoryStatus,
    MemoryUpdate,
    MemoryView,
    WorkAgentSelection,
    WorkspaceChangeProposal,
    WorkspaceContentConsent,
)
from neuroagent.application.contracts.external_imports import (
    ExternalAnalysisImportSummaryView,
    ExternalAnalysisImportView,
)
from neuroagent.application.contracts.plans import (
    ApprovalCreate,
    ApprovalView,
    PlanRevisionView,
    ValidationIssue,
)
from neuroagent.application.contracts.projects import (
    DatasetSplitView,
    DatasetView,
    DemographicsRevisionView,
    ExternalDemographicsRevisionView,
    ManifestRevisionView,
    ProjectView,
)
from neuroagent.application.contracts.runs import (
    ArtifactView,
    HeadMotionQcCreate,
    QcExecutionMode,
    QcReviewApprove,
    QcReviewCreate,
    QcReviewView,
    QcSkipCreate,
    QcSkipView,
    RuntimeEventView,
    RunView,
)
from neuroagent.application.contracts.statistics import (
    StatisticalResultDetailView,
    StatisticalResultView,
)
from neuroagent.domain.lifecycle import WorkflowState
from neuroagent.workflow.models import ClaimedJob


class IdempotencyStorePort(Protocol):
    """Unit of Work and idempotency operations shared by use cases."""

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


class ConversationRepositoryPort(Protocol):
    """Repository operations required by one application capability."""

    def create_conversation(
        self,
        *,
        mode: ConversationMode,
        title: str,
        welcome: str,
        workspace_path: str | None,
        preferred_profile_id: str | None,
        project_id: str | None = None,
    ) -> ConversationView: ...

    def get_conversation(self, conversation_id: str) -> ConversationView: ...

    def select_work_agent(
        self, conversation_id: str, request: WorkAgentSelection
    ) -> ConversationView: ...

    def bind_statistics_task(
        self,
        *,
        conversation_id: str,
        project_id: str | None,
        proposal_hash: str,
        plan_revision_id: str,
        run_id: str | None = None,
        work_task_id: str | None = None,
    ) -> ConversationView: ...

    def create_confirmation_challenge(
        self,
        *,
        conversation_id: str,
        project_id: str | None,
        plan_revision_id: str,
        plan_hash: str,
        qc_mode: QcExecutionMode,
        ttl_seconds: int = 600,
    ) -> ConfirmationChallengeView: ...

    def consume_confirmation_challenge(
        self,
        *,
        confirmation_id: str,
        conversation_id: str,
        project_id: str,
        plan_revision_id: str,
        plan_hash: str,
        qc_mode: QcExecutionMode,
        run_id: str,
    ) -> ConfirmationChallengeView: ...

    def list_conversations(
        self, *, mode: ConversationMode | None = None
    ) -> list[ConversationView]: ...

    def append_conversation_exchange(
        self,
        conversation_id: str,
        *,
        user_content: str,
        assistant_content: str,
        assistant_payload: dict[str, Any],
        tool: dict[str, Any] | None,
        workspace_path: str | None,
        preferred_profile_id: str | None,
        project_id: str | None,
        active_run_id: str | None,
        tool_calls: Sequence[dict[str, Any]] | None = None,
        work_task_id: str | None = None,
    ) -> tuple[
        ConversationView,
        ConversationMessageView,
        ConversationMessageView,
        ConversationToolCallView | None,
    ]: ...

    def append_assistant_continuation(
        self,
        conversation_id: str,
        *,
        continuation_key: str,
        assistant_content: str,
        assistant_payload: dict[str, Any],
        project_id: str | None,
        work_task_id: str | None,
        update_work_state: bool = True,
    ) -> tuple[ConversationView, ConversationMessageView]: ...

    def get_conversation_context(self, conversation_id: str) -> ConversationContextView: ...

    def create_memory(self, conversation_id: str, request: MemoryCreate) -> MemoryView: ...

    def store_memory_embedding(
        self,
        conversation_id: str,
        memory_id: str,
        *,
        expected_version: int,
        model_identity: str,
        vector: tuple[float, ...],
    ) -> bool: ...

    def get_memory_embeddings(
        self,
        conversation_id: str,
    ) -> dict[str, tuple[int, str, tuple[float, ...]]]: ...

    def upsert_memory_candidate(
        self,
        conversation_id: str,
        *,
        kind: MemoryKind,
        key: str,
        content: str,
        status: MemoryStatus,
        pinned: bool,
        confidence: float,
        source_message_id: str,
        scope: MemoryScope = MemoryScope.CONVERSATION,
        importance: float = 0.5,
    ) -> MemoryView: ...

    def get_forgotten_source_message_ids(self, conversation_id: str) -> set[str]: ...

    def update_memory(
        self, conversation_id: str, memory_id: str, request: MemoryUpdate
    ) -> MemoryView: ...

    def create_context_summary(
        self,
        conversation_id: str,
        *,
        content: str,
        covered_sequence: int,
        source_hash: str,
        method: str,
    ) -> ContextSummaryView: ...

    def create_context_snapshot(
        self,
        conversation_id: str,
        *,
        assistant_message_id: str | None,
        context_hash: str,
        profile_id: str | None,
        manifest: dict[str, Any],
    ) -> None: ...


class WorkspaceRepositoryPort(Protocol):
    """Repository operations required to authorize and apply workspace edits."""

    def create_workspace_consent(
        self,
        *,
        conversation_id: str,
        workspace_hash: str,
        profile_id: str,
        profile_revision: str,
        model: str,
        policy_version: str,
        provider_host: str,
    ) -> WorkspaceContentConsent: ...

    def list_workspace_consents(self, conversation_id: str) -> list[WorkspaceContentConsent]: ...

    def revoke_workspace_consent(
        self, conversation_id: str, consent_id: str
    ) -> WorkspaceContentConsent: ...

    def create_workspace_patch(
        self,
        *,
        conversation_id: str,
        workspace_hash: str,
        proposal_hash: str,
        edits: list[dict[str, Any]],
        diff: str,
        citations: list[dict[str, Any]],
        ttl_seconds: int = 600,
    ) -> WorkspaceChangeProposal: ...

    def get_workspace_patch(
        self, conversation_id: str, proposal_id: str
    ) -> WorkspaceChangeProposal: ...

    def get_workspace_patch_metadata(
        self, conversation_id: str, proposal_id: str
    ) -> dict[str, Any] | None: ...

    def set_workspace_patch_applied(
        self,
        conversation_id: str,
        proposal_id: str,
        *,
        backup_dir: str,
        postimage: dict[str, str],
    ) -> WorkspaceChangeProposal: ...

    def set_workspace_patch_undone(
        self, conversation_id: str, proposal_id: str
    ) -> WorkspaceChangeProposal: ...

    def invalidate_workspace_patch_undo(
        self, conversation_id: str, workspace_hash: str, except_proposal_id: str
    ) -> None: ...


class ProjectRepositoryPort(Protocol):
    """Repository operations required by one application capability."""

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

    def get_manifest_content(self, manifest_id: str) -> dict[str, Any]: ...

    def create_demographics(
        self,
        dataset_id: str,
        *,
        expected_version: int,
        content: dict[str, Any],
    ) -> DemographicsRevisionView: ...

    def get_demographics_content(self, demographics_id: str) -> tuple[str, dict[str, Any]]: ...

    def get_demographics(self, demographics_id: str) -> DemographicsRevisionView: ...

    def get_latest_demographics_for_dataset(
        self, dataset_id: str
    ) -> DemographicsRevisionView | None: ...

    def create_split(
        self,
        dataset_id: str,
        *,
        expected_version: int,
        content: dict[str, Any],
    ) -> DatasetSplitView: ...

    def get_split(self, split_id: str) -> DatasetSplitView: ...


class PlanRepositoryPort(Protocol):
    """Repository operations required by one application capability."""

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

    def get_approved_plan_approval(self, plan_revision_id: str) -> ApprovalView: ...


class ArtifactCatalogRepositoryPort(Protocol):
    """Artifact lineage and result catalog operations, shared by Run and Statistics use cases."""

    def register_artifacts(
        self, project_id: str, run_id: str, artifacts: tuple[dict[str, Any], ...]
    ) -> None: ...

    def list_artifacts(self, run_id: str) -> list[ArtifactView]: ...

    def get_artifact(self, artifact_id: str) -> ArtifactView: ...

    def assert_artifacts_belong_to_run(
        self, artifact_ids: tuple[str, ...], run_id: str
    ) -> None: ...

    def create_statistical_result(
        self,
        *,
        project_id: str,
        run_id: str,
        design_revision_id: str,
        mode: str,
        non_scientific: bool,
        non_scientific_reason: str | None,
        bundle_hash: str,
        manifest: dict[str, Any],
        report_markdown: str,
        report_json: str,
        actor: str,
    ) -> StatisticalResultView: ...

    def list_statistical_results(
        self, *, project_id: str, run_id: str | None = None
    ) -> list[StatisticalResultView]: ...

    def get_statistical_result(self, result_id: str) -> StatisticalResultDetailView: ...


class RunRepositoryPort(Protocol):
    """Run lifecycle writes and queries, without QC or catalog operations."""

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


class QcRepositoryPort(Protocol):
    """Immutable QC reviews and their run-state transitions."""

    def create_qc_review(self, request: QcReviewCreate) -> QcReviewView: ...

    def create_head_motion_qc_review(
        self, run_id: str, request: HeadMotionQcCreate
    ) -> QcReviewView: ...

    def get_qc_review(self, review_revision_id: str) -> QcReviewView: ...

    def get_latest_qc_review_for_run(self, run_id: str) -> QcReviewView | None: ...

    def approve_qc_review(
        self, review_revision_id: str, request: QcReviewApprove
    ) -> tuple[QcReviewView, RunView]: ...

    def get_qc_skip_for_run(self, run_id: str) -> QcSkipView | None: ...

    def skip_qc(self, run_id: str, request: QcSkipCreate) -> tuple[QcSkipView, RunView]: ...

    def get_approved_qc_review(self, review_revision_id: str) -> QcReviewView: ...


class ExternalAnalysisRepositoryPort(Protocol):
    """Repository operations required by one application capability."""

    def create_external_analysis_import(
        self,
        *,
        project_id: str,
        source_path: str,
        inspection: dict[str, Any],
    ) -> ExternalAnalysisImportView: ...

    def create_external_demographics(
        self, import_id: str, *, expected_version: int, content: dict[str, Any]
    ) -> ExternalDemographicsRevisionView: ...

    def get_external_analysis_import(self, import_id: str) -> ExternalAnalysisImportView: ...

    def get_latest_external_demographics(
        self, import_id: str
    ) -> ExternalDemographicsRevisionView | None: ...

    def get_external_demographics_content(
        self, demographics_id: str
    ) -> tuple[ExternalDemographicsRevisionView, dict[str, Any]]: ...

    def include_external_analysis_import(
        self,
        import_id: str,
        *,
        expected_version: int,
        inclusion: dict[str, Any],
        inclusion_hash: str,
    ) -> ExternalAnalysisImportView: ...

    def list_external_analysis_imports(
        self, *, project_id: str
    ) -> list[ExternalAnalysisImportSummaryView]: ...


class ModelAgentRepositoryPort(Protocol):
    """Repository operations required by one application capability."""

    def create_agent_task(
        self,
        *,
        project_id: str,
        expected_project_version: int,
        task_type: str,
        result: GatewayResult,
    ) -> AgentTaskView: ...

    def create_model_profile(self, profile: ModelProfileInput) -> ModelProfileView: ...

    def delete_model_profile(self, profile_id: str) -> None: ...

    def get_agent_task(self, task_id: str) -> AgentTaskView: ...

    def get_model_profile(self, profile_id: str) -> ModelProfileView: ...

    def list_model_profiles(self) -> list[ModelProfileView]: ...


class JobQueueRepositoryPort(Protocol):
    """Repository operations required by one application capability."""

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

    def get_run(self, run_id: str) -> RunView: ...


class EventRepositoryPort(Protocol):
    """Append-only event storage separate from capability repositories."""

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
        limit: int | None = None,
    ) -> list[RuntimeEventView]: ...


class RepositoryPort(
    IdempotencyStorePort,
    ConversationRepositoryPort,
    WorkspaceRepositoryPort,
    ArtifactCatalogRepositoryPort,
    QcRepositoryPort,
    ProjectRepositoryPort,
    PlanRepositoryPort,
    RunRepositoryPort,
    ExternalAnalysisRepositoryPort,
    ModelAgentRepositoryPort,
    JobQueueRepositoryPort,
    EventRepositoryPort,
    Protocol,
):
    """Legacy all-capability adapter contract retained for Worker and compatibility callers."""

    def get_demographics_content(self, demographics_id: str) -> tuple[str, dict[str, Any]]: ...

    def get_external_demographics_content(
        self, demographics_id: str
    ) -> tuple[ExternalDemographicsRevisionView, dict[str, Any]]: ...

    pass
