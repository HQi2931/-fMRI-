"""Durable semantic recall with an explicit lexical fallback."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from neuroagent.application.contracts import MemoryStatus
from neuroagent.application.ports import RepositoryPort
from neuroagent.memory.embeddings import MemoryEmbeddingError, MemoryEmbeddings
from neuroagent.memory.semantic import MemoryCandidate, MemoryMatch, SemanticMemoryRanker


@dataclass(frozen=True, slots=True)
class RecallResult:
    matches: tuple[MemoryMatch, ...]
    backend: str
    warning: str | None = None


class SemanticMemoryService:
    def __init__(
        self,
        repository: RepositoryPort,
        embeddings: MemoryEmbeddings | None = None,
        *,
        limit: int = 12,
        half_life_days: float = 180,
        minimum_similarity: float = 0.45,
    ) -> None:
        self.repository = repository
        self.embeddings = embeddings
        self.limit = limit
        self.half_life_days = half_life_days
        self.minimum_similarity = minimum_similarity
        self.ranker = SemanticMemoryRanker()

    async def index(self, conversation_id: str) -> dict[str, str]:
        """Explicit retryable indexing; no write transaction spans the provider call."""
        state = self.repository.get_conversation_context(conversation_id)
        if self.embeddings is None:
            return {"backend": "lexical", "warning": "memory_embedding_not_configured"}
        stored = self.repository.get_memory_embeddings(conversation_id)
        pending = [
            item
            for item in state.memories
            if item.status is MemoryStatus.CONFIRMED
            and (item.expires_at is None or item.expires_at > datetime.now(UTC))
            and (
                item.memory_id not in stored
                or stored[item.memory_id][1] != self.embeddings.identity
            )
        ]
        indexed = stale = 0
        # Bound each provider request independently; a retry reuses completed rows.
        for start in range(0, len(pending), 16):
            batch = pending[start : start + 16]
            vectors = await self.embeddings.embed_documents([item.content for item in batch])
            for item, vector in zip(batch, vectors, strict=True):
                saved = self.repository.store_memory_embedding(
                    conversation_id,
                    item.memory_id,
                    expected_version=item.version,
                    model_identity=self.embeddings.identity,
                    vector=vector,
                )
                indexed += int(saved)
                stale += int(not saved)
        return {"backend": "semantic", "indexed": str(indexed), "stale": str(stale)}

    async def recall(self, conversation_id: str, question: str) -> RecallResult:
        conversation = self.repository.get_conversation(conversation_id)
        query_vector: tuple[float, ...] = ()
        identity = None
        warning = None
        if self.embeddings is not None:
            try:
                query_vector = await self.embeddings.embed_query(question)
                identity = self.embeddings.identity
            except MemoryEmbeddingError as exc:
                warning = str(exc)
        else:
            warning = "memory_embedding_not_configured"
        # Re-read after network I/O so mutations during the request take effect.
        state = self.repository.get_conversation_context(conversation_id)
        vectors = self.repository.get_memory_embeddings(conversation_id)
        candidates = []
        for item in state.memories:
            version, model, vector = vectors.get(item.memory_id, (0, "", ()))
            candidates.append(
                MemoryCandidate(
                    memory_id=item.memory_id,
                    content=item.content,
                    conversation_id=item.conversation_id,
                    project_id=item.project_id,
                    scope=item.scope.value,
                    status=item.status.value,
                    pinned=item.pinned,
                    updated_at=item.updated_at,
                    importance=item.importance,
                    expires_at=item.expires_at,
                    vector=vector if version == item.version else (),
                    embedding_model=model,
                )
            )
        matches = self.ranker.rank(
            candidates,
            query=question,
            conversation_id=conversation_id,
            project_id=conversation.project_id,
            now=datetime.now(UTC),
            query_vector=query_vector,
            embedding_model=identity,
            limit=self.limit,
            half_life_days=self.half_life_days,
            minimum_similarity=self.minimum_similarity,
        )
        return RecallResult(tuple(matches), "semantic" if query_vector else "lexical", warning)
