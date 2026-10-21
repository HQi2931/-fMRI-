"""Application composition root for local API and worker processes."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from neuroagent.agent.providers import ModelProvider, OpenAICompatibleProvider
from neuroagent.application.environment_lock import EnvironmentLockProvider
from neuroagent.application.ports import SecretWriterPort, WorkerHostRepositoryPort
from neuroagent.application.services import ApplicationServices
from neuroagent.application.settings import Settings
from neuroagent.application.work_routing import WorkAgentRouter
from neuroagent.execution.ports import ExecutionResult, JobExecutor
from neuroagent.infrastructure.environment import SettingsEnvironmentLockProvider
from neuroagent.infrastructure.filesystem.dataset_inspector import DatasetInspector
from neuroagent.infrastructure.filesystem.demographics import read_demographics
from neuroagent.infrastructure.filesystem.directory_picker import SystemDirectoryPicker
from neuroagent.infrastructure.filesystem.head_motion_qc import parse_dpabi_head_motion_qc
from neuroagent.infrastructure.filesystem.path_policy import PathPolicy
from neuroagent.infrastructure.filesystem.workspace_files import WorkspaceFilePolicy
from neuroagent.infrastructure.matlab_executor import MatlabJobExecutor
from neuroagent.infrastructure.mock_executor import MockJobExecutor
from neuroagent.infrastructure.persistence.artifact_catalog_repository import (
    SqliteArtifactCatalogRepository,
)
from neuroagent.infrastructure.persistence.automation_repository import (
    SqliteWorkTaskRepository,
)
from neuroagent.infrastructure.persistence.conversation_repository import (
    SqliteConversationRepository,
)
from neuroagent.infrastructure.persistence.database import Database
from neuroagent.infrastructure.persistence.event_store import SqliteEventStore
from neuroagent.infrastructure.persistence.external_import_repository import (
    SqliteExternalImportRepository,
)
from neuroagent.infrastructure.persistence.idempotency_store import SqliteIdempotencyStore
from neuroagent.infrastructure.persistence.literature_repository import SqliteLiteratureRepository
from neuroagent.infrastructure.persistence.model_repository import SqliteModelRepository
from neuroagent.infrastructure.persistence.plan_repository import SqlitePlanRepository
from neuroagent.infrastructure.persistence.project_repository import SqliteProjectRepository
from neuroagent.infrastructure.persistence.qc_repository import SqliteQcRepository
from neuroagent.infrastructure.persistence.repository import SqliteRepository
from neuroagent.infrastructure.persistence.run_repository import SqliteWorkflowRunRepository
from neuroagent.infrastructure.persistence.worker_repository import SqliteWorkerRepository
from neuroagent.infrastructure.persistence.workspace_repository import SqliteWorkspaceRepository
from neuroagent.infrastructure.secrets import LocalDotenvSecretResolver, LocalDotenvSecretWriter
from neuroagent.skills.registry import SkillRegistry
from neuroagent.workflow.models import JobEnvelope, JobKind
from neuroagent.workflow.runtime import WorkflowFactory
from neuroagent.workflow.worker import SQLiteWorker


def build_service(
    settings: Settings | None = None,
    *,
    providers: Mapping[str, ModelProvider] | None = None,
    skill_registry: SkillRegistry | None = None,
    environment_provider: EnvironmentLockProvider | None = None,
    database: Database | None = None,
    repository: SqliteRepository | None = None,
    secret_writer: SecretWriterPort | None = None,
    work_agent_router: WorkAgentRouter | None = None,
) -> ApplicationServices:
    """Assemble the local application service and all infrastructure adapters."""

    if (database is None) != (repository is None):
        raise ValueError("database and repository must be supplied together")
    resolved_settings = settings or Settings.from_env()
    resolved_database = database or Database(resolved_settings.database_url)
    try:
        resolved_database.acquire_runtime_lease()
        resolved_database.initialize()
    except BaseException:
        resolved_database.dispose()
        raise
    resolved_repository = repository or SqliteRepository(resolved_database)
    resolved_worker_repository: WorkerHostRepositoryPort = repository or SqliteWorkerRepository(
        resolved_database,
        unit_of_work=resolved_repository.unit_of_work,
    )
    path_policy = PathPolicy(
        resolved_settings.allowed_source_roots,
        resolved_settings.allowed_work_root,
    )
    dataset_inspector = DatasetInspector(
        path_policy,
        max_files=resolved_settings.dataset_scan_max_files,
    )
    resolved_environment_provider = environment_provider or SettingsEnvironmentLockProvider(
        resolved_settings
    )
    if providers is None:
        compatible_provider = OpenAICompatibleProvider()
        providers = {
            "openai-compatible": compatible_provider,
            "deepseek": compatible_provider,
        }
    return ApplicationServices(
        resolved_settings,
        resolved_database,
        resolved_worker_repository,
        legacy_repository=resolved_repository,
        idempotency_store=SqliteIdempotencyStore(
            resolved_database,
            unit_of_work=resolved_repository.unit_of_work,
        ),
        event_store=SqliteEventStore(
            resolved_database,
            unit_of_work=resolved_repository.unit_of_work,
        ),
        artifact_repository=SqliteArtifactCatalogRepository(
            resolved_database,
            unit_of_work=resolved_repository.unit_of_work,
        ),
        qc_repository=SqliteQcRepository(
            resolved_database,
            unit_of_work=resolved_repository.unit_of_work,
        ),
        run_repository=SqliteWorkflowRunRepository(
            resolved_database,
            unit_of_work=resolved_repository.unit_of_work,
        ),
        project_repository=SqliteProjectRepository(
            resolved_database,
            unit_of_work=resolved_repository.unit_of_work,
        ),
        conversation_repository=SqliteConversationRepository(
            resolved_database,
            unit_of_work=resolved_repository.unit_of_work,
        ),
        workspace_repository=SqliteWorkspaceRepository(
            resolved_database,
            unit_of_work=resolved_repository.unit_of_work,
        ),
        plan_repository=SqlitePlanRepository(
            resolved_database,
            unit_of_work=resolved_repository.unit_of_work,
        ),
        external_import_repository=SqliteExternalImportRepository(
            resolved_database,
            unit_of_work=resolved_repository.unit_of_work,
        ),
        model_repository=SqliteModelRepository(
            resolved_database,
            unit_of_work=resolved_repository.unit_of_work,
        ),
        work_task_repository=SqliteWorkTaskRepository(
            resolved_database,
            unit_of_work=resolved_repository.unit_of_work,
        ),
        path_policy=path_policy,
        dataset_inspector=dataset_inspector,
        demographics_reader=read_demographics,
        head_motion_qc_parser=parse_dpabi_head_motion_qc,
        environment_provider=resolved_environment_provider,
        secret_resolver=LocalDotenvSecretResolver(resolved_settings.secrets_file),
        secret_writer=secret_writer or LocalDotenvSecretWriter(),
        providers=providers,
        workspace_picker=SystemDirectoryPicker(),
        workspace_file_factory=lambda root: WorkspaceFilePolicy(
            root,
            max_text_bytes=resolved_settings.workspace_max_text_bytes,
            max_document_bytes=resolved_settings.workspace_max_document_bytes,
        ),
        skill_registry=skill_registry,
        literature_repository=SqliteLiteratureRepository(resolved_database),
        work_agent_router=work_agent_router,
    )


def build_worker(
    service: ApplicationServices,
    *,
    worker_id: str | None = None,
    executor: JobExecutor | None = None,
) -> SQLiteWorker:
    """Assemble a worker against the same repository as an application service."""

    resolved_executor = executor
    if resolved_executor is None:
        mock_executor = MockJobExecutor(
            WorkflowFactory(service.skill_registry, service.tool_registry),
            repository=service.worker_repository,
        )
        resolved_executor = _RoutingExecutor(
            mock=mock_executor,
            matlab=MatlabJobExecutor(
                service.worker_repository,
                service.settings,
                environment_provider=service.environment_provider,
                workflow_factory=WorkflowFactory(service.skill_registry, service.tool_registry),
            ),
        )
    return SQLiteWorker(
        service.worker_repository,
        resolved_executor,
        worker_id=worker_id,
        lease_seconds=service.settings.worker_lease_seconds,
    )


class _RoutingExecutor:
    """Validate the queue contract, then dispatch a registered job capability."""

    def __init__(self, *, mock: JobExecutor, matlab: JobExecutor) -> None:
        self._executors = {
            JobKind.WORKFLOW_MOCK: mock,
            JobKind.MATLAB_PREPROCESSING: matlab,
            JobKind.MATLAB_STATISTICS: matlab,
        }

    def execute(
        self,
        payload: dict[str, Any],
        *,
        is_cancelled: Callable[[], bool],
    ) -> ExecutionResult:
        try:
            envelope = JobEnvelope.from_persisted(
                payload,
                job_id=str(payload.get("job_id", "unknown-job")),
                run_id=str(payload.get("run_id", "unknown-run")),
                attempt=int(payload.get("attempt", 1)),
            )
        except (TypeError, ValueError):
            return ExecutionResult(
                status="failed_terminal", error="job envelope is invalid or unsupported"
            )
        executor = self._executors.get(envelope.kind)
        if executor is None:
            return ExecutionResult(status="failed_terminal", error="job kind is not registered")
        return executor.execute(envelope.executor_payload(), is_cancelled=is_cancelled)
