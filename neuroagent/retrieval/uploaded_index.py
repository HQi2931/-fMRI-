"""Explicit upload indexing and candidates for the shared final reranker."""

from __future__ import annotations

from importlib import import_module
from pathlib import Path
from threading import Lock
from typing import Any

from neuroagent.agent.redaction import OutboundContextPolicy, OutboundPolicyError
from neuroagent.agent.secrets import SecretResolver
from neuroagent.application.errors import ApplicationError, InputValidationError
from neuroagent.literature.models import PaperIngestResult
from neuroagent.literature.ports import LiteratureRepository


class UploadedLiteratureIndex:
    def __init__(
        self,
        *,
        work_root: Path,
        repository: LiteratureRepository,
        secret_resolver: SecretResolver,
        api_key_env: str,
        redaction_salt: str | None,
    ) -> None:
        self._path = work_root / "literature_index"
        self._repository = repository
        self._secrets = secret_resolver
        self._api_key_env = api_key_env
        self._salt = redaction_salt
        self._collection: Any = None
        self._embeddings: Any = None
        self._lock = Lock()

    def _open(self) -> None:
        if self._collection is not None:
            return
        key = self._secrets.resolve(self._api_key_env)
        if not key:
            raise ApplicationError(
                "rag_key_missing", "未配置文献检索的 DashScope API Key。", status_code=503
            )
        chromadb = import_module("chromadb")

        from neuroagent.retrieval.fmrianalysis.dashscope_embeddings import DashScopeEmbeddings

        self._embeddings = DashScopeEmbeddings(api_key=key)
        self._collection = chromadb.PersistentClient(path=str(self._path)).get_or_create_collection(
            "uploaded_literature_v1", metadata={"hnsw:space": "cosine"}
        )

    def index(self, result: PaperIngestResult) -> None:
        if not self._salt:
            raise InputValidationError(
                "redaction_policy_not_configured", "文献索引外发前需要配置脱敏策略。"
            )
        try:
            policy = OutboundContextPolicy(self._salt)
            documents = [
                str(policy.redact({"text": c.text}).payload["text"]) for c in result.chunks
            ]
        except OutboundPolicyError as exc:
            raise InputValidationError(
                "outbound_context_rejected", "文献文本未通过外发校验。"
            ) from exc
        with self._lock:
            self._open()
            self._collection.upsert(
                ids=[c.chunk_id for c in result.chunks],
                documents=documents,
                embeddings=self._embeddings.embed_documents(documents),
                metadatas=[
                    {
                        "paper_id": c.paper_id,
                        "title": result.paper.title or "上传论文",
                        "section": c.section,
                        "subsection": c.subsection or "",
                        "page_start": c.page_start,
                        "page_end": c.page_end,
                        "source": "source.pdf",
                    }
                    for c in result.chunks
                ],
            )

    def ready_ids(self, paper_ids: list[str] | None = None) -> list[str]:
        if paper_ids:
            for paper_id in paper_ids:
                self._repository.get_literature(paper_id)
        return self._repository.list_ready_paper_ids(paper_ids)

    def candidates(
        self, query: str, *, limit: int, paper_ids: list[str] | None = None
    ) -> list[dict[str, Any]]:
        ready = self.ready_ids(paper_ids)
        if not ready:
            return []
        with self._lock:
            self._open()
            result = self._collection.query(
                query_embeddings=[self._embeddings.embed_query(query)],
                n_results=limit,
                where={"paper_id": {"$in": ready}},
                include=["documents", "metadatas"],
            )
        return [
            dict(metadata, chunk_id=chunk_id, content=text, rrf_score=1 / (60 + rank))
            for rank, (chunk_id, text, metadata) in enumerate(
                zip(result["ids"][0], result["documents"][0], result["metadatas"][0], strict=True),
                1,
            )
        ]
