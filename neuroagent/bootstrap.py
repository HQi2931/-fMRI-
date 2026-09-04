"""Application composition root for local API and worker processes."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from neuroagent.agent.providers import ModelProvider, OpenAICompatibleProvider
from neuroagent.application.environment_lock import EnvironmentLockProvider
from neuroagent.application.ports import ExecutionResult, JobExecutor, SecretWriterPort
from neuroagent.application.services import NeuroAgentService
from neuroagent.application.settings import Settings
from neuroagent.infrastructure.environment import SettingsEnvironmentLockProvider
from neuroagent.infrastructure.filesystem.dataset_inspector import DatasetInspector
from neuroagent.infrastructure.filesystem.demographics import read_demographics
from neuroagent.infrastructure.filesystem.path_policy import PathPolicy
from neuroagent.infrastructure.matlab_executor import MatlabJobExecutor
from neuroagent.infrastructure.mock_executor import MockJobExecutor
from neuroagent.infrastructure.persistence.database import Database
from neuroagent.infrastructure.persistence.repository import SqliteRepository
from neuroagent.infrastructure.secrets import LocalDotenvSecretResolver, LocalDotenvSecretWriter
from neuroagent.skills.registry import SkillRegistry
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
) -> NeuroAgentService:
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
    return NeuroAgentService(
        resolved_settings,
        resolved_database,
        resolved_repository,
        path_policy=path_policy,
        dataset_inspector=dataset_inspector,
        demographics_reader=read_demographics,
        environment_provider=resolved_environment_provider,
        secret_resolver=LocalDotenvSecretResolver(resolved_settings.secrets_file),
        secret_writer=secret_writer or LocalDotenvSecretWriter(),
        providers=providers,
        skill_registry=skill_registry,
    )


def build_worker(
    service: NeuroAgentService,
    *,
    worker_id: str | None = None,
    executor: JobExecutor | None = None,
) -> SQLiteWorker:
    """Assemble a worker against the same repository as an application service."""

    resolved_executor = executor
    if resolved_executor is None:
        mock_executor = MockJobExecutor(
            WorkflowFactory(service.skill_registry, service.tool_registry)
        )
        resolved_executor = _RoutingExecutor(
            mock=mock_executor,
            matlab=MatlabJobExecutor(
                service.repository,
                service.settings,
                environment_provider=service.environment_provider,
            ),
        )
    return SQLiteWorker(
        service.repository,
        resolved_executor,
        worker_id=worker_id,
        lease_seconds=service.settings.worker_lease_seconds,
    )


class _RoutingExecutor:
    """Dispatch by the server-owned executor_type field in the queued payload."""

    def __init__(self, *, mock: JobExecutor, matlab: JobExecutor) -> None:
        self._executors = {
            "workflow_mock": mock,
            "matlab_preprocessing": matlab,
            "matlab_statistics": matlab,
        }

    def execute(
        self,
        payload: dict[str, Any],
        *,
        is_cancelled: Callable[[], bool],
    ) -> ExecutionResult:
        executor_type = payload.get("executor_type", "workflow_mock")
        executor = self._executors.get(executor_type)
        if executor is None:
            return ExecutionResult(
                status="failed_terminal", error="requested executor type is not registered"
            )
        return executor.execute(payload, is_cancelled=is_cancelled)
