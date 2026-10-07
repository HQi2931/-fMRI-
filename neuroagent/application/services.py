"""Application use cases for projects, immutable plans, and local runs."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from contextlib import suppress
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlsplit

from neuroagent.agent.gateway import ModelGatewayError
from neuroagent.agent.loop import AgentLoopBudget
from neuroagent.agent.models import ModelCapability
from neuroagent.agent.providers import ModelProvider, ProviderError
from neuroagent.agent.redaction import OutboundContextPolicy
from neuroagent.agent.secrets import SecretResolver
from neuroagent.agent.tool_spec import LlmTool, ToolTier
from neuroagent.application.chat_tools import build_chat_dispatcher
from neuroagent.application.contracts import (
    EnvironmentConfigUpdate,
    EnvironmentConfigView,
    EnvironmentProbeView,
    HealthView,
    ModelProfileView,
    RsFmriQuestionRequest,
    WorkspaceChangeProposal,
    WorkspaceContentConsent,
    WorkspaceContentConsentCreate,
    WorkspacePatchApplyRequest,
    WorkspacePatchApplyView,
    WorkspacePatchUndoView,
    WorkspacePickView,
)
from neuroagent.application.conversation_context import ConversationContextCoordinator
from neuroagent.application.conversation_work import ConversationWorkCoordinator
from neuroagent.application.environment_lock import EnvironmentLockProvider
from neuroagent.application.errors import ConflictError, InputValidationError
from neuroagent.application.hashing import canonical_json
from neuroagent.application.ports import (
    DatabaseLifecyclePort,
    DatasetInspectorPort,
    DemographicsReaderPort,
    HeadMotionQcParserPort,
    PathPolicyPort,
    RepositoryPort,
    SecretWriterPort,
    WorkspaceFilePolicyPort,
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
from neuroagent.application.work_dialogue import WorkDialogue
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

    workspace_policy_version = "workspace-files-v1"

    @staticmethod
    def _workspace_hash(root: Path) -> str:
        return hashlib.sha256(str(root.resolve(strict=True)).encode("utf-8")).hexdigest()

    @staticmethod
    def _profile_revision(profile_view: ModelProfileView) -> str:
        profile = profile_view.profile.model_dump(mode="json")
        # Capabilities are a set. Its iteration order changes across processes,
        # but does not represent a profile change or invalidate consent.
        profile["capabilities"] = sorted(profile["capabilities"])
        material = canonical_json(
            {
                "profile": profile,
                "version": profile_view.version,
                "created_at": profile_view.created_at.isoformat(),
            }
        )
        return hashlib.sha256(material.encode("utf-8")).hexdigest()

    def _consent_matches_current_context(
        self,
        consent: WorkspaceContentConsent,
        *,
        workspace_hash: str | None,
        profile_view: ModelProfileView | None,
    ) -> bool:
        return bool(
            workspace_hash
            and profile_view
            and consent.revoked_at is None
            and consent.workspace_hash == workspace_hash
            and consent.profile_id == profile_view.profile.id
            and consent.profile_revision == self._profile_revision(profile_view)
            and consent.policy_version == self.workspace_policy_version
            and ModelCapability.TOOL_USE in profile_view.profile.capabilities
        )

    @staticmethod
    def _file_hash(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
        return digest.hexdigest()

    def _workspace_root(self, path: str | None) -> Path | None:
        if not path:
            return None
        return self.path_policy.validate_project_source_root(path)

    def workspace_content_consent_granted(
        self,
        conversation_id: str,
        workspace_path: str | None,
        profile_id: str | None,
        model: str | None,
    ) -> bool:
        if not workspace_path or not profile_id or not model:
            return False
        try:
            root = self._workspace_root(workspace_path)
        except Exception:
            return False
        if root is None:
            return False
        expected_hash = self._workspace_hash(root)
        try:
            profile_view = self.repository.get_model_profile(profile_id)
        except Exception:
            return False
        try:
            consents = self.repository.list_workspace_consents(conversation_id)
        except (AttributeError, NotImplementedError):
            return False
        return any(
            item.model == model
            and self._consent_matches_current_context(
                item, workspace_hash=expected_hash, profile_view=profile_view
            )
            for item in consents
        )

    def _workspace_policy(self, path: str | None) -> WorkspaceFilePolicyPort | None:
        root = self._workspace_root(path)
        if root is None:
            return None
        return self.workspace_file_factory(root)

    async def create_workspace_content_consent(
        self, conversation_id: str, request: WorkspaceContentConsentCreate
    ) -> WorkspaceContentConsent:
        if not self.settings.workspace_file_assistant_enabled or not self.settings.redaction_salt:
            raise InputValidationError(
                "workspace_file_assistant_unavailable",
                "文件助手未启用：必须配置 redaction_salt 且开启功能。",
            )
        conversation = self.repository.get_conversation(conversation_id)
        if conversation.mode.value != "work" or not conversation.workspace_path:
            raise InputValidationError("workspace_required", "文件助手需要绑定 Work 工作区。")
        profile_view = self.get_model_profile(request.profile_id)
        if ModelCapability.TOOL_USE not in profile_view.profile.capabilities:
            raise InputValidationError(
                "workspace_tool_use_required",
                "该服务商配置尚未声明工具调用能力。请在设置中确认并启用 tool_use。",
                profile_id=request.profile_id,
                required_capability=ModelCapability.TOOL_USE.value,
            )
        if not request.acknowledge_remote_processing:
            raise InputValidationError(
                "workspace_consent_required", "必须确认文件内容可能发送给所选模型服务。"
            )
        root = self._workspace_root(conversation.workspace_path)
        if root is None:
            raise InputValidationError("workspace_required", "文件助手需要绑定 Work 工作区。")
        workspace_hash = self._workspace_hash(root)
        await self._probe_workspace_tool_support(profile_view, request.model)

        # Do not persist a consent if the model configuration or selected
        # workspace changed while the external capability probe was running.
        latest_profile = self.get_model_profile(request.profile_id)
        latest_conversation = self.repository.get_conversation(conversation_id)
        latest_root = self._workspace_root(latest_conversation.workspace_path)
        latest_workspace_hash = (
            self._workspace_hash(latest_root) if latest_root is not None else None
        )
        if (
            self._profile_revision(latest_profile) != self._profile_revision(profile_view)
            or latest_workspace_hash != workspace_hash
        ):
            raise ConflictError(
                "workspace_consent_context_changed",
                "工作区或模型配置在兼容性测试期间发生变化，请刷新后重新授权。",
            )
        host = urlsplit(profile_view.profile.base_url).hostname or profile_view.profile.base_url
        consent = self.repository.create_workspace_consent(
            conversation_id=conversation_id,
            workspace_hash=workspace_hash,
            profile_id=request.profile_id,
            profile_revision=self._profile_revision(profile_view),
            model=request.model,
            policy_version=self.workspace_policy_version,
            provider_host=host,
        )
        self.repository.append_event(
            project_id=conversation.project_id,
            run_id=None,
            event_type="WorkspaceContentConsentGranted",
            severity="info",
            payload={
                "consent_id": consent.consent_id,
                "conversation_id": conversation_id,
                "workspace_hash": workspace_hash,
                "profile_id": request.profile_id,
                "profile_revision": self._profile_revision(profile_view),
                "model": request.model,
                "provider_host": host,
                "tool_probe": "passed",
            },
        )
        return consent.model_copy(update={"valid_for_current_context": True})

    async def _probe_workspace_tool_support(
        self, profile_view: ModelProfileView, model: str
    ) -> None:
        probe_tool = LlmTool(
            name="workspace_tool_probe",
            description="Synthetic compatibility probe. This function has no side effects.",
            parameters={
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
            tier=ToolTier.READ,
            timeout_seconds=15,
            max_result_bytes=1_024,
        )
        messages: list[dict[str, object]] = [
            {
                "role": "system",
                "content": (
                    "This is a tool compatibility check. Call the offered "
                    "workspace_tool_probe function exactly once with an empty object. "
                    "Do not answer in prose."
                ),
            },
            {"role": "user", "content": "Run the synthetic tool compatibility check."},
        ]
        try:
            result = await self._model_gateway().generate_tool_turn(
                messages=messages,
                tools=[probe_tool],
                preferred_profile_id=profile_view.profile.id,
                model=model,
                remaining_seconds=min(profile_view.profile.timeout_seconds, 15.0),
            )
        except (ModelGatewayError, ProviderError) as exc:
            cause: BaseException | None = exc
            status = None
            timeout = False
            for _ in range(6):
                if cause is None:
                    break
                timeout = timeout or bool(getattr(cause, "timeout", False))
                if isinstance(cause, ProviderError):
                    import re

                    match = re.fullmatch(
                        r"provider (?:rejected request|temporarily unavailable) \(([0-9]{3})\)",
                        str(cause),
                    )
                    if match:
                        status = int(match[1])
                cause = cause.__cause__
            reason = "timeout" if timeout else "http_rejected" if status else "provider_error"
            raise InputValidationError(
                "workspace_tool_probe_failed",
                "工具测试请求超时；尚不能判定模型是否支持工具调用。"
                if timeout
                else "工具测试请求失败；请检查服务连接与接口兼容性。",
                profile_id=profile_view.profile.id,
                model=model,
                reason=reason,
                provider_status=status,
            ) from exc
        calls = result.response.tool_calls
        if (
            result.selected_profile_id != profile_view.profile.id
            or len(calls) != 1
            or calls[0].name != probe_tool.name
            or not calls[0].arguments_valid
            or calls[0].arguments
            or result.response.finish_reason == "length"
        ):
            reason = (
                "output_truncated"
                if result.response.finish_reason == "length"
                else "profile_mismatch"
                if result.selected_profile_id != profile_view.profile.id
                else "no_tool_call"
                if not calls
                else "wrong_tool"
                if len(calls) != 1 or calls[0].name != probe_tool.name
                else "invalid_arguments"
            )
            raise InputValidationError(
                "workspace_tool_probe_failed",
                "工具测试回复被截断；请检查输出上限和思考模式。"
                if reason == "output_truncated"
                else "工具测试未返回合格调用；尚不能判定模型是否支持工具调用。",
                profile_id=profile_view.profile.id,
                model=model,
                reason=reason,
                finish_reason=result.response.finish_reason
                if result.response.finish_reason in {None, "length", "stop", "tool_calls"}
                else "other",
                tool_call_count=len(calls),
                arguments_valid=bool(calls and calls[0].arguments_valid and not calls[0].arguments),
                usage={
                    key: value
                    for key, value in result.response.usage.items()
                    if key in {"prompt_tokens", "completion_tokens", "total_tokens"}
                },
            )

    def list_workspace_content_consents(
        self, conversation_id: str
    ) -> list[WorkspaceContentConsent]:
        consents = self.repository.list_workspace_consents(conversation_id)
        conversation = self.repository.get_conversation(conversation_id)
        try:
            root = self._workspace_root(conversation.workspace_path)
            workspace_hash = self._workspace_hash(root) if root is not None else None
        except Exception:
            workspace_hash = None
        profiles = {item.profile.id: item for item in self.repository.list_model_profiles()}
        return [
            item.model_copy(
                update={
                    "valid_for_current_context": self._consent_matches_current_context(
                        item,
                        workspace_hash=workspace_hash,
                        profile_view=profiles.get(item.profile_id),
                    )
                }
            )
            for item in consents
        ]

    def revoke_workspace_content_consent(
        self, conversation_id: str, consent_id: str
    ) -> WorkspaceContentConsent:
        consent = self.repository.revoke_workspace_consent(conversation_id, consent_id)
        self.repository.append_event(
            project_id=None,
            run_id=None,
            event_type="WorkspaceContentConsentRevoked",
            severity="info",
            payload={
                "consent_id": consent.consent_id,
                "conversation_id": conversation_id,
                "workspace_hash": consent.workspace_hash,
                "profile_id": consent.profile_id,
                "model": consent.model,
            },
        )
        return consent

    def list_workspace_files(self, conversation_id: str, directory: str = "") -> dict[str, Any]:
        conversation = self.repository.get_conversation(conversation_id)
        policy = self._workspace_policy(conversation.workspace_path)
        if policy is None:
            raise InputValidationError("workspace_required", "请先选择工作区。")
        return policy.list_entries(directory=directory)

    def create_workspace_proposal(
        self,
        *,
        conversation_id: str,
        workspace_path: str | None,
        profile_id: str | None,
        model: str | None,
        edits: list[dict[str, Any]],
        citations: list[dict[str, Any]],
    ) -> dict[str, Any]:
        if not self.workspace_content_consent_granted(
            conversation_id, workspace_path, profile_id, model
        ):
            raise InputValidationError(
                "workspace_consent_required", "请先授权当前模型读取工作区文件。"
            )
        policy = self._workspace_policy(workspace_path)
        if policy is None:
            raise InputValidationError("workspace_required", "请先选择工作区。")
        if not citations:
            raise InputValidationError(
                "workspace_citation_required", "文件修改提案必须包含证据引用。"
            )
        for citation in citations:
            policy._check_file(str(citation.get("path", "")))
        built = policy.build_patch(
            edits,
            max_files=self.settings.workspace_patch_max_files,
            max_bytes=self.settings.workspace_patch_max_bytes,
        )
        proposal = self.repository.create_workspace_patch(
            conversation_id=conversation_id,
            workspace_hash=self._workspace_hash(policy.root),
            proposal_hash=built["proposal_hash"],
            edits=built["edits"],
            diff=built["diff"],
            citations=citations,
        )
        return proposal.model_dump(mode="json")

    def get_workspace_proposal(
        self, conversation_id: str, proposal_id: str
    ) -> WorkspaceChangeProposal:
        return self.repository.get_workspace_patch(conversation_id, proposal_id)

    def apply_workspace_proposal(
        self,
        conversation_id: str,
        proposal_id: str,
        request: WorkspacePatchApplyRequest,
    ) -> WorkspacePatchApplyView:
        from datetime import UTC, datetime

        proposal = self.repository.get_workspace_patch(conversation_id, proposal_id)
        if proposal.proposal_hash != request.proposal_hash:
            raise ConflictError("workspace_patch_hash_mismatch", "补丁哈希与当前提案不一致。")
        if proposal.status != "proposed" or proposal.expires_at <= datetime.now(UTC):
            raise ConflictError("workspace_patch_not_applicable", "补丁不存在、已过期或已处理。")
        conversation = self.repository.get_conversation(conversation_id)
        policy = self._workspace_policy(conversation.workspace_path)
        if policy is None or self._workspace_hash(policy.root) != proposal.workspace_hash:
            raise ConflictError("workspace_mismatch", "当前工作区与提案不一致。")
        result = policy.apply_patch(
            [item.model_dump(mode="json") for item in proposal.edits],
            self.settings.allowed_work_root,
        )
        try:
            updated = self.repository.set_workspace_patch_applied(
                conversation_id,
                proposal_id,
                backup_dir=result["backup_dir"],
                postimage=result["postimage"],
            )
        except BaseException:
            self._restore_workspace_patch(policy, proposal, result["backup_dir"])
            raise
        with suppress(Exception):
            self.repository.invalidate_workspace_patch_undo(
                conversation_id, proposal.workspace_hash, proposal_id
            )
        from datetime import timedelta

        return WorkspacePatchApplyView(
            proposal_id=proposal_id,
            proposal_hash=proposal.proposal_hash,
            status=updated.status,
            applied_at=updated.applied_at,
            undo_expires_at=(
                updated.applied_at + timedelta(hours=24) if updated.applied_at else None
            ),
        )

    @staticmethod
    def _restore_workspace_patch(
        policy: WorkspaceFilePolicyPort, proposal: WorkspaceChangeProposal, backup_dir: str
    ) -> None:
        import shutil

        backup_root = Path(backup_dir)
        for item in proposal.edits:
            target = policy._relative(item.path, must_exist=False)
            backup = backup_root / item.path
            if backup.exists():
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(backup, target)
            else:
                target.unlink(missing_ok=True)

    def undo_workspace_proposal(
        self, conversation_id: str, proposal_id: str
    ) -> WorkspacePatchUndoView:
        proposal = self.repository.get_workspace_patch(conversation_id, proposal_id)
        if proposal.status != "applied":
            raise ConflictError("workspace_patch_not_undoable", "补丁当前不可撤销。")
        conversation = self.repository.get_conversation(conversation_id)
        policy = self._workspace_policy(conversation.workspace_path)
        if policy is None:
            raise InputValidationError("workspace_required", "请先选择工作区。")
        metadata = self.repository.get_workspace_patch_metadata(conversation_id, proposal_id)
        if metadata is None:
            raise ConflictError("workspace_patch_backup_missing", "补丁恢复快照不存在。")
        from datetime import UTC, datetime, timedelta

        if proposal.applied_at and proposal.applied_at + timedelta(hours=24) <= datetime.now(UTC):
            raise ConflictError("workspace_patch_undo_expired", "补丁撤销窗口已过期。")
        from pathlib import Path

        backup_dir = Path(metadata["backup_dir"]).resolve()
        try:
            backup_dir.relative_to(self.settings.allowed_work_root.resolve())
        except ValueError as exc:
            raise ConflictError("workspace_patch_backup_invalid", "补丁恢复目录无效。") from exc
        for path_text, digest in metadata["postimage"].items():
            target = (policy.root / path_text).resolve(strict=False)
            if not target.exists() or self._file_hash(target) != digest:
                raise ConflictError("workspace_file_changed", "文件已被后续修改，不能覆盖式撤销。")
        import shutil

        for item in proposal.edits:
            target = policy._relative(item.path, must_exist=False)
            backup = backup_dir / item.path
            if backup.exists():
                shutil.copy2(backup, target)
            else:
                target.unlink(missing_ok=True)
        self.repository.set_workspace_patch_undone(conversation_id, proposal_id)
        return WorkspacePatchUndoView(
            proposal_id=proposal_id, status="undone", undone_at=datetime.now(UTC)
        )

    def __init__(
        self,
        settings: Settings,
        database: DatabaseLifecyclePort,
        repository: RepositoryPort,
        *,
        path_policy: PathPolicyPort,
        dataset_inspector: DatasetInspectorPort,
        demographics_reader: DemographicsReaderPort,
        head_motion_qc_parser: HeadMotionQcParserPort,
        environment_provider: EnvironmentLockProvider,
        secret_resolver: SecretResolver,
        secret_writer: SecretWriterPort,
        providers: Mapping[str, ModelProvider],
        workspace_picker: WorkspacePickerPort | None = None,
        workspace_file_factory: Any | None = None,
        skill_registry: SkillRegistry | None = None,
    ) -> None:
        self.settings = settings

        self.database = database
        self.repository = repository
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
        self.context_engine: ContextEngine = cast(ContextEngine, DEFAULT_CONTEXT_MANAGER)
        self.semantic_memory = SemanticMemoryService(
            repository,
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
            repository,
            self.context_engine,
            self._generate_rsfmri_chat,
            self.semantic_memory,
        )
        self.conversation_work = ConversationWorkCoordinator(
            repository=repository,
            path_policy=path_policy,
            create_project=self.create_project,
            create_dataset=self.create_dataset,
            inspect_dataset=self.inspect_dataset,
            inspect_source=self._inspect_source,
            resolve_skill_plan=self.resolve_skill_plan,
            validate_plan_current=self._require_plan_current,
            get_qc_review=self.get_qc_review,
            get_latest_qc_review_for_run=repository.get_latest_qc_review_for_run,
            create_head_motion_qc_review=self.create_head_motion_qc_review,
            approve_qc_review=self.approve_qc_review,
            skip_qc=self.skip_qc,
            list_statistical_results=self.list_statistical_results,
            get_run=self.get_run,
            create_run=self.create_run,
            approve_plan=self.approve_plan,
            tool_result=self._tool_result,
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
            for spec in self.list_skills()
        ]
        work_tool_runner: WorkToolRunner | None = None
        if tool_policy is not None:
            # Bind non-optional: a closure does not keep the narrowing above.
            work_policy = tool_policy
            work_tool_runner = WorkToolRunner(
                build_messages=self._build_chat_messages,
                call_turn=self._tool_turn,
                dispatcher_factory=lambda context: build_work_dispatcher(
                    get_run=lambda run_id: self.get_run(run_id).model_dump(mode="json"),
                    diagnose_run=lambda run_id: self.diagnose_latest_run(run_id),
                    list_artifacts=lambda run_id: [
                        item.model_dump(mode="json") for item in self.list_artifacts(run_id)
                    ],
                    list_results=lambda run_id: self._statistical_results_for(run_id),
                    correction_capabilities=lambda: [
                        item.model_dump(mode="json") for item in self.correction_capabilities()
                    ],
                    list_skills=skill_summaries,
                    policy=work_policy,
                    project_id=context.project_id,
                    allowed_run_ids=context.allowed_run_ids,
                    workspace_policy=(
                        self._workspace_policy(context.workspace_path)
                        if context.workspace_content_consent
                        and self.settings.workspace_file_assistant_enabled
                        else None
                    ),
                    workspace_scope=context.workspace_file_scope,
                    workspace_stage_candidates=context.workspace_stage_candidates,
                    workspace_stage_check_available=context.workspace_path is not None,
                ),
                probe_capability=self._tool_capability,
                budget=tool_budget,
            )
        self.work_dialogue = WorkDialogue(
            self.conversation_work,
            lambda **kwargs: self._generate_rsfmri_chat(**kwargs),
            tool_runner=work_tool_runner,
            create_workspace_proposal=self.create_workspace_proposal,
            confirm_workspace_stage=self.conversation_work.confirm_workspace_stage,
        )
        uploaded_index = UploadedLiteratureIndex(
            work_root=settings.allowed_work_root,
            repository=cast(LiteratureRepository, repository),
            secret_resolver=secret_resolver,
            api_key_env=settings.rag_api_key_env,
            redaction_salt=settings.redaction_salt,
        )
        self.literature = LiteratureService(
            indexer=uploaded_index,
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
                    self.answer_rsfmri_question(
                        RsFmriQuestionRequest(question=question, allow_remote_search=False)
                    ).answer
                )
            ),
        )
        tool_runner: ChatToolRunner | None = None
        if tool_policy is not None:
            tool_runner = ChatToolRunner(
                build_messages=self._build_chat_messages,
                call_turn=self._tool_turn,
                dispatcher_factory=lambda collector: build_chat_dispatcher(
                    retrieve=lambda query, limit: rag_service.retrieve(query, limit=limit),
                    collector=collector,
                    policy=tool_policy,
                    list_skills=skill_summaries,
                ),
                probe_capability=self._tool_capability,
                budget=tool_budget,
            )
        self.chat_agent = ChatAgent(
            intent_router=ModelIntentRouter(
                self._classify_chat_intent,
                has_profiles=lambda: bool(self.repository.list_model_profiles()),
            ),
            rag_service=rag_service,
            context_manager=self.context_engine,
            memory_service=DEFAULT_MEMORY_SERVICE,
            llm_client=GatewayLlmClient(
                generate=self._generate_rsfmri_chat,
                has_profiles=lambda: bool(self.repository.list_model_profiles()),
            ),
            citation_service=DEFAULT_CITATION_SERVICE,
            tool_runner=tool_runner,
        )

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
        result = await self._generate_rsfmri_chat(
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

    def health(self) -> HealthView:
        self.database.ping()
        return HealthView()

    def _statistical_results_for(self, run_id: str) -> list[dict[str, Any]]:
        """Project a run's registered statistical results for the Work catalog."""

        run = self.get_run(run_id)
        return [
            item.model_dump(mode="json")
            for item in self.list_statistical_results(project_id=run.project_id, run_id=run_id)
        ]

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
