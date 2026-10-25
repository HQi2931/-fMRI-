"""Application use cases for projects, immutable plans, and local runs."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, cast

from neuroagent.agent.loop import AgentLoopBudget
from neuroagent.agent.providers import ModelProvider
from neuroagent.agent.redaction import OutboundContextPolicy
from neuroagent.agent.secrets import SecretResolver
from neuroagent.application.automation.ports import WorkTaskRepositoryPort
from neuroagent.application.automation.service import WorkTaskApplicationService
from neuroagent.application.chat_tools import build_chat_dispatcher
from neuroagent.application.contracts.agent import RsFmriQuestionRequest
from neuroagent.application.conversation_context import ConversationContextCoordinator
from neuroagent.application.conversation_work import ConversationWorkCoordinator
from neuroagent.application.environment_lock import EnvironmentLockProvider
from neuroagent.application.errors import ApplicationError
from neuroagent.application.ports import (
    DatabaseLifecyclePort,
    DatasetInspectorPort,
    DemographicsReaderPort,
    HeadMotionQcParserPort,
    PathPolicyPort,
    RepositoryPort,
    SecretWriterPort,
    WorkerHostRepositoryPort,
    WorkspacePickerPort,
)
from neuroagent.application.repository_ports import (
    ArtifactCatalogRepositoryPort,
    ConversationRepositoryPort,
    EventRepositoryPort,
    ExternalAnalysisRepositoryPort,
    IdempotencyStorePort,
    ModelAgentRepositoryPort,
    PlanRepositoryPort,
    ProjectRepositoryPort,
    QcRepositoryPort,
    RunRepositoryPort,
    WorkspaceRepositoryPort,
)
from neuroagent.application.service_mixins import (
    ConversationMixin,
    ExternalAnalysisImportMixin,
    ModelAgentMixin,
    PlanApprovalMixin,
    ProjectDatasetMixin,
    RunMixin,
    SkillPlanMixin,
    StatisticsMixin,
)
from neuroagent.application.service_mixins.environment import EnvironmentMixin
from neuroagent.application.service_mixins.workspaces import WorkspaceMixin
from neuroagent.application.settings import Settings
from neuroagent.application.work_dialogue import WorkDialogue
from neuroagent.application.work_routing import WorkAgentRouter
from neuroagent.application.work_tools import (
    WorkToolRunner,
    build_work_dispatcher,
)
from neuroagent.chat.agent import ChatAgent
from neuroagent.chat.models import ChatAgentRequest
from neuroagent.chat.services import (
    DEFAULT_CITATION_SERVICE,
    DEFAULT_CONTEXT_MANAGER,
    DEFAULT_MEMORY_SERVICE,
    GatewayLlmClient,
    LocalEvidenceRagService,
    ModelIntentRouter,
)
from neuroagent.chat.tool_loop import ChatToolRunner
from neuroagent.context.engine import ContextEngine
from neuroagent.domain.fmri.skillpacks.builtin import build_builtin_registry
from neuroagent.infrastructure.model_work_agent_router import ModelWorkAgentRouter
from neuroagent.literature.chunker import ScientificChunker
from neuroagent.literature.pdf_parser import PypdfParser
from neuroagent.literature.ports import LiteratureRepository
from neuroagent.literature.section_parser import RuleBasedSectionParser
from neuroagent.literature.service import LiteratureService
from neuroagent.memory.embeddings import MemoryEmbeddings
from neuroagent.memory.service import SemanticMemoryService
from neuroagent.retrieval.fmrianalysis_service import FmriAnalysisRagService
from neuroagent.retrieval.uploaded_index import UploadedLiteratureIndex
from neuroagent.skills.compiler import SkillCompiler
from neuroagent.skills.registry import SkillRegistry
from neuroagent.skills.resolver import SkillResolver
from neuroagent.skills.validation import SkillValidator
from neuroagent.tools.registry import build_default_tool_registry


class ApplicationServices:
    """Composition root that wires capability services and their collaborators.

    Use cases are exposed through named capability objects. This class owns
    construction and dependency wiring; it does not forward capability methods.
    """

    repository: RepositoryPort  # Compatibility alias for legacy scripts and tests.
    worker_repository: WorkerHostRepositoryPort
    environment: EnvironmentMixin
    workspaces: WorkspaceMixin
    conversations: ConversationMixin
    external_imports: ExternalAnalysisImportMixin
    projects: ProjectDatasetMixin
    plans: PlanApprovalMixin
    skills: SkillPlanMixin
    statistics: StatisticsMixin
    runs: RunMixin
    models: ModelAgentMixin
    automation: WorkTaskApplicationService

    def __init__(
        self,
        settings: Settings,
        database: DatabaseLifecyclePort,
        worker_repository: WorkerHostRepositoryPort,
        *,
        legacy_repository: RepositoryPort,
        idempotency_store: IdempotencyStorePort,
        event_store: EventRepositoryPort,
        artifact_repository: ArtifactCatalogRepositoryPort,
        qc_repository: QcRepositoryPort,
        run_repository: RunRepositoryPort,
        project_repository: ProjectRepositoryPort,
        conversation_repository: ConversationRepositoryPort,
        workspace_repository: WorkspaceRepositoryPort,
        plan_repository: PlanRepositoryPort,
        external_import_repository: ExternalAnalysisRepositoryPort,
        model_repository: ModelAgentRepositoryPort,
        work_task_repository: WorkTaskRepositoryPort,
        path_policy: PathPolicyPort,
        dataset_inspector: DatasetInspectorPort,
        demographics_reader: DemographicsReaderPort,
        head_motion_qc_parser: HeadMotionQcParserPort,
        environment_provider: EnvironmentLockProvider,
        secret_resolver: SecretResolver,
        secret_writer: SecretWriterPort,
        providers: Mapping[str, ModelProvider],
        literature_repository: LiteratureRepository,
        workspace_picker: WorkspacePickerPort | None = None,
        workspace_file_factory: Any | None = None,
        skill_registry: SkillRegistry | None = None,
        work_agent_router: WorkAgentRouter | None = None,
    ) -> None:
        self.settings = settings

        self.database = database
        self.repository = legacy_repository
        self.worker_repository = worker_repository
        self.idempotency_store = idempotency_store
        self.event_store = event_store
        self.artifact_repository = artifact_repository
        self.qc_repository = qc_repository
        self.run_repository = run_repository
        self.project_repository = project_repository
        self.path_policy = path_policy
        self.dataset_inspector = dataset_inspector
        self.demographics_reader = demographics_reader
        self.head_motion_qc_parser = head_motion_qc_parser
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
        self.workspace_file_factory = workspace_file_factory or (lambda root: None)

        # Each capability has its own method set and narrow repository protocol.
        self.environment = EnvironmentMixin()
        self.workspaces = WorkspaceMixin()
        self.conversations = ConversationMixin()
        self.external_imports = ExternalAnalysisImportMixin()
        self.projects = ProjectDatasetMixin()
        self.plans = PlanApprovalMixin()
        self.skills = SkillPlanMixin()
        self.statistics = StatisticsMixin()
        self.runs = RunMixin()
        self.models = ModelAgentMixin()
        self.automation = WorkTaskApplicationService(
            work_task_repository,
            event_repository=event_store,
            run_repository=run_repository,
            qc_repository=qc_repository,
            artifact_repository=artifact_repository,
            conversation_repository=conversation_repository,
            plan_repository=plan_repository,
            project_repository=project_repository,
        )
        capabilities = (
            self.environment,
            self.workspaces,
            self.conversations,
            self.external_imports,
            self.projects,
            self.plans,
            self.skills,
            self.statistics,
            self.runs,
            self.models,
        )
        for capability in capabilities:
            capability.bind_shared_dependencies(
                settings=settings,
                database=database,
                idempotency_store=idempotency_store,
                event_store=event_store,
                project_repository=project_repository,
                secret_resolver=secret_resolver,
                secret_writer=secret_writer,
                providers=self.providers,
                path_policy=path_policy,
                dataset_inspector=dataset_inspector,
                demographics_reader=demographics_reader,
                head_motion_qc_parser=head_motion_qc_parser,
                environment_provider=environment_provider,
                skill_registry=self.skill_registry,
                skill_validator=self.skill_validator,
                skill_resolver=self.skill_resolver,
                tool_registry=self.tool_registry,
                skill_compiler=self.skill_compiler,
            )
        self.environment.workspace_picker = workspace_picker
        self.workspaces.workspace_file_factory = self.workspace_file_factory
        self.workspaces.repository = workspace_repository
        self.workspaces.model_gateway = self.models._model_gateway
        self.conversations.repository = conversation_repository
        self.conversations.automation = self.automation
        self.conversations.work_agent_router = work_agent_router or ModelWorkAgentRouter(
            generate=self.models._generate_rsfmri_chat,
            small_profile_id=settings.work_router_small_profile_id,
            fallback_profile_id=settings.work_router_fallback_profile_id,
            timeout_seconds=settings.work_router_timeout_seconds,
            minimum_confidence=settings.work_router_minimum_confidence,
            max_request_tokens=settings.work_router_max_request_tokens,
        )
        self.external_imports.repository = external_import_repository
        self.projects.repository = project_repository
        self.plans.repository = plan_repository
        self.skills.repository = plan_repository
        self.statistics.artifact_repository = artifact_repository
        self.statistics.automation = self.automation
        self.statistics.conversation_repository = conversation_repository
        self.statistics.external_import_repository = external_import_repository
        self.statistics.plan_repository = plan_repository
        self.statistics.qc_repository = qc_repository
        self.statistics.run_repository = run_repository
        self.runs.repository = run_repository
        self.runs.automation = self.automation
        self.runs.artifact_repository = artifact_repository
        self.runs.qc_repository = qc_repository
        self.runs.conversation_repository = conversation_repository
        self.runs.external_import_repository = external_import_repository
        self.runs.plan_repository = plan_repository
        self.models.repository = model_repository
        # Cross-capability calls are explicit collaborators. Capability objects
        # never fall back to reading arbitrary attributes from this composition root.
        self.workspaces.get_model_profile = self.models.get_model_profile
        self.workspaces.list_model_profiles = self.models.list_model_profiles
        self.workspaces.get_conversation = self.conversations.get_conversation
        self.conversations.get_plan = self.plans.get_plan
        self.conversations.get_run = self.runs.get_run
        self.conversations.get_external_import = self.external_imports.get_external_analysis_import
        self.conversations.list_model_profiles = self.models.list_model_profiles
        self.conversations.workspace_content_consent_granted = (
            self.workspaces.workspace_content_consent_granted
        )
        self.plans.get_conversation = self.conversations.get_conversation
        self.plans._validate_work_plan_handoff = self.statistics._validate_work_plan_handoff
        self.plans._validate_statistical_atlas = self.statistics._validate_statistical_atlas
        self.external_imports.get_project = self.projects.get_project
        self.skills.get_artifact = self.runs.get_artifact
        self.skills.get_dataset = self.projects.get_dataset
        self.skills.get_manifest = self.projects.get_manifest
        self.skills.get_run = self.runs.get_run
        self.runs._require_plan_current = self.statistics._require_plan_current
        self.runs._statistics_view = self.statistics._statistics_view
        self.runs._require_skill_parameter_schema = self.statistics._require_skill_parameter_schema
        self.context_engine: ContextEngine = cast(ContextEngine, DEFAULT_CONTEXT_MANAGER)
        self.semantic_memory = SemanticMemoryService(
            conversation_repository,
            MemoryEmbeddings(
                settings.memory_embedding_profile,
                secret_resolver,
                OutboundContextPolicy(settings.redaction_salt),
            )
            if settings.memory_embedding_profile is not None and settings.redaction_salt
            else None,
            limit=settings.memory_recall_limit,
            half_life_days=settings.memory_half_life_days,
            minimum_similarity=settings.memory_minimum_similarity,
        )
        self.conversation_context = ConversationContextCoordinator(
            conversation_repository,
            self.event_store,
            self.context_engine,
            self.models._generate_rsfmri_chat,
            self.semantic_memory,
            list_model_profiles=self.models.list_model_profiles,
            get_run=self.runs.get_run,
            list_artifacts=self.runs.list_artifacts,
            get_latest_qc_review_for_run=self.runs.get_run_qc_review,
            get_plan=self.plans.get_plan,
        )
        self.conversation_work = ConversationWorkCoordinator(
            conversation_repository=conversation_repository,
            project_repository=project_repository,
            plan_repository=plan_repository,
            event_store=self.event_store,
            path_policy=path_policy,
            create_project=self.projects.create_project,
            create_dataset=self.projects.create_dataset,
            inspect_dataset=self.projects.inspect_dataset,
            inspect_source=self.projects._inspect_source,
            resolve_skill_plan=self.skills.resolve_skill_plan,
            validate_plan_current=self.statistics._require_plan_current,
            get_qc_review=self.runs.get_qc_review,
            get_latest_qc_review_for_run=self.runs.get_run_qc_review,
            create_head_motion_qc_review=self.runs.create_head_motion_qc_review,
            approve_qc_review=self.runs.approve_qc_review,
            skip_qc=self.runs.skip_qc,
            list_statistical_results=self.runs.list_statistical_results,
            get_result_facts=self._statistical_result_facts,
            get_run=self.runs.get_run,
            create_run=self.runs.create_run,
            approve_plan=self.plans.approve_plan,
            tool_result=self.conversations._tool_result,
            work_root=settings.allowed_work_root,
        )
        # The bounded tool loop needs the same outbound policy as the single-shot
        # path, so it is only wired when de-identification is configured and the
        # operator has not switched the loop off.
        salt = settings.redaction_salt
        tool_policy: OutboundContextPolicy | None = None
        tool_budget: AgentLoopBudget | None = None
        if salt and settings.agent_tool_loop_enabled:
            tool_policy = OutboundContextPolicy(salt)
            tool_budget = AgentLoopBudget(
                max_steps=settings.agent_max_tool_steps,
                max_seconds=settings.agent_max_tool_seconds,
                max_total_tokens=settings.agent_max_tool_tokens,
                max_repeat_failures=settings.agent_max_repeat_tool_failures,
            )
        skill_summaries = lambda: [  # noqa: E731 - bound once, used by both catalogs
            {
                "skill_id": spec.skill_id,
                "title": spec.title,
                "status": spec.status.value,
            }
            for spec in self.skills.list_skills()
        ]
        work_tool_runner: WorkToolRunner | None = None
        if tool_policy is not None:
            # Bind non-optional: a closure does not keep the narrowing above.
            work_policy = tool_policy
            work_tool_runner = WorkToolRunner(
                build_messages=self.models._build_chat_messages,
                call_turn=self.models._tool_turn,
                dispatcher_factory=lambda context: build_work_dispatcher(
                    get_run=lambda run_id: self.runs.get_run(run_id).model_dump(mode="json"),
                    diagnose_run=lambda run_id: self.runs.diagnose_latest_run(run_id),
                    list_artifacts=lambda run_id: [
                        item.model_dump(mode="json") for item in self.runs.list_artifacts(run_id)
                    ],
                    list_results=lambda run_id: self._statistical_results_for(run_id),
                    get_result_detail=lambda result_id: self.runs.get_statistical_result(
                        result_id
                    ).model_dump(mode="json"),
                    get_result_summary=lambda result_id: self.runs.get_statistical_result(
                        result_id
                    ).model_dump(mode="json"),
                    localize_result=lambda result_id, atlas_id: (
                        self.runs.localize_statistical_result(result_id, atlas_id).model_dump(
                            mode="json"
                        )
                    ),
                    cluster_roi=lambda result_id, cluster_id: self.runs.statistical_cluster_roi(
                        result_id, cluster_id
                    ).model_dump(mode="json"),
                    list_external_imports=lambda project_id: [
                        item.model_dump(mode="json")
                        for item in self.external_imports.list_external_analysis_imports(
                            project_id=project_id
                        )
                    ],
                    correction_capabilities=lambda: [
                        item.model_dump(mode="json")
                        for item in self.statistics.correction_capabilities()
                    ],
                    list_skills=skill_summaries,
                    policy=work_policy,
                    project_id=context.project_id,
                    allowed_run_ids=context.allowed_run_ids,
                    workspace_policy=(
                        self.workspaces._workspace_policy(context.workspace_path)
                        if context.workspace_content_consent
                        and self.settings.workspace_file_assistant_enabled
                        else None
                    ),
                    workspace_scope=context.workspace_file_scope,
                    workspace_stage_candidates=context.workspace_stage_candidates,
                    workspace_stage_check_available=context.workspace_path is not None,
                    work_agent_role=context.work_agent_role,
                ),
                probe_capability=self.models._tool_capability,
                budget=tool_budget,
            )
        self.work_dialogue = WorkDialogue(
            self.conversation_work,
            lambda **kwargs: self.models._generate_rsfmri_chat(**kwargs),
            tool_runner=work_tool_runner,
            create_workspace_proposal=self.workspaces.create_workspace_proposal,
            confirm_workspace_stage=self.conversation_work.confirm_workspace_stage,
            dry_run_statistical_proposal=self.statistics.dry_run_work_statistical_design_proposal,
            get_external_import=self.external_imports.get_external_analysis_import,
            get_external_demographics=self.external_imports.get_external_demographics,
            prepare_analysis_handoff=self.statistics.prepare_work_analysis_handoff,
            prepare_preprocessing_handoff=self.statistics.prepare_work_preprocessing_handoff,
            dry_run_skill_plan=self.skills.dry_run_skill_plan,
            get_result_facts=self._analysis_result_facts,
            resolve_frozen_analysis=self.conversations.resolve_selected_frozen_analysis,
            response_repair_profile_id=settings.work_router_fallback_profile_id,
            model_budget=AgentLoopBudget(
                max_steps=settings.agent_max_tool_steps,
                max_seconds=settings.agent_max_tool_seconds,
                max_total_tokens=settings.agent_max_tool_tokens,
                max_repeat_failures=settings.agent_max_repeat_tool_failures,
            ),
        )
        uploaded_index = UploadedLiteratureIndex(
            work_root=settings.allowed_work_root,
            repository=literature_repository,
            secret_resolver=secret_resolver,
            api_key_env=settings.rag_api_key_env,
            redaction_salt=settings.redaction_salt,
        )
        self.literature = LiteratureService(
            indexer=uploaded_index,
            repository=literature_repository,
            work_root=settings.allowed_work_root,
            pdf_parser=PypdfParser(),
            section_parser=RuleBasedSectionParser(),
            chunker=ScientificChunker(
                target_tokens=settings.literature_chunk_target_tokens,
                overlap_tokens=settings.literature_chunk_overlap_tokens,
            ),
            max_pdf_bytes=settings.literature_max_pdf_bytes,
        )
        rag_service = FmriAnalysisRagService(
            db_dir=settings.rag_db_dir,
            collection=settings.rag_collection,
            secret_resolver=secret_resolver,
            api_key_env=settings.rag_api_key_env,
            redaction_salt=settings.redaction_salt,
            rerank=settings.rag_rerank,
            uploaded_index=uploaded_index,
            fallback_rag=LocalEvidenceRagService(
                lambda question: (
                    self.runs.answer_rsfmri_question(
                        RsFmriQuestionRequest(question=question, allow_remote_search=False)
                    ).answer
                )
            ),
        )
        tool_runner: ChatToolRunner | None = None
        if tool_policy is not None:
            tool_runner = ChatToolRunner(
                build_messages=self.models._build_chat_messages,
                call_turn=self.models._tool_turn,
                dispatcher_factory=lambda collector: build_chat_dispatcher(
                    retrieve=lambda query, limit: rag_service.retrieve(query, limit=limit),
                    collector=collector,
                    policy=tool_policy,
                    list_skills=skill_summaries,
                ),
                probe_capability=self.models._tool_capability,
                budget=tool_budget,
            )
        self.chat_agent = ChatAgent(
            intent_router=ModelIntentRouter(
                self._classify_chat_intent,
                has_profiles=lambda: bool(self.models.list_model_profiles()),
            ),
            rag_service=rag_service,
            context_manager=self.context_engine,
            memory_service=DEFAULT_MEMORY_SERVICE,
            llm_client=GatewayLlmClient(
                generate=self.models._generate_rsfmri_chat,
                has_profiles=lambda: bool(self.models.list_model_profiles()),
            ),
            citation_service=DEFAULT_CITATION_SERVICE,
            tool_runner=tool_runner,
        )
        self.conversations.conversation_context = self.conversation_context
        self.conversations.conversation_work = self.conversation_work
        self.conversations.work_dialogue = self.work_dialogue
        self.conversations.chat_agent = self.chat_agent

    def resolve_capability(self, name: str) -> object:
        """Return a registered capability for API and worker composition."""

        allowed = {
            "environment",
            "workspaces",
            "conversations",
            "external_imports",
            "projects",
            "plans",
            "skills",
            "statistics",
            "runs",
            "models",
            "literature",
            "semantic_memory",
        }
        if name not in allowed:
            raise KeyError(f"unknown application capability: {name}")
        try:
            return getattr(self, name)
        except AttributeError:
            raise KeyError(f"application capability is unavailable: {name}") from None

    def close(self) -> None:
        self.database.dispose()

    async def _classify_chat_intent(self, request: ChatAgentRequest) -> str:
        routing_context = self.context_engine.build(
            question=request.message,
            recent_messages=request.recent_messages,
            retrieval_context=(),
            pinned_context=request.pinned_context,
            conversation_summary=request.conversation_summary,
            context_window_tokens=request.context_window_tokens,
            max_output_tokens=request.max_output_tokens,
            context_kind="conversation",
            work_context=request.work_context,
        )
        result = await self.models._generate_rsfmri_chat(
            question=request.message,
            evidence=[],
            recent_messages=[
                {"role": item.role, "content": item.content}
                for item in routing_context.recent_messages
            ],
            pinned_context=[item.model_dump() for item in routing_context.pinned_context],
            conversation_summary=routing_context.conversation_summary,
            work_context=routing_context.work_context,
            preferred_profile_id=request.preferred_profile_id,
            model=request.model,
            allow_web_search=False,
            routing=True,
        )
        return result.response.content

    def _statistical_results_for(self, run_id: str) -> list[dict[str, Any]]:
        """Project a run's registered statistical results for the Work catalog."""

        run = self.runs.get_run(run_id)
        return [
            item.model_dump(mode="json")
            for item in self.runs.list_statistical_results(project_id=run.project_id, run_id=run_id)
        ]

    def _analysis_result_facts(
        self, run_id: str, include_spatial_details: bool = False
    ) -> list[dict[str, Any]]:
        run = self.runs.get_run(run_id)
        facts = [
            self._statistical_result_facts(result.result_id)
            for result in self.runs.list_statistical_results(
                project_id=run.project_id, run_id=run_id
            )[:5]
        ]
        if include_spatial_details:
            for fact in facts:
                self._add_statistical_spatial_facts(fact)
        return facts

    def _add_statistical_spatial_facts(self, facts: dict[str, Any]) -> None:
        from neuroagent.application.statistical_result_facts import (
            aggregate_roi,
            localization_model_facts,
        )

        if facts.get("status") != "succeeded" or facts.get("non_scientific"):
            return
        result_id = str(facts["result_id"])
        if facts.get("localization_status") in {
            "no_significant_voxels",
            "no_reportable_clusters",
            "pending_atlas_verification",
        }:
            atlas = facts.get("frozen_atlas") or {}
            try:
                localized = self.runs.localize_statistical_result(
                    result_id, atlas.get("atlas_id", "")
                )
                facts["localization"] = localization_model_facts(localized.model_dump(mode="json"))
                facts["localization_status"] = localized.status
            except ApplicationError as exc:
                facts["localization"] = {"status": "blocked", "error_code": exc.code}
        else:
            facts["localization"] = {"status": facts.get("localization_status")}
        # The initial context describes one registered cluster; other clusters
        # remain available through explicit, audited READ queries.
        clusters = facts.get("clusters", [])
        cluster_id = str(clusters[0]["cluster_id"]) if clusters else None
        try:
            facts["roi_summary"] = aggregate_roi(
                self.runs.statistical_cluster_roi(result_id, cluster_id)
            )
            facts["roi_summary_cluster_selection"] = (
                "first_reported_cluster" if clusters else "empty_result"
            )
        except ApplicationError as exc:
            facts["roi_summary"] = {"status": "blocked", "error_code": exc.code}

    def _statistical_result_facts(self, result_id: str) -> dict[str, Any]:
        from neuroagent.application.statistical_result_facts import result_model_facts

        try:
            return result_model_facts(self.runs.get_statistical_result(result_id))
        except (ValueError, TypeError, KeyError):
            return {"result_id": result_id, "status": "method_summary_unavailable"}
