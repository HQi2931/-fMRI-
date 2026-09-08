"""ChatAgent orchestration with no persistence or execution responsibilities."""

# ruff: noqa: RUF001

from __future__ import annotations

import inspect

from neuroagent.chat.interfaces import ChatAgentError, CitationService, IntentRouter, LLMClient
from neuroagent.chat.models import ChatAgentRequest, ChatAgentResponse, ChatIntent
from neuroagent.context.interfaces import ContextManager
from neuroagent.memory.interfaces import MemoryService
from neuroagent.retrieval.interfaces import RagService


class ChatAgent:
    def __init__(
        self,
        *,
        intent_router: IntentRouter,
        rag_service: RagService,
        context_manager: ContextManager,
        memory_service: MemoryService,
        llm_client: LLMClient,
        citation_service: CitationService,
    ) -> None:
        self._intent_router = intent_router
        self._rag_service = rag_service
        self._context_manager = context_manager
        self._memory_service = memory_service
        self._llm_client = llm_client
        self._citation_service = citation_service

    async def respond(self, request: ChatAgentRequest) -> ChatAgentResponse:
        if request.stream:
            raise ChatAgentError(
                "chat_streaming_not_implemented",
                "Chat token streaming 尚未在本阶段启用。",
            )
        routed = self._intent_router.route(request.message)
        intent, work_request = (
            await routed if inspect.isawaitable(routed) else routed
        )
        if intent is ChatIntent.WORK_REQUEST:
            assert work_request is not None
            return ChatAgentResponse(
                session_id=request.session_id,
                intent=intent,
                answer=(
                    "已将这条执行型需求整理为待确认的 WorkRequest 草案；"
                    "当前 Chat Mode 不会执行预处理、MATLAB 或数据分析。"
                ),
                work_request=work_request,
                metadata={
                    "retrieval_performed": False,
                    "llm_used": False,
                    "streaming": False,
                    "work_dispatched": False,
                },
            )

        memory = self._memory_service.recall(
            session_id=request.session_id,
            recent_messages=request.recent_messages,
            pinned_context=request.pinned_context,
        )
        retrieval = await self._rag_service.retrieve(request.message)
        context = self._context_manager.build(
            question=request.message,
            recent_messages=memory.recent_messages,
            retrieval_context=retrieval.chunks,
            pinned_context=memory.pinned_context,
            conversation_summary=memory.conversation_summary,
        )
        llm_result = None
        if retrieval.in_scope and self._llm_client.available(request.preferred_profile_id):
            llm_result = await self._llm_client.generate(
                context,
                preferred_profile_id=request.preferred_profile_id,
                model=request.model,
                allow_remote_search=request.allow_remote_search,
            )
        citations = self._citation_service.build(
            retrieval.chunks,
            provider_citations=(
                llm_result.provider_citations if llm_result is not None else ()
            ),
        )
        answer = (
            llm_result.content
            if llm_result is not None
            else retrieval.suggested_answer
            or "当前没有足够证据回答这个问题。"
        )
        metadata = {
            "retrieval_performed": True,
            "retrieved_chunk_count": len(retrieval.chunks),
            "in_scope": retrieval.in_scope,
            "llm_used": llm_result is not None,
            "streaming": False,
            "remote_search_used": bool(llm_result and llm_result.remote_search_used),
            "rag": retrieval.metadata.get("legacy_view"),
            "retrieval": retrieval.metadata,
        }
        if llm_result is not None:
            metadata["model"] = {
                "profile_id": llm_result.profile_id,
                "model": llm_result.model,
                "context_hash": llm_result.context_hash,
                "attempted_profile_ids": list(llm_result.attempted_profile_ids),
                "usage": llm_result.usage,
                "remote_search_used": llm_result.remote_search_used,
            }
        return ChatAgentResponse(
            session_id=request.session_id,
            intent=intent,
            answer=answer,
            citations=citations,
            metadata=metadata,
        )
