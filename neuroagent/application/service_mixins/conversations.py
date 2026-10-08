"""Persistent Chat/Work conversations with deterministic tool orchestration."""

# ruff: noqa: RUF001

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

from neuroagent.analysis.models import EvidenceChunk, RsFmriAnswer
from neuroagent.application.contracts import (
    ConversationAction,
    ConversationContextView,
    ConversationCreate,
    ConversationMessageView,
    ConversationMode,
    ConversationToolCallView,
    ConversationToolStatus,
    ConversationTurnCreate,
    ConversationTurnView,
    ConversationView,
    MemoryCreate,
    MemoryUpdate,
    MemoryView,
    QcExecutionMode,
    RsFmriAnswerView,
    RsFmriQuestionRequest,
    RunCreate,
    RunView,
    WorkConfirmationCreate,
    WorkDraftUpdate,
    WorkReviewCreate,
)
from neuroagent.application.conversation_context import (
    ContextBudgetError,
    ConversationContextCoordinator,
)
from neuroagent.application.conversation_work import (
    ConversationWorkCoordinator,
    WorkActionResult,
)
from neuroagent.application.errors import ConflictError, InputValidationError
from neuroagent.application.service_mixins._base import BaseServiceMixin
from neuroagent.application.work_dialogue import (
    STAGE_CONFIRMATION_PATTERN,
    WorkDialogue,
    execution_command,
    latest_payload,
)
from neuroagent.application.work_draft import (
    WorkDraft,
    draft_hash,
    draft_snapshot,
    execution_summary,
    invalidate_input_evidence,
    merge_work_draft,
    plan_preview,
    planning_decisions,
    render_draft_review,
    review_issues,
    validate_draft_patch,
)
from neuroagent.chat.agent import ChatAgent
from neuroagent.chat.interfaces import ChatAgentError
from neuroagent.chat.models import ChatAgentRequest
from neuroagent.memory.models import MemoryCandidateDraft
from neuroagent.observability.work_timings import capture_work_timings


class ConversationMixin(BaseServiceMixin):
    work_dialogue: WorkDialogue

    def _check_work_input_revision(
        self, conversation: ConversationView, saved: dict[str, Any] | None
    ) -> None:
        if not saved or not conversation.workspace_path:
            return
        if saved.get("input_evidence_stale") or saved.get("input_manifest_stale"):
            raise ConflictError("work_input_changed", "输入文件已变化，请通过对话重新读取并核实。")
        if not saved.get("input_revision") and not saved.get("facts"):
            return
        report = self.conversation_work.workspace_stage_candidates(conversation.workspace_path)
        revision = report.get("data_profile", {}).get("metadata_revision")
        if invalidate_input_evidence(saved, revision):
            raise ConflictError("work_input_changed", "输入文件已变化，请通过对话重新读取并核实。")

    answer_rsfmri_question: Callable[[RsFmriQuestionRequest], RsFmriAnswerView]
    get_run: Callable[[str], RunView]
    create_run: Callable[[RunCreate, str], RunView]
    chat_agent: ChatAgent
    conversation_context: ConversationContextCoordinator
    conversation_work: ConversationWorkCoordinator

    def create_conversation(
        self, request: ConversationCreate, idempotency_key: str
    ) -> ConversationView:
        title = request.title or (
            "fMRI 专项问答" if request.mode is ConversationMode.CHAT else "rs-fMRI 工作流"
        )
        welcome = (
            "这里是 fMRI 专项问答。我会检索项目内的 rs-fMRI、DPABI 和统计方法文档，"
            "并根据找到的证据回答。"
            if request.mode is ConversationMode.CHAT
            else "这里是 rs-fMRI 工作模式。请选择由你维护文件内容的工作区，"
            "我会协助准备预处理、查看进度和分析结果。"
        )
        return self._idempotent(
            scope=f"conversations:{request.mode.value}:create",
            key=idempotency_key,
            request=request,
            response_type=ConversationView,
            action=lambda: self.repository.create_conversation(
                mode=request.mode,
                title=title,
                welcome=welcome,
                workspace_path=request.workspace_path,
                preferred_profile_id=request.preferred_profile_id,
                project_id=request.project_id,
            ),
        )

    def list_conversations(self, mode: ConversationMode | None = None) -> list[ConversationView]:
        return self.repository.list_conversations(mode=mode)

    def get_conversation(self, conversation_id: str) -> ConversationView:
        return self.repository.get_conversation(conversation_id)

    def update_work_draft(
        self, conversation_id: str, request: WorkDraftUpdate, idempotency_key: str
    ) -> ConversationTurnView:
        def act() -> ConversationTurnView:
            conversation = self.repository.get_conversation(conversation_id)
            if conversation.mode is not ConversationMode.WORK:
                raise InputValidationError("work_draft_mode_required", "只能编辑 Work 方案草稿。")
            saved = latest_payload(conversation, "work_draft")
            if saved is None:
                raise ConflictError("work_draft_missing", "请先通过对话创建参数草稿。")
            if (
                not conversation.workspace_path
                or saved.get("workspace_path") != conversation.workspace_path
            ):
                raise ConflictError(
                    "work_draft_workspace_changed", "草稿与当前工作区不一致，请重新准备。"
                )
            draft = WorkDraft.model_validate(saved["draft"])
            self._check_work_input_revision(conversation, saved)
            if request.base_hash != draft_hash(draft):
                raise ConflictError("work_draft_changed", "参数草稿已更新，请核对最新版本后重试。")
            try:
                validate_draft_patch(request.patch)
                updated = merge_work_draft(draft, request.patch)
            except ValueError as exc:
                raise InputValidationError(
                    "work_draft_patch_invalid", "修改包含未知字段或不合法的参数结构。"
                ) from exc
            issues = review_issues(updated, saved.get("facts"))
            sources = dict(saved.get("sources", {}))

            def mark(value: Any, prefix: str = "") -> None:
                if isinstance(value, dict):
                    for name, child in value.items():
                        mark(child, f"{prefix}.{name}" if prefix else name)
                else:
                    sources[prefix] = "user"

            mark(request.patch)
            payload: dict[str, Any] = {
                "work_draft": draft_snapshot(
                    updated,
                    workspace_path=conversation.workspace_path,
                    sources=sources,
                    prior=saved,
                    facts=saved.get("facts"),
                ),
                "draft_issues": list(issues),
                "plan_preview": plan_preview(updated),
                "planning_decisions": planning_decisions(updated, sources),
            }
            review = render_draft_review(updated, sources)
            if not issues:
                payload["confirmation_request"] = {
                    "title": "确认方案参数",
                    "summary": review,
                    "options": [
                        {
                            "label": "确认并生成方案",
                            "description": "编译当前参数，不启动计算。",
                            "command": f"确认方案参数 {draft_hash(updated)[:12]}",
                        }
                    ],
                }
                dataset = latest_payload(conversation, "dataset")
                if dataset and conversation.project_id:
                    result = self.work_dialogue._compile(
                        conversation,
                        ConversationTurnCreate(content="准备更新后的方案审查"),
                        updated,
                        conversation.workspace_path,
                        conversation.project_id,
                        idempotency_key + ":review",
                        proposal=True,
                        snapshot=payload["work_draft"],
                    )
                    payload.update(result.payload)
                    review = result.assistant_content
            stored, user, assistant, tool = self.repository.append_conversation_exchange(
                conversation_id,
                user_content="已在参数面板保存修改。",
                assistant_content=review,
                assistant_payload=payload,
                tool=None,
                workspace_path=conversation.workspace_path,
                preferred_profile_id=conversation.preferred_profile_id,
                project_id=conversation.project_id,
                active_run_id=conversation.active_run_id,
            )
            return ConversationTurnView(
                conversation=stored, user_message=user, assistant_message=assistant, tool_call=tool
            )

        return self._idempotent(
            scope=f"conversations:{conversation_id}:work-draft",
            key=idempotency_key,
            request=request,
            response_type=ConversationTurnView,
            action=act,
        )

    async def confirm_work_action(
        self,
        conversation_id: str,
        request: WorkConfirmationCreate,
        idempotency_key: str,
    ) -> ConversationTurnView:
        return await self.send_conversation_turn(
            conversation_id,
            ConversationTurnCreate(content=request.command),
            idempotency_key,
            _confirmation=request,
        )

    def _validate_work_confirmation(
        self, conversation: ConversationView, request: WorkConfirmationCreate
    ) -> None:
        # Validate only after the idempotency lookup: a durable response must
        # remain recoverable even when the conversation has since changed.
        if conversation.mode is not ConversationMode.WORK:
            raise InputValidationError(
                "work_confirmation_mode_required", "此操作仅用于 Work 会话。"
            )
        saved = latest_payload(conversation, "work_draft")
        # Re-freezing is the recovery operation itself. The exact issued option
        # and the coordinator's live stage/path checks still apply below.
        if not re.fullmatch(STAGE_CONFIRMATION_PATTERN, request.command):
            self._check_work_input_revision(conversation, saved)
        if request.expected_draft_hash and (
            not saved or saved.get("hash") != request.expected_draft_hash
        ):
            raise ConflictError("work_draft_changed", "方案已更新，请审查最新版本。")
        confirmation = latest_payload(conversation, "confirmation_request")
        commands = [item.get("command") for item in (confirmation or {}).get("options", [])]
        if request.command not in commands:
            raise ConflictError("work_confirmation_changed", "确认操作已更新，请重新审查方案。")

    def prepare_work_review(
        self,
        conversation_id: str,
        request: WorkReviewCreate,
        idempotency_key: str,
    ) -> ConversationTurnView:
        def act() -> ConversationTurnView:
            conversation = self.repository.get_conversation(conversation_id)
            saved = latest_payload(conversation, "work_draft")
            if conversation.mode is not ConversationMode.WORK or not saved:
                raise ConflictError("work_draft_missing", "请先准备 Work 方案。")
            draft = WorkDraft.model_validate(saved["draft"])
            if (
                saved.get("workspace_path") != conversation.workspace_path
                or draft_hash(draft) != request.base_hash
            ):
                raise ConflictError("work_draft_changed", "工作区或方案已更新，请审查最新版本。")
            if saved.get("consumed"):
                raise ConflictError("work_review_already_started", "此方案已启动，请查看运行卡片。")
            recovery: WorkActionResult | None = None
            try:
                self._check_work_input_revision(conversation, saved)
            except ConflictError as exc:
                if exc.code != "work_input_changed":
                    raise
                report = self.conversation_work.workspace_stage_candidates(
                    conversation.workspace_path
                )
                revision = report.get("data_profile", {}).get("metadata_revision")
                invalidated = invalidate_input_evidence(saved, revision) or saved
                recovery = WorkActionResult(
                    "旧输入证据或确认已失效，参数选择已保留。请重新确认输入阶段并读取采集信息，再审查方案。",
                    payload={
                        "work_draft": invalidated,
                        "draft_issues": invalidated.get("review_issues", []),
                        "plan_preview": plan_preview(draft),
                        "planning_decisions": planning_decisions(
                            draft, invalidated.get("sources", {})
                        ),
                        "confirmation_request": self.work_dialogue.input_recovery_confirmation(
                            conversation.workspace_path, list(report.get("candidates", []))
                        ),
                    },
                )
            issues = review_issues(draft, saved.get("facts"))
            if issues and recovery is None:
                raise ConflictError("work_review_incomplete", "请先处理方案中的待填写或冲突项。")
            plan_data = latest_payload(conversation, "plan_revision")
            if recovery is not None:
                result = recovery
            elif saved.get("compiled") and plan_data and conversation.project_id:
                plan = self.repository.get_plan(str(plan_data["plan_revision_id"]))
                qc = (
                    QcExecutionMode.REQUIRE
                    if draft.qc_mode == "head_motion"
                    else QcExecutionMode.SKIP
                )
                challenge = self.conversation_work.create_confirmation_challenge(
                    conversation_id=conversation_id,
                    project_id=conversation.project_id,
                    plan_revision_id=plan.plan_revision_id,
                    plan_hash=plan.plan_hash,
                    qc_mode=qc,
                )
                result = WorkActionResult(
                    "方案保持原版本，启动确认已更新。请审查后确认本次计算。",
                    payload={
                        "work_draft": saved,
                        "draft_issues": [],
                        "plan_preview": plan_preview(draft),
                        "planning_decisions": planning_decisions(draft, saved.get("sources", {})),
                        "plan_revision": plan.model_dump(mode="json"),
                        "confirmation_request": {
                            "title": "确认并启动计算",
                            "summary": execution_summary(draft),
                            "options": [
                                {
                                    "label": "确认并启动计算",
                                    "description": "仅用于本次运行。",
                                    "command": execution_command(
                                        plan.plan_hash,
                                        "做头动QC" if draft.qc_mode == "head_motion" else "跳过QC",
                                        challenge,
                                    ),
                                }
                            ],
                        },
                    },
                )
            else:
                result = self.work_dialogue._compile(
                    conversation,
                    ConversationTurnCreate(content="重新校验当前方案"),
                    draft,
                    conversation.workspace_path,
                    conversation.project_id,
                    idempotency_key,
                    proposal=True,
                    snapshot=saved,
                )
            stored, user, assistant, tool = self.repository.append_conversation_exchange(
                conversation_id,
                user_content="重新校验当前方案与启动确认。",
                assistant_content=result.assistant_content,
                assistant_payload=result.payload,
                tool=result.tool,
                workspace_path=conversation.workspace_path,
                preferred_profile_id=conversation.preferred_profile_id,
                project_id=result.project_id or conversation.project_id,
                active_run_id=conversation.active_run_id,
            )
            return ConversationTurnView(
                conversation=stored, user_message=user, assistant_message=assistant, tool_call=tool
            )

        return self._idempotent(
            scope=f"conversations:{conversation_id}:work-review",
            key=idempotency_key,
            request=request,
            response_type=ConversationTurnView,
            action=act,
        )

    def get_conversation_context(self, conversation_id: str) -> ConversationContextView:
        return self.repository.get_conversation_context(conversation_id)

    def create_conversation_memory(
        self, conversation_id: str, request: MemoryCreate, idempotency_key: str
    ) -> MemoryView:
        def act() -> MemoryView:
            memory = self.repository.create_memory(conversation_id, request)
            self.repository.append_event(
                project_id=memory.project_id,
                run_id=None,
                event_type="MemoryUpdated",
                severity="info",
                payload={"memory_id": memory.memory_id, "action": "create"},
            )
            return memory

        return self._idempotent(
            scope=f"conversations:{conversation_id}:memories:create",
            key=idempotency_key,
            request=request,
            response_type=MemoryView,
            action=act,
        )

    def update_conversation_memory(
        self,
        conversation_id: str,
        memory_id: str,
        request: MemoryUpdate,
        idempotency_key: str,
    ) -> MemoryView:
        def act() -> MemoryView:
            memory = self.repository.update_memory(conversation_id, memory_id, request)
            self.repository.append_event(
                project_id=memory.project_id,
                run_id=None,
                event_type="MemoryUpdated",
                severity="info",
                payload={"memory_id": memory.memory_id, "action": request.action.value},
            )
            return memory

        return self._idempotent(
            scope=f"conversations:{conversation_id}:memories:{memory_id}",
            key=idempotency_key,
            request=request,
            response_type=MemoryView,
            action=act,
        )

    async def send_conversation_turn(
        self,
        conversation_id: str,
        request: ConversationTurnCreate,
        idempotency_key: str,
        *,
        _confirmation: WorkConfirmationCreate | None = None,
    ) -> ConversationTurnView:
        conversation = self.repository.get_conversation(conversation_id)
        if conversation.mode is ConversationMode.CHAT and _confirmation is None:
            return await self._send_chat_turn(
                conversation_id,
                request,
                idempotency_key,
            )

        async def prepare() -> tuple[
            int,
            WorkActionResult,
            dict[str, Any],
            tuple[MemoryCandidateDraft, ...],
            str | None,
            list[str],
            str | None,
            dict[str, Any] | None,
        ]:
            conversation = self.repository.get_conversation(conversation_id)
            if _confirmation is not None:
                self._validate_work_confirmation(conversation, _confirmation)
            workspace_path = request.workspace_path or conversation.workspace_path
            preferred_profile_id = (
                request.preferred_profile_id
                if request.preferred_profile_id is not None
                else conversation.preferred_profile_id
            )
            project_id = request.project_id or conversation.project_id
            active_run_id = conversation.active_run_id
            confirmed_stage = None
            for message in reversed(conversation.messages):
                manifest_payload = message.payload.get("manifest")
                if isinstance(manifest_payload, dict) and manifest_payload.get(
                    "selected_input_stage"
                ):
                    confirmed_stage = str(manifest_payload["selected_input_stage"])
                    break
            workspace_stage_candidates = self.conversation_work.workspace_stage_candidates(
                workspace_path,
                confirmed_stage=confirmed_stage,
            )
            try:
                prepared_context = self.conversation_context.prepare_work(
                    conversation,
                    question=request.content,
                    preferred_profile_id=preferred_profile_id,
                    project_id=project_id,
                    active_run_id=active_run_id,
                    plan_revision_id=request.plan_revision_id,
                    workspace_stage_candidates=workspace_stage_candidates,
                )
                work_packet = prepared_context.packet
                context_state = prepared_context.state
                preferred_profile_id = prepared_context.profile_id
            except ContextBudgetError as exc:
                raise InputValidationError(
                    "context_input_too_large", "当前消息超过所选模型的上下文预算。"
                ) from exc

            if request.action is ConversationAction.AUTO:
                action_result = await self.work_dialogue.respond(
                    conversation,
                    request,
                    work_packet,
                    workspace_path=workspace_path,
                    project_id=project_id,
                    preferred_profile_id=preferred_profile_id,
                    workspace_content_consent=(
                        self.workspace_content_consent_granted(
                            conversation_id, workspace_path, preferred_profile_id, request.model
                        )
                        if hasattr(self, "workspace_content_consent_granted")
                        else False
                    ),
                    idempotency_key=idempotency_key,
                    has_profiles=bool(self.repository.list_model_profiles()),
                )
            else:
                action_result = self.conversation_work.execute(
                    request,
                    workspace_path=workspace_path,
                    active_run_id=active_run_id,
                    project_id=project_id,
                    idempotency_key=idempotency_key,
                    conversation_id=conversation_id,
                )
            model_metadata = action_result.payload.get("model")
            return (
                conversation.version,
                action_result,
                work_packet.metadata,
                prepared_context.memory_candidates,
                context_state.summary.summary_id if context_state.summary else None,
                [item.memory_id for item in context_state.memories if item.pinned],
                preferred_profile_id,
                model_metadata,
            )

        def finalize(
            prepared: tuple[
                int,
                WorkActionResult,
                dict[str, Any],
                tuple[MemoryCandidateDraft, ...],
                str | None,
                list[str],
                str | None,
                dict[str, Any] | None,
            ],
        ) -> ConversationTurnView:
            (
                expected_conversation_version,
                action_result,
                context_metadata,
                memory_candidates,
                summary_id,
                pinned_memory_ids,
                preferred_profile_id,
                model_metadata,
            ) = prepared
            conversation = self.repository.get_conversation(conversation_id)
            if conversation.version != expected_conversation_version:
                raise ConflictError(
                    "conversation_changed_during_prepare",
                    "工作请求准备期间对话已变化，请基于最新状态重试。",
                )
            if action_result.commit is not None:
                action_result = action_result.commit()
            project_id = action_result.project_id or request.project_id or conversation.project_id
            workspace_path = action_result.workspace_path
            active_run_id = action_result.active_run_id
            payload = action_result.payload
            payload["context"] = context_metadata
            timings.add("prepare_total", timings.clock() - request_started)
            payload["work_timings"] = {
                "stages": timings.snapshot(),
                "note": "各阶段可能包含子步骤，耗时不可直接相加；未出现的阶段本轮未测量。",
            }

            stored, user_message, assistant_message, stored_tool = (
                self.repository.append_conversation_exchange(
                    conversation_id,
                    user_content=request.content,
                    assistant_content=action_result.assistant_content,
                    assistant_payload=payload,
                    tool=action_result.tool,
                    tool_calls=_loop_tool_calls(payload),
                    workspace_path=workspace_path,
                    preferred_profile_id=preferred_profile_id,
                    project_id=project_id,
                    active_run_id=active_run_id,
                )
            )
            candidate_ids = self.conversation_context.persist_memory_candidates(
                conversation_id,
                memory_candidates,
                user_message.message_id,
            )
            if candidate_ids:
                self.repository.append_event(
                    project_id=project_id,
                    run_id=active_run_id,
                    event_type="MemoryCandidatesUpdated",
                    severity="info",
                    payload={"conversation_id": conversation_id, "memory_ids": candidate_ids},
                )
            self.conversation_context.persist_snapshot(
                conversation_id,
                assistant_message_id=assistant_message.message_id,
                context_metadata=context_metadata,
                summary_id=summary_id,
                memory_ids=pinned_memory_ids,
                profile_id=preferred_profile_id,
                model_metadata=model_metadata,
            )
            return ConversationTurnView(
                conversation=stored,
                user_message=user_message,
                assistant_message=assistant_message,
                tool_call=stored_tool,
                tool_calls=_turn_loop_steps(stored, user_message),
            )

        with capture_work_timings() as timings:
            request_started = timings.clock()
            return await self._idempotent_async(
                scope=f"conversations:{conversation_id}:"
                + ("work-confirmation" if _confirmation is not None else "turns"),
                key=idempotency_key,
                request=_confirmation if _confirmation is not None else request,
                response_type=ConversationTurnView,
                prepare=prepare,
                finalize=finalize,
            )

    async def _send_chat_turn(
        self,
        conversation_id: str,
        request: ConversationTurnCreate,
        idempotency_key: str,
    ) -> ConversationTurnView:
        async def prepare() -> tuple[
            str,
            dict[str, Any],
            dict[str, Any],
            str | None,
        ]:
            conversation = self.repository.get_conversation(conversation_id)
            preferred_profile_id = (
                request.preferred_profile_id
                if request.preferred_profile_id is not None
                else conversation.preferred_profile_id
            )
            prepared_context = await self.conversation_context.prepare_chat(
                conversation,
                question=request.content,
                preferred_profile_id=preferred_profile_id,
                model=request.model,
            )
            preferred_profile_id = prepared_context.profile_id
            try:
                result = await self.chat_agent.respond(
                    ChatAgentRequest(
                        session_id=conversation_id,
                        message=request.content,
                        stream=request.stream,
                        paper_ids=request.paper_ids,
                        recent_messages=prepared_context.recent_messages,
                        pinned_context=prepared_context.pinned_context,
                        conversation_summary=(
                            prepared_context.summary.content if prepared_context.summary else None
                        ),
                        context_window_tokens=prepared_context.context_window_tokens,
                        max_output_tokens=prepared_context.max_output_tokens,
                        preferred_profile_id=preferred_profile_id,
                        model=request.model,
                        allow_remote_search=request.allow_remote_search,
                    )
                )
            except (ChatAgentError, ContextBudgetError) as exc:
                code = getattr(exc, "code", "context_input_too_large")
                message = getattr(exc, "message", "当前消息超过所选模型的上下文预算。")
                raise InputValidationError(code, message) from exc
            evidence = tuple(
                EvidenceChunk(
                    source=citation.source,
                    title=citation.title,
                    excerpt=citation.excerpt,
                    score=1.0,
                )
                for citation in result.citations
            )
            answer = RsFmriAnswer(
                in_scope=bool(result.metadata.get("in_scope", True)),
                answer=result.answer,
                evidence=evidence,
            )
            view = RsFmriAnswerView(
                answer=answer,
                remote_search_used=bool(result.metadata.get("remote_search_used", False)),
            )
            payload = {
                "rag": view.model_dump(mode="json"),
                "chat": result.model_dump(mode="json"),
                "context_summary_id": (
                    prepared_context.summary.summary_id if prepared_context.summary else None
                ),
                "memory_ids": list(prepared_context.memory_ids),
                "memory_recall": prepared_context.memory_recall,
                "memory_candidates": [
                    item.model_dump(mode="json") for item in prepared_context.memory_candidates
                ],
                "memory_extraction": prepared_context.memory_extraction,
            }
            model_metadata = result.metadata.get("model")
            if isinstance(model_metadata, dict):
                payload["model"] = model_metadata
                preferred_profile_id = str(model_metadata["profile_id"])
            tool_name = (
                "rsfmri_chat_llm"
                if result.metadata.get("llm_used")
                else "work_request_draft"
                if result.work_request is not None
                else "rag_rsfmri_question"
            )
            tool = self._tool_result(
                tool_name,
                {
                    "question": request.content,
                    "allow_remote_search": request.allow_remote_search,
                    "model_profile_id": preferred_profile_id,
                    "model": request.model,
                },
                payload,
            )
            return result.answer, payload, tool, preferred_profile_id

        def finalize(
            prepared: tuple[str, dict[str, Any], dict[str, Any], str | None],
        ) -> ConversationTurnView:
            return self._persist_chat_turn(conversation_id, request, prepared)

        return await self._idempotent_async(
            scope=f"conversations:{conversation_id}:turns",
            key=idempotency_key,
            request=request,
            response_type=ConversationTurnView,
            prepare=prepare,
            finalize=finalize,
        )

    def _persist_chat_turn(
        self,
        conversation_id: str,
        request: ConversationTurnCreate,
        prepared: tuple[str, dict[str, Any], dict[str, Any], str | None],
    ) -> ConversationTurnView:
        assistant_content, payload, tool, preferred_profile_id = prepared
        conversation = self.repository.get_conversation(conversation_id)
        loop_calls = _loop_tool_calls(payload)
        stored, user_message, assistant_message, stored_tool = (
            self.repository.append_conversation_exchange(
                conversation_id,
                user_content=request.content,
                assistant_content=assistant_content,
                assistant_payload=payload,
                tool=tool,
                tool_calls=loop_calls,
                workspace_path=conversation.workspace_path,
                preferred_profile_id=preferred_profile_id,
                project_id=conversation.project_id,
                active_run_id=conversation.active_run_id,
            )
        )
        candidates = tuple(
            MemoryCandidateDraft.model_validate(item)
            for item in payload.get("memory_candidates", [])
        )
        candidate_ids = self.conversation_context.persist_memory_candidates(
            conversation_id, candidates, user_message.message_id
        )
        if candidate_ids:
            self.repository.append_event(
                project_id=conversation.project_id,
                run_id=conversation.active_run_id,
                event_type="MemoryCandidatesUpdated",
                severity="info",
                payload={"conversation_id": conversation_id, "memory_ids": candidate_ids},
            )
        context_metadata = payload.get("chat", {}).get("metadata", {}).get("context", {})
        model_metadata = payload.get("model")
        context_hash = self.conversation_context.persist_snapshot(
            conversation_id,
            assistant_message_id=assistant_message.message_id,
            context_metadata=context_metadata,
            summary_id=payload.get("context_summary_id"),
            memory_ids=payload.get("memory_ids", []),
            profile_id=preferred_profile_id,
            model_metadata=model_metadata if isinstance(model_metadata, dict) else None,
        )
        self.repository.append_event(
            project_id=conversation.project_id,
            run_id=conversation.active_run_id,
            event_type="ContextBuilt",
            severity="info",
            payload={"conversation_id": conversation_id, "context_hash": context_hash},
        )
        return ConversationTurnView(
            conversation=stored,
            user_message=user_message,
            assistant_message=assistant_message,
            tool_call=stored_tool,
            tool_calls=_turn_loop_steps(stored, user_message),
        )

    @staticmethod
    def _tool_result(
        name: str, input_data: dict[str, Any], output: dict[str, Any]
    ) -> dict[str, Any]:
        return {
            "tool_name": name,
            "status": ConversationToolStatus.SUCCEEDED.value,
            "input": input_data,
            "output": output,
        }


def _turn_loop_steps(
    conversation: ConversationView, user_message: ConversationMessageView
) -> list[ConversationToolCallView]:
    """The Agent tool-loop steps this turn produced, in execution order.

    Shared by the Chat and Work paths so both report the same contract.  Only
    rows carrying a ``step_index`` are loop steps; the turn's summary tool record
    is reported separately through ``tool_call``.
    """

    return [
        item
        for item in conversation.tool_calls
        if item.user_message_id == user_message.message_id and item.step_index is not None
    ]


def _loop_tool_calls(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract bounded-tool-loop audit records from a turn payload.

    Chat nests them under ``chat.metadata.tool_loop``; Work keeps ``tool_loop`` at
    the top level of its payload.  Both shapes are accepted.  The records were
    produced by the dispatcher, so their arguments and results are already
    redacted and bounded; malformed entries are dropped rather than trusted,
    because this payload has been persisted and re-read before.
    """

    loop: Any = payload.get("tool_loop")
    if not isinstance(loop, dict):
        chat = payload.get("chat")
        metadata = chat.get("metadata") if isinstance(chat, dict) else None
        loop = metadata.get("tool_loop") if isinstance(metadata, dict) else None
    if not isinstance(loop, dict):
        return []
    raw_calls = loop.get("calls")
    if not isinstance(raw_calls, list):
        return []

    calls: list[dict[str, Any]] = []
    for entry in raw_calls:
        if not isinstance(entry, dict):
            continue
        name = entry.get("tool_name")
        status = entry.get("status")
        if not isinstance(name, str) or not name or not isinstance(status, str):
            continue
        calls.append(
            {
                "tool_name": name,
                "status": status,
                "input": entry.get("input") if isinstance(entry.get("input"), dict) else {},
                "output": entry.get("output") if isinstance(entry.get("output"), dict) else {},
                "error": entry.get("error") if isinstance(entry.get("error"), str) else None,
                "step_index": entry.get("step_index"),
                "risk_tier": entry.get("risk_tier"),
                "provider_call_id": entry.get("provider_call_id"),
                "duration_ms": entry.get("duration_ms"),
            }
        )
    return calls
