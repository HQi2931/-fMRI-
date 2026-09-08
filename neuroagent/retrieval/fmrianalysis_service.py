"""Chat adapter over the reused fMRIAnalysis retriever, with one final rerank."""

# ruff: noqa: RUF001

from __future__ import annotations

import asyncio
import hashlib
import logging
from pathlib import Path
from threading import Lock
from typing import Any

from neuroagent.agent.redaction import OutboundContextPolicy, OutboundPolicyError
from neuroagent.agent.secrets import SecretResolver
from neuroagent.application.errors import ApplicationError, InputValidationError
from neuroagent.retrieval.fmrianalysis.retriever import (
    LiteratureRetriever,
    _generate_query_variants,
    _reciprocal_rank_fusion,
)
from neuroagent.retrieval.interfaces import RetrievalResult, RetrievedChunk

logger = logging.getLogger(__name__)


class FmriAnalysisRagService:
    def __init__(
        self,
        *,
        db_dir: Path,
        collection: str,
        secret_resolver: SecretResolver,
        api_key_env: str,
        redaction_salt: str | None,
        rerank: bool = True,
    ) -> None:
        self._db_dir = db_dir
        self._collection = collection
        self._secrets = secret_resolver
        self._api_key_env = api_key_env
        self._salt = redaction_salt
        self._rerank = rerank
        self._lock = Lock()
        self._retriever: Any = None

    async def retrieve(
        self, query: str, *, limit: int = 8, filters: dict[str, Any] | None = None
    ) -> RetrievalResult:
        if filters and set(filters) - {"category"}:
            raise InputValidationError("rag_filter_unsupported", "当前检索仅支持 category 过滤。")
        if not self._salt:
            raise InputValidationError(
                "redaction_policy_not_configured", "文献检索外发前需要配置脱敏策略。"
            )
        try:
            safe = OutboundContextPolicy(self._salt).redact({"question": query})
        except OutboundPolicyError as exc:
            raise InputValidationError(
                "outbound_context_rejected", "检索问题未通过外发校验。"
            ) from exc
        return await asyncio.to_thread(
            self._retrieve, str(safe.payload["question"]), min(max(limit, 1), 10), filters or {}
        )

    def _retrieve(self, query: str, limit: int, filters: dict[str, Any]) -> RetrievalResult:
        with self._lock:
            if not (self._db_dir / "chroma.sqlite3").is_file():
                raise ApplicationError("rag_index_missing", "未找到已有文献索引。", status_code=503)
            key = self._secrets.resolve(self._api_key_env)
            if not key:
                raise ApplicationError(
                    "rag_key_missing", "未配置文献检索的 DashScope API Key。", status_code=503
                )
            try:
                if self._retriever is None:
                    self._retriever = LiteratureRetriever(
                        db_dir=self._db_dir,
                        collection_name=self._collection,
                        api_key=key,
                        similarity_threshold=float("-inf"),
                    )
                variants = _generate_query_variants(query)
                ranked = [
                    self._retriever.retrieve(
                        variant,
                        top_k=limit * 2,
                        filter_category=filters.get("category"),
                        include_scores=True,
                        use_rerank=False,
                        min_results=0,
                    )
                    for variant in variants
                ]
                fused = _reciprocal_rank_fusion(ranked, top_n=limit * 2)
            except Exception as exc:
                logger.warning("Literature retrieval failed type=%s", type(exc).__name__)
                raise ApplicationError(
                    "rag_unavailable",
                    "文献检索失败，请检查 RAG 依赖、索引和服务配置。",
                    status_code=503,
                ) from exc
            rerank_used = False
            if self._rerank and len(fused) > 1:
                try:
                    from neuroagent.retrieval.fmrianalysis.reranker import DashScopeReranker

                    fused = DashScopeReranker(api_key=key).rerank_with_texts(
                        query, fused, text_key="content", top_n=limit
                    )
                    rerank_used = True
                except Exception as exc:
                    logger.warning("Rerank fallback type=%s", type(exc).__name__)
            chunks = tuple(_to_chunk(item) for item in fused[:limit] if item.get("content"))
            return RetrievalResult(
                chunks=chunks,
                suggested_answer=(
                    "检索到以下论文片段：\n"
                    + "\n".join(
                        f"[C{i}] {chunk.title}: {chunk.text[:500]}"
                        for i, chunk in enumerate(chunks, 1)
                    )
                    if chunks
                    else "当前文献库没有检索到可用证据。"
                ),
                metadata={
                    "backend": "fmrianalysis",
                    "query_variants": variants,
                    "rerank_used": rerank_used,
                },
            )


def _to_chunk(item: dict[str, Any]) -> RetrievedChunk:
    source = str(item.get("source") or "legacy-literature").replace("\\", "/")
    source_name = source.rsplit("/", 1)[-1]
    text = str(item["content"])
    chunk_id = str(
        item.get("chunk_id") or hashlib.sha256((source + "\0" + text).encode()).hexdigest()
    )
    start, end = item.get("page_start"), item.get("page_end")
    valid_pages = type(start) is int and type(end) is int and 1 <= start <= end
    return RetrievedChunk(
        chunk_id=chunk_id,
        source=source_name,
        title=str(item.get("title") or item.get("data_id") or source_name),
        text=text,
        score=max(float(item.get("rrf_score", 0)), 0),
        paper_id=item.get("paper_id"),
        section=item.get("section") or item.get("h1") or None,
        subsection=item.get("subsection") or item.get("h2") or None,
        page_start=start if valid_pages else None,
        page_end=end if valid_pages else None,
        metadata={
            "backend": "fmrianalysis",
            "category": item.get("category"),
            "page_provenance": "verified_metadata" if valid_pages else "unavailable",
        },
    )
