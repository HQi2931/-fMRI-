"""Request-scoped evidence collected by Chat tools.

Citations must be built from typed chunks that a retriever actually returned, so
tools record what they retrieved here instead of trusting model prose.  The
collector assigns ``C<n>`` identifiers in insertion order, which is exactly the
order ``EvidenceCitationService`` enumerates, so an identifier the model sees in
a tool result is the same identifier the final answer cites.
"""

from __future__ import annotations

from collections.abc import Sequence

from neuroagent.retrieval.interfaces import RetrievedChunk

DEFAULT_EVIDENCE_LIMIT = 24


class EvidenceCollector:
    def __init__(self, *, limit: int = DEFAULT_EVIDENCE_LIMIT) -> None:
        if limit < 1:
            raise ValueError("limit must be at least 1")
        self._limit = limit
        self._order: list[str] = []
        self._chunks: dict[str, RetrievedChunk] = {}

    def add(self, chunks: Sequence[RetrievedChunk]) -> tuple[tuple[str, RetrievedChunk], ...]:
        """Store previously unseen chunks and return their citation pairs."""

        assigned: list[tuple[str, RetrievedChunk]] = []
        for chunk in chunks:
            stored = self._chunks.get(chunk.chunk_id)
            if stored is not None:
                assigned.append((self.citation_id(chunk.chunk_id), stored))
                continue
            if len(self._order) >= self._limit:
                continue
            self._order.append(chunk.chunk_id)
            self._chunks[chunk.chunk_id] = chunk
            assigned.append((self.citation_id(chunk.chunk_id), chunk))
        return tuple(assigned)

    def citation_id(self, chunk_id: str) -> str:
        return f"C{self._order.index(chunk_id) + 1}"

    @property
    def chunks(self) -> tuple[RetrievedChunk, ...]:
        return tuple(self._chunks[chunk_id] for chunk_id in self._order)

    def __len__(self) -> int:
        return len(self._order)
