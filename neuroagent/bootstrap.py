"""Application composition root for local API and worker processes."""

from __future__ import annotations

from collections.abc import Mapping

from neuroagent.agent.providers import ModelProvider, OpenAICompatibleProvider
from neuroagent.application.environment_lock import EnvironmentLockProvider
from neuroagent.application.ports import JobExecutor
from neuroagent.application.services import NeuroAgentService
from neuroagent.application.settings import Settings
from neuroagent.infrastructure.environment import SettingsEnvironmentLockProvider
from neuroagent.infrastructure.filesystem.dataset_inspector import DatasetInspector
from neuroagent.infrastructure.filesystem.demographics import read_demographics
from neuroagent.infrastructure.filesystem.path_policy import PathPolicy
from neuroagent.infrastructure.mock_executor import MockJobExecutor
from neuroagent.infrastructure.persistence.database import Database
from neuroagent.infrastructure.persistence.repository import SqliteRepository
from neuroagent.infrastructure.secrets import LocalDotenvSecretResolver
from neuroagent.skills.registry import SkillRegistry
from neuroagent.workflow.worker import SQLiteWorker


def build_service(
    settings: Settings | None = None,
    *,
    providers: Mapping[str, ModelProvider] | None = None,
    skill_registry: SkillRegistry | None = None,
    environment_provider: EnvironmentLockProvider | None = None,
    database: Database | None = None,
    repository: SqliteRepository | None = None,
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

    return SQLiteWorker(
        service.repository,
        executor or MockJobExecutor(),
        worker_id=worker_id,
        lease_seconds=service.settings.worker_lease_seconds,
    )
