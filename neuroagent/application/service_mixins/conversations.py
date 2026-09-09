"""Persistent Chat/Work conversations with deterministic tool orchestration."""

# ruff: noqa: RUF001

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from neuroagent.analysis.models import EvidenceChunk, RsFmriAnswer
from neuroagent.application.contracts import (
    ConversationContextView,
    ConversationCreate,
    ConversationMode,
    ConversationToolStatus,
    ConversationTurnCreate,
    ConversationTurnView,
    ConversationView,
    MemoryCreate,
    MemoryUpdate,
    MemoryView,
    RsFmriAnswerView,
    RsFmriQuestionRequest,
    RunCreate,
    RunView,
    WorkspaceCheckRequest,
    WorkspaceCheckView,
)
from neuroagent.application.conversation_context import (
    ContextBudgetError,
    ConversationContextCoordinator,
)
from neuroagent.application.conversation_work import ConversationWorkCoordinator
from neuroagent.application.errors import InputValidationError
from neuroagent.application.service_mixins._base import BaseServiceMixin
from neuroagent.chat.agent import ChatAgent
from neuroagent.chat.interfaces import ChatAgentError
from neuroagent.chat.models import ChatAgentRequest


class ConversationMixin(BaseServiceMixin):
    answer_rsfmri_question: Callable[[RsFmriQuestionRequest], RsFmriAnswerView]
    check_workspace: Callable[[WorkspaceCheckRequest], WorkspaceCheckView]
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
            else "这里是 rs-fMRI 工作模式。请选择工作区，我会调用检查工具，"
            "再协助启动受控预处理和查看进度。"
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
            ),
        )

    def list_conversations(self, mode: ConversationMode | None = None) -> list[ConversationView]:
        return self.repository.list_conversations(mode=mode)

    def get_conversation(self, conversation_id: str) -> ConversationView:
        return self.repository.get_conversation(conversation_id)

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
    ) -> ConversationTurnView:
        conversation = self.repository.get_conversation(conversation_id)
        if conversation.mode is ConversationMode.CHAT:
            return await self._send_chat_turn(
                conversation_id,
                request,
                idempotency_key,
            )

        def act() -> ConversationTurnView:
            conversation = self.repository.get_conversation(conversation_id)
            workspace_path = request.workspace_path or conversation.workspace_path
            preferred_profile_id = (
                request.preferred_profile_id
                if request.preferred_profile_id is not None
                else conversation.preferred_profile_id
            )
            project_id = request.project_id or conversation.project_id
            active_run_id = conversation.active_run_id
            tool: dict[str, Any] | None = None
            payload: dict[str, Any] = {}
            try:
                prepared_context = self.conversation_context.prepare_work(
                    conversation,
                    question=request.content,
                    preferred_profile_id=preferred_profile_id,
                    project_id=project_id,
                    active_run_id=active_run_id,
                    plan_revision_id=request.plan_revision_id,
                )
                work_packet = prepared_context.packet
                context_state = prepared_context.state
                preferred_profile_id = prepared_context.profile_id
            except ContextBudgetError as exc:
                raise InputValidationError(
                    "context_input_too_large", "当前消息超过所选模型的上下文预算。"
                ) from exc

            action_result = self.conversation_work.execute(
                request,
                workspace_path=workspace_path,
                active_run_id=active_run_id,
                project_id=project_id,
                idempotency_key=idempotency_key,
            )
            workspace_path = action_result.workspace_path
            active_run_id = action_result.active_run_id
            payload = action_result.payload
            tool = action_result.tool
            assistant_content = action_result.assistant_content
            payload["context"] = work_packet.metadata

            stored, user_message, assistant_message, stored_tool = (
                self.repository.append_conversation_exchange(
                    conversation_id,
                    user_content=request.content,
                    assistant_content=assistant_content,
                    assistant_payload=payload,
                    tool=tool,
                    workspace_path=workspace_path,
                    preferred_profile_id=preferred_profile_id,
                    project_id=project_id,
                    active_run_id=active_run_id,
                )
            )
            self.conversation_context.persist_snapshot(
                conversation_id,
                assistant_message_id=assistant_message.message_id,
                context_metadata=work_packet.metadata,
                summary_id=context_state.summary.summary_id if context_state.summary else None,
                memory_ids=[item.memory_id for item in context_state.memories if item.pinned],
                profile_id=preferred_profile_id,
            )
            return ConversationTurnView(
                conversation=stored,
                user_message=user_message,
                assistant_message=assistant_message,
                tool_call=stored_tool,
            )

        return self._idempotent(
            scope=f"conversations:{conversation_id}:turns",
            key=idempotency_key,
            request=request,
            response_type=ConversationTurnView,
            action=act,
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
        stored, user_message, assistant_message, stored_tool = (
            self.repository.append_conversation_exchange(
                conversation_id,
                user_content=request.content,
                assistant_content=assistant_content,
                assistant_payload=payload,
                tool=tool,
                workspace_path=conversation.workspace_path,
                preferred_profile_id=preferred_profile_id,
                project_id=conversation.project_id,
                active_run_id=conversation.active_run_id,
            )
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
