"""Replaceable retrieval contracts for Chat Mode.

These protocols keep infrastructure choices outside ChatAgent, including the
optional fMRIAnalysis DashScope/Chroma adapter.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field


class RetrievedChunk(BaseModel):
    """Evidence returned by a retriever, with citation provenance attached."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    chunk_id: str = Field(min_length=1)
    source: str = Field(min_length=1)
    title: str = Field(min_length=1)
    text: str = Field(min_length=1)
    score: float = Field(ge=0)
    paper_id: str | None = None
    section: str | None = None
    subsection: str | None = None
    page_start: int | None = Field(default=None, ge=1)
    page_end: int | None = Field(default=None, ge=1)
    metadata: dict[str, Any] = Field(default_factory=dict)


class RetrievalResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    in_scope: bool = True
    chunks: tuple[RetrievedChunk, ...] = ()
    suggested_answer: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class EmbeddingProvider(Protocol):
    async def embed_documents(self, texts: Sequence[str]) -> Sequence[Sequence[float]]: ...

    async def embed_query(self, text: str) -> Sequence[float]: ...


class VectorStore(Protocol):
    async def upsert(
        self,
        chunks: Sequence[RetrievedChunk],
        vectors: Sequence[Sequence[float]],
    ) -> None: ...

    async def delete(self, *, paper_id: str) -> None: ...

    async def query(
        self,
        vector: Sequence[float],
        *,
        limit: int,
        filters: dict[str, Any] | None = None,
    ) -> Sequence[RetrievedChunk]: ...


class Retriever(Protocol):
    async def retrieve(
        self,
        query: str,
        *,
        limit: int = 8,
        filters: dict[str, Any] | None = None,
    ) -> Sequence[RetrievedChunk]: ...


class Reranker(Protocol):
    async def rerank(
        self, query: str, candidates: Sequence[RetrievedChunk], *, limit: int
    ) -> Sequence[RetrievedChunk]: ...


class RagService(Protocol):
    async def retrieve(
        self,
        query: str,
        *,
        limit: int = 8,
        filters: dict[str, Any] | None = None,
    ) -> RetrievalResult: ...
