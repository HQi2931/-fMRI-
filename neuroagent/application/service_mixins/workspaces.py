"""Workspace permission and patch use cases."""

# ruff: noqa: RUF001

from __future__ import annotations

import hashlib
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from neuroagent.agent.gateway import ModelGateway, ModelGatewayError
from neuroagent.agent.models import ModelCapability
from neuroagent.agent.providers import ProviderError
from neuroagent.agent.tool_spec import LlmTool, ToolTier
from neuroagent.application.contracts.agent import ModelProfileView
from neuroagent.application.contracts.conversations import (
    ConversationView,
    WorkspaceChangeProposal,
    WorkspaceContentConsent,
    WorkspaceContentConsentCreate,
    WorkspacePatchApplyRequest,
    WorkspacePatchApplyView,
    WorkspacePatchUndoView,
)
from neuroagent.application.errors import ConflictError, InputValidationError
from neuroagent.application.hashing import canonical_json
from neuroagent.application.ports import WorkspaceFilePolicyPort
from neuroagent.application.repository_ports import WorkspaceRepositoryPort
from neuroagent.application.service_mixins._base import BaseServiceMixin


class WorkspaceMixin(BaseServiceMixin):
    repository: WorkspaceRepositoryPort
    model_gateway: Callable[[], ModelGateway]
    workspace_policy_version = "workspace-files-v1"
    get_conversation: Callable[[str], ConversationView]
    get_model_profile: Callable[[str], ModelProfileView]
    list_model_profiles: Callable[[], list[ModelProfileView]]

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
            profile_view = self.get_model_profile(profile_id)
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
        conversation = self.get_conversation(conversation_id)
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
        latest_conversation = self.get_conversation(conversation_id)
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
        self.event_store.append_event(
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
            result = await self.model_gateway().generate_tool_turn(
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
        conversation = self.get_conversation(conversation_id)
        try:
            root = self._workspace_root(conversation.workspace_path)
            workspace_hash = self._workspace_hash(root) if root is not None else None
        except Exception:
            workspace_hash = None
        profiles = {item.profile.id: item for item in self.list_model_profiles()}
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
        self.event_store.append_event(
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
        conversation = self.get_conversation(conversation_id)
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
        conversation = self.get_conversation(conversation_id)
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
        conversation = self.get_conversation(conversation_id)
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
