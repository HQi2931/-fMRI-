"""Small default services used by the phase-one ChatAgent."""

# ruff: noqa: RUF001

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Awaitable, Callable
from typing import Any

from neuroagent.analysis.models import RsFmriAnswer
from neuroagent.chat.interfaces import LlmResult
from neuroagent.chat.models import ChatIntent, Citation, WorkRequest
from neuroagent.context.interfaces import (
    ContextManager,
    ContextMessage,
    ContextPacket,
    PinnedContext,
)
from neuroagent.memory.interfaces import MemoryService, MemorySnapshot
from neuroagent.retrieval.interfaces import RetrievalResult, RetrievedChunk


class RuleBasedIntentRouter:
    """Conservative router that only drafts work; it never dispatches it."""

    _WORK_VERBS = re.compile(
        r"(?:帮我|请|替我|按照.*(?:方法|文献).*)?(?:计算|运行|执行|预处理|分析我的|处理我的|"
        r"calculate|run|execute|preprocess|analy[sz]e my)",
        re.IGNORECASE,
    )
    _KNOWLEDGE_MARKERS = re.compile(
        r"(?:是什么|什么意思|为什么|如何理解|区别|原理|介绍|解释|怎么|怎样|能否|"
        r"可以吗|吗[？?。！!]?$|what is|why|explain|difference)",
        re.IGNORECASE,
    )
    _BAND = re.compile(r"(0?\.\d+)\s*(?:-|–|—|至|到|,)\s*(0?\.\d+)")

    def route(self, message: str) -> tuple[ChatIntent, WorkRequest | None]:
        text = message.strip()
        if self._WORK_VERBS.search(text) and not self._KNOWLEDGE_MARKERS.search(text):
            lowered = text.casefold()
            if "alff" in lowered:
                task = "calculate_alff"
            elif any(term in lowered for term in ("预处理", "preprocess", "dpabi", "dparsf")):
                task = "preprocess_rsfmri"
            elif any(term in lowered for term in ("统计", "分析", "analy")):
                task = "analyze_data"
            else:
                task = "unspecified_work"
            parameters: dict[str, Any] = {}
            band = self._BAND.search(text)
            if band:
                parameters["frequency_band"] = [float(band.group(1)), float(band.group(2))]
            return ChatIntent.WORK_REQUEST, WorkRequest(task=task, parameters=parameters)
        if self._KNOWLEDGE_MARKERS.search(text) or "?" in text or "？" in text:
            return ChatIntent.KNOWLEDGE_QUERY, None
        return ChatIntent.CONVERSATION, None


class ModelIntentRouter:
    """Classify intent with the configured LLM and fail closed to rules.

    The classifier only returns a small allow-listed JSON contract. It never
    dispatches work; malformed, unavailable, or uncertain model output uses
    ``RuleBasedIntentRouter`` instead.
    """

    def __init__(
        self,
        classify: Callable[[str], Awaitable[str]],
        *,
        fallback: RuleBasedIntentRouter | None = None,
    ) -> None:
        self._classify = classify
        self._fallback = fallback or RuleBasedIntentRouter()

    async def route(self, message: str) -> tuple[ChatIntent, WorkRequest | None]:
        try:
            raw = await self._classify(message)
            payload = _parse_intent_payload(raw)
            intent = ChatIntent(payload["intent"])
            if intent is ChatIntent.WORK_REQUEST:
                task = payload.get("task")
                if not isinstance(task, str) or not task.strip():
                    raise ValueError("work request task is missing")
                parameters = payload.get("parameters", {})
                if not isinstance(parameters, dict):
                    raise ValueError("work request parameters are invalid")
                return intent, WorkRequest(task=task.strip(), parameters=parameters)
            return intent, None
        except Exception:
            return self._fallback.route(message)


def _parse_intent_payload(raw: str) -> dict[str, Any]:
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.IGNORECASE)
    payload = json.loads(text)
    if not isinstance(payload, dict) or payload.get("intent") not in {
        ChatIntent.KNOWLEDGE_QUERY.value,
        ChatIntent.CONVERSATION.value,
        ChatIntent.WORK_REQUEST.value,
    }:
        raise ValueError("unknown intent")
    return payload


class RequestMemoryService:
    def __init__(self, *, recent_limit: int = 12) -> None:
        self._recent_limit = recent_limit

    def recall(
        self,
        *,
        session_id: str,
        recent_messages: tuple[ContextMessage, ...],
        pinned_context: tuple[PinnedContext, ...],
    ) -> MemorySnapshot:
        del session_id
        return MemorySnapshot(
            recent_messages=recent_messages[-self._recent_limit :],
            pinned_context=pinned_context,
        )


class DefaultContextManager:
    def build(
        self,
        *,
        question: str,
        recent_messages: tuple[ContextMessage, ...],
        retrieval_context: tuple[RetrievedChunk, ...],
        pinned_context: tuple[PinnedContext, ...],
        conversation_summary: str | None,
    ) -> ContextPacket:
        return ContextPacket(
            question=question,
            recent_messages=recent_messages,
            retrieval_context=retrieval_context,
            pinned_context=pinned_context,
            conversation_summary=conversation_summary,
            metadata={"phase": "chat_phase_1"},
        )


class EvidenceCitationService:
    """Build citations only from typed retrieval/provider evidence."""

    def build(
        self,
        chunks: tuple[RetrievedChunk, ...],
        *,
        provider_citations: tuple[dict[str, str], ...] = (),
    ) -> tuple[Citation, ...]:
        citations = [
            Citation(
                citation_id=f"C{index}",
                chunk_id=chunk.chunk_id,
                paper_id=chunk.paper_id,
                source=chunk.source,
                title=chunk.title,
                section=chunk.section,
                subsection=chunk.subsection,
                page_start=chunk.page_start,
                page_end=chunk.page_end,
                excerpt=chunk.text[:500],
            )
            for index, chunk in enumerate(chunks, start=1)
        ]
        seen_urls = {citation.source for citation in citations}
        for item in provider_citations:
            url, title = item.get("url"), item.get("title")
            if not url or not title or url in seen_urls:
                continue
            seen_urls.add(url)
            digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:24]
            citations.append(
                Citation(
                    citation_id=f"C{len(citations) + 1}",
                    chunk_id=f"web:{digest}",
                    source=url,
                    title=title,
                    excerpt="联网来源；请打开原文核对回答中的引用。",
                )
            )
        return tuple(citations)


class LocalEvidenceRagService:
    """Adapter for the existing local documentation evidence search."""

    def __init__(self, answer: Callable[[str], RsFmriAnswer]) -> None:
        self._answer = answer

    async def retrieve(
        self,
        query: str,
        *,
        limit: int = 8,
        filters: dict[str, Any] | None = None,
    ) -> RetrievalResult:
        del limit, filters
        answer = self._answer(query)
        chunks = tuple(
            RetrievedChunk(
                chunk_id="local:" + hashlib.sha256(
                    f"{item.source}\0{item.title}\0{item.excerpt}".encode()
                ).hexdigest()[:24],
                source=item.source,
                title=item.title.replace("\\", "/"),
                text=item.excerpt,
                score=item.score,
                metadata={"source_kind": "project_document"},
            )
            for item in answer.evidence
        )
        return RetrievalResult(
            in_scope=answer.in_scope,
            chunks=chunks,
            suggested_answer=answer.answer,
            metadata={
                "legacy_view": {
                    "answer": answer.model_dump(mode="json"),
                    "remote_search_used": False,
                }
            },
        )


class GatewayLlmClient:
    def __init__(
        self,
        *,
        generate: Callable[..., Awaitable[Any]],
        has_profiles: Callable[[], bool],
    ) -> None:
        self._generate = generate
        self._has_profiles = has_profiles

    def available(self, preferred_profile_id: str | None) -> bool:
        return preferred_profile_id is not None or self._has_profiles()

    async def generate(
        self,
        context: ContextPacket,
        *,
        preferred_profile_id: str | None,
        model: str | None,
        allow_remote_search: bool,
    ) -> LlmResult:
        result = await self._generate(
            question=context.question,
            evidence=[
                {
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
                for chunk in context.retrieval_context
            ],
            preferred_profile_id=preferred_profile_id,
            model=model,
            allow_web_search=allow_remote_search,
        )
        return LlmResult(
            content=result.response.content,
            model=result.response.model,
            profile_id=result.selected_profile_id,
            context_hash=result.context_hash,
            attempted_profile_ids=result.attempted_profile_ids,
            usage=result.response.usage,
            provider_citations=tuple(
                {"url": citation.url, "title": citation.title}
                for citation in result.response.citations
            ),
            remote_search_used=result.remote_search_used,
        )


DEFAULT_INTENT_ROUTER = RuleBasedIntentRouter()
DEFAULT_MEMORY_SERVICE: MemoryService = RequestMemoryService()
DEFAULT_CONTEXT_MANAGER: ContextManager = DefaultContextManager()
DEFAULT_CITATION_SERVICE = EvidenceCitationService()
