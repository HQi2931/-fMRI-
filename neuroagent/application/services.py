"""Application use cases for projects, immutable plans, and local runs."""

from __future__ import annotations

from collections.abc import Mapping
from typing import cast

from neuroagent.agent.providers import ModelProvider
from neuroagent.agent.secrets import SecretResolver
from neuroagent.application.contracts import (
    EnvironmentConfigUpdate,
    EnvironmentConfigView,
    EnvironmentProbeView,
    HealthView,
    RsFmriQuestionRequest,
    WorkspacePickView,
)
from neuroagent.application.environment_lock import EnvironmentLockProvider
from neuroagent.application.ports import (
    DatabaseLifecyclePort,
    DatasetInspectorPort,
    DemographicsReaderPort,
    PathPolicyPort,
    RepositoryPort,
    SecretWriterPort,
    WorkspacePickerPort,
)
from neuroagent.application.service_mixins import (
    ConversationMixin,
    ModelAgentMixin,
    PlanApprovalMixin,
    ProjectDatasetMixin,
    RunMixin,
    SkillPlanMixin,
    StatisticsMixin,
)
from neuroagent.application.settings import Settings
from neuroagent.chat.agent import ChatAgent
from neuroagent.chat.services import (
    DEFAULT_CITATION_SERVICE,
    DEFAULT_CONTEXT_MANAGER,
    DEFAULT_MEMORY_SERVICE,
    GatewayLlmClient,
    LocalEvidenceRagService,
    ModelIntentRouter,
)
from neuroagent.domain.fmri.skillpacks.builtin import build_builtin_registry
from neuroagent.literature.chunker import ScientificChunker
from neuroagent.literature.pdf_parser import PypdfParser
from neuroagent.literature.ports import LiteratureRepository
from neuroagent.literature.section_parser import RuleBasedSectionParser
from neuroagent.literature.service import LiteratureService
from neuroagent.retrieval.fmrianalysis_service import FmriAnalysisRagService
from neuroagent.skills.compiler import SkillCompiler
from neuroagent.skills.registry import SkillRegistry
from neuroagent.skills.resolver import SkillResolver
from neuroagent.skills.validation import SkillValidator
from neuroagent.tools.registry import build_default_tool_registry


class NeuroAgentService(
    ConversationMixin,
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
        workspace_picker: WorkspacePickerPort | None = None,
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
        self.workspace_picker = workspace_picker
        self.literature = LiteratureService(
            repository=cast(LiteratureRepository, repository),
            work_root=settings.allowed_work_root,
            pdf_parser=PypdfParser(),
            section_parser=RuleBasedSectionParser(),
            chunker=ScientificChunker(
                target_tokens=settings.literature_chunk_target_tokens,
                overlap_tokens=settings.literature_chunk_overlap_tokens,
            ),
            max_pdf_bytes=settings.literature_max_pdf_bytes,
        )
        self.chat_agent = ChatAgent(
            intent_router=ModelIntentRouter(self._classify_chat_intent),
            rag_service=FmriAnalysisRagService(
                db_dir=settings.rag_db_dir,
                collection=settings.rag_collection,
                secret_resolver=secret_resolver,
                api_key_env=settings.rag_api_key_env,
                redaction_salt=settings.redaction_salt,
                rerank=settings.rag_rerank,
            ) if settings.rag_db_dir else LocalEvidenceRagService(
                lambda question: self.answer_rsfmri_question(
                    RsFmriQuestionRequest(question=question, allow_remote_search=False)
                ).answer
            ),
            context_manager=DEFAULT_CONTEXT_MANAGER,
            memory_service=DEFAULT_MEMORY_SERVICE,
            llm_client=GatewayLlmClient(
                generate=self._generate_rsfmri_chat,
                has_profiles=lambda: bool(self.repository.list_model_profiles()),
            ),
            citation_service=DEFAULT_CITATION_SERVICE,
        )

    def close(self) -> None:
        self.database.dispose()

    async def _classify_chat_intent(self, message: str) -> str:
        prompt = (
            "将用户消息分类为 rs-fMRI Chat 意图。只返回 JSON, 不要 Markdown。\n"
            '格式: {"intent":"knowledge_query|conversation|work_request",'
            '"task":"可选任务名","parameters":{}}\n'
            "knowledge_query: 询问概念、方法、原理或文献; conversation: 普通对话; "
            "work_request: 明确要求对用户数据执行、计算、运行或处理。"
            "仅提到某个方法不代表 work_request。\n用户消息: " + message
        )
        result = await self._model_gateway().generate_chat(
            question=prompt,
            evidence=[],
            preferred_profile_id=None,
            model=None,
            allow_web_search=False,
        )
        return result.response.content

    def health(self) -> HealthView:
        self.database.ping()
        return HealthView()

    def environment_probe(self) -> EnvironmentProbeView:
        return self.environment_provider.current().probe

    def environment_config(self) -> EnvironmentConfigView:
        return self.environment_provider.configuration_view()

    def update_environment_config(self, request: EnvironmentConfigUpdate) -> EnvironmentConfigView:
        return self.environment_provider.update_configuration(request)

    def pick_workspace(self) -> WorkspacePickView:
        if self.workspace_picker is None:
            return WorkspacePickView(cancelled=True)
        return WorkspacePickView(path=self.workspace_picker.pick_directory())
