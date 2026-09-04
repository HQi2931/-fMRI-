"""Application use cases for projects, immutable plans, and local runs."""

from __future__ import annotations

from collections.abc import Mapping

from neuroagent.agent.providers import ModelProvider
from neuroagent.agent.secrets import SecretResolver
from neuroagent.application.contracts import (
    EnvironmentConfigUpdate,
    EnvironmentConfigView,
    EnvironmentProbeView,
    HealthView,
)
from neuroagent.application.environment_lock import EnvironmentLockProvider
from neuroagent.application.ports import (
    DatabaseLifecyclePort,
    DatasetInspectorPort,
    DemographicsReaderPort,
    PathPolicyPort,
    RepositoryPort,
    SecretWriterPort,
)
from neuroagent.application.service_mixins import (
    ModelAgentMixin,
    PlanApprovalMixin,
    ProjectDatasetMixin,
    RunMixin,
    SkillPlanMixin,
    StatisticsMixin,
)
from neuroagent.application.settings import Settings
from neuroagent.domain.fmri.skillpacks.builtin import build_builtin_registry
from neuroagent.skills.compiler import SkillCompiler
from neuroagent.skills.registry import SkillRegistry
from neuroagent.skills.resolver import SkillResolver
from neuroagent.skills.validation import SkillValidator
from neuroagent.tools.registry import build_default_tool_registry


class NeuroAgentService(
    ProjectDatasetMixin,
    PlanApprovalMixin,
    SkillPlanMixin,
    StatisticsMixin,
    RunMixin,
    ModelAgentMixin,
):
    """A transport-neutral facade around transactional use cases."""

    def __init__(
        self,
        settings: Settings,
        database: DatabaseLifecyclePort,
        repository: RepositoryPort,
        *,
        path_policy: PathPolicyPort,
        dataset_inspector: DatasetInspectorPort,
        demographics_reader: DemographicsReaderPort,
        environment_provider: EnvironmentLockProvider,
        secret_resolver: SecretResolver,
        secret_writer: SecretWriterPort,
        providers: Mapping[str, ModelProvider],
        skill_registry: SkillRegistry | None = None,
    ) -> None:
        self.settings = settings
        self.database = database
        self.repository = repository
        self.path_policy = path_policy
        self.dataset_inspector = dataset_inspector
        self.demographics_reader = demographics_reader
        self.skill_registry = skill_registry or build_builtin_registry()
        self.skill_validator = SkillValidator()
        self.skill_resolver = SkillResolver(self.skill_registry)
        self.tool_registry = build_default_tool_registry()
        self.skill_compiler = SkillCompiler(self.tool_registry, self.skill_validator)
        self.environment_provider = environment_provider
        self.secret_resolver = secret_resolver
        self.secret_writer = secret_writer
        self.providers = dict(providers)

    def close(self) -> None:
        self.database.dispose()

    def health(self) -> HealthView:
        self.database.ping()
        return HealthView()

    def environment_probe(self) -> EnvironmentProbeView:
        return self.environment_provider.current().probe

    def environment_config(self) -> EnvironmentConfigView:
        return self.environment_provider.configuration_view()

    def update_environment_config(self, request: EnvironmentConfigUpdate) -> EnvironmentConfigView:
        return self.environment_provider.update_configuration(request)
