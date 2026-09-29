"""ChatAgent orchestration with no persistence or execution responsibilities."""

# ruff: noqa: RUF001

from __future__ import annotations

import re

from neuroagent.chat.evidence import EvidenceCollector
from neuroagent.chat.interfaces import ChatAgentError, CitationService, IntentRouter, LLMClient
from neuroagent.chat.models import ChatAgentRequest, ChatAgentResponse, ChatIntent
from neuroagent.chat.tool_loop import ChatToolLoopResult, ChatToolRunner
from neuroagent.context.interfaces import ContextManager
from neuroagent.memory.interfaces import MemoryService
from neuroagent.retrieval.interfaces import RagService, RetrievalResult


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
        tool_runner: ChatToolRunner | None = None,
    ) -> None:
        self._intent_router = intent_router
        self._rag_service = rag_service
        self._context_manager = context_manager
        self._memory_service = memory_service
        self._llm_client = llm_client
        self._citation_service = citation_service
        # Optional: when absent, Chat keeps the single-shot retrieval path.
        self._tool_runner = tool_runner

    async def respond(self, request: ChatAgentRequest) -> ChatAgentResponse:
        routed = await self._intent_router.route(request)
        intent, work_request = routed.intent, routed.work_request
        if intent is ChatIntent.WORK_REQUEST:
            if work_request is None:
                raise ChatAgentError("chat_intent_invalid", "执行需求缺少任务草案，请重试。")
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
                    "streaming": request.stream,
                    "work_dispatched": False,
                },
            )

        memory = self._memory_service.recall(
            session_id=request.session_id,
            recent_messages=request.recent_messages,
            pinned_context=request.pinned_context,
            conversation_summary=request.conversation_summary,
        )
        retrieval_performed = intent is ChatIntent.KNOWLEDGE_QUERY
        retrieval = (
            await self._rag_service.retrieve(
                routed.query,
                filters={"paper_ids": list(request.paper_ids)} if request.paper_ids else None,
            )
            if retrieval_performed
            else RetrievalResult(
                suggested_answer="你好，我可以协助你阅读论文和讨论 rs-fMRI 科研方法。"
            )
        )
        context = self._context_manager.build(
            question=request.message,
            recent_messages=memory.recent_messages,
            retrieval_context=retrieval.chunks,
            pinned_context=memory.pinned_context,
            conversation_summary=memory.conversation_summary,
            context_window_tokens=request.context_window_tokens,
            max_output_tokens=request.max_output_tokens,
            context_kind=intent.value,
            work_context=request.work_context,
        )
        llm_result = None
        loop_result: ChatToolLoopResult | None = None
        loop_candidate: ChatToolLoopResult | None = None
        tool_evidence: EvidenceCollector | None = None
        if retrieval.in_scope and self._llm_client.available(request.preferred_profile_id):
            if (
                retrieval_performed
                and self._tool_runner is not None
                and await self._tool_runner.available(request.preferred_profile_id)
            ):
                collector = EvidenceCollector()
                # Seed with the upfront evidence so the loop can only add to it,
                # and citation ids stay identical to what the citation service
                # assigns when it enumerates the same collector.
                collector.add(retrieval.chunks)
                seed = collector.add(context.retrieval_context)
                candidate = await self._tool_runner.run(
                    question=request.message,
                    evidence=[
                        {
                            "citation_id": citation_id,
                            "source": chunk.source,
                            "title": chunk.title,
                            "excerpt": chunk.text,
                            "score": chunk.score,
                            "chunk_id": chunk.chunk_id,
                            "paper_id": chunk.paper_id,
                            "section": chunk.section,
                            "subsection": chunk.subsection,
                            "page_start": chunk.page_start,
                            "page_end": chunk.page_end,
                        }
                        for citation_id, chunk in seed
                    ],
                    recent_messages=[item.model_dump() for item in memory.recent_messages],
                    pinned_context=[item.model_dump() for item in memory.pinned_context],
                    conversation_summary=memory.conversation_summary,
                    work_context=request.work_context,
                    preferred_profile_id=request.preferred_profile_id,
                    model=request.model,
                    collector=collector,
                    context_window_tokens=request.context_window_tokens,
                    max_output_tokens=request.max_output_tokens,
                )
                loop_candidate = candidate
                # A loop that produced no answer (budget exhausted, provider
                # error) must not degrade the reply, so fall back to one shot.
                if candidate.final_text.strip():
                    loop_result = candidate
                    tool_evidence = collector
            if loop_result is None:
                llm_result = await self._llm_client.generate(
                    context,
                    preferred_profile_id=request.preferred_profile_id,
                    model=request.model,
                    allow_remote_search=request.allow_remote_search,
                )
        citation_source = (
            tool_evidence.chunks if tool_evidence is not None else context.retrieval_context
        )
        citations = self._citation_service.build(
            citation_source,
            provider_citations=(llm_result.provider_citations if llm_result is not None else ()),
        )
        if loop_result is not None:
            answer = loop_result.final_text
        elif llm_result is not None:
            answer = llm_result.content
        else:
            answer = retrieval.suggested_answer or "当前没有足够证据回答这个问题。"
        known = {
            citation.citation_id
            for citation in citations
            if citation.paper_id is not None or not citation.chunk_id.startswith("web:")
        }
        cited = set(re.findall(r"\[(C\d+)\]", answer))
        unknown = cited - known
        if unknown:
            answer = re.sub(
                r"\[(C\d+)\]", lambda match: "" if match[1] in unknown else match[0], answer
            )
            answer += "\n\n部分引用未能对应本次证据，已移除无效编号，引用不完整。"
        citations = tuple(
            citation
            for citation in citations
            if (citation.citation_id in cited and citation.citation_id in known)
            or citation.chunk_id.startswith("web:")
        )
        if (
            retrieval_performed
            and not retrieval.chunks
            and not (llm_result and llm_result.provider_citations)
        ):
            answer = "未找到文献依据；以下内容仅供一般方法讨论。\n\n" + answer
        metadata = {
            "retrieval_performed": retrieval_performed,
            "retrieval_query": routed.query if retrieval_performed else None,
            "retrieved_chunk_count": len(retrieval.chunks),
            "in_scope": retrieval.in_scope,
            "llm_used": llm_result is not None,
            "streaming": request.stream,
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
                "redaction_count": llm_result.redaction_count,
            }
        if loop_result is not None:
            metadata["llm_used"] = True
            metadata["retrieved_chunk_count"] = len(tool_evidence.chunks) if tool_evidence else 0
            metadata["model"] = {
                "profile_id": loop_result.profile_id,
                "model": loop_result.model,
                "context_hash": loop_result.context_hash,
                "attempted_profile_ids": list(loop_result.attempted_profile_ids),
                "usage": loop_result.usage,
                "remote_search_used": False,
                "redaction_count": loop_result.redaction_count,
            }
            metadata["tool_loop"] = _chat_loop_metadata(loop_result)
        elif loop_candidate is not None:
            # Preserve attempted calls when the candidate was empty and the
            # response fell back to the single-shot path.
            metadata["tool_loop"] = _chat_loop_metadata(loop_candidate)
        metadata["context"] = context.metadata
        return ChatAgentResponse(
            session_id=request.session_id,
            intent=intent,
            answer=answer,
            citations=citations,
            metadata=metadata,
        )


def _chat_loop_metadata(looped: ChatToolLoopResult) -> dict[str, object]:
    return {
        "enabled": True,
        "stop_reason": looped.stop_reason,
        "steps": looped.steps,
        "tool_calls": looped.tool_calls,
        "tool_names": list(looped.tool_names),
        "fallback_candidate": not bool(looped.final_text.strip()),
        "fallback_used": not bool(looped.final_text.strip()),
        "calls": [
            {
                "tool_name": outcome.name,
                "status": outcome.status.value,
                "risk_tier": outcome.tier.value if outcome.tier is not None else None,
                "step_index": index,
                "provider_call_id": outcome.call_id,
                "duration_ms": outcome.duration_ms,
                "input": outcome.arguments,
                "output": outcome.result,
                "error": outcome.error_code or outcome.message,
            }
            for index, outcome in enumerate(looped.outcomes, start=1)
        ],
    }
