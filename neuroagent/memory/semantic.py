"""Pure ranking policy for durable memories; persistence stays in adapters."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class MemoryCandidate:
    memory_id: str
    content: str
    conversation_id: str
    project_id: str | None
    scope: str
    status: str
    pinned: bool
    updated_at: datetime
    importance: float = 0.5
    expires_at: datetime | None = None
    superseded_by: str | None = None
    vector: tuple[float, ...] = ()
    embedding_model: str | None = None


@dataclass(frozen=True, slots=True)
class MemoryMatch:
    memory: MemoryCandidate
    score: float
    semantic_score: float | None
    lexical_score: float
    reason: str


def terms(text: str) -> set[str]:
    """English words and Chinese bigrams without an external tokenizer."""
    result = set(re.findall(r"[a-z0-9_]+", text.casefold()))
    for span in re.findall(r"[\u3400-\u9fff]+", text):
        result.update(span[i : i + 2] for i in range(max(1, len(span) - 1)))
    return result


def cosine(left: tuple[float, ...], right: tuple[float, ...]) -> float | None:
    if not left or len(left) != len(right):
        return None
    if not all(math.isfinite(item) for item in (*left, *right)):
        return None
    a = math.sqrt(sum(item * item for item in left))
    b = math.sqrt(sum(item * item for item in right))
    if not a or not b:
        return None
    return max(-1.0, min(1.0, sum(x * y for x, y in zip(left, right, strict=True)) / a / b))


class SemanticMemoryRanker:
    """Scope-filter first, then fuse ranks and apply importance/time weighting."""

    def rank(
        self,
        candidates: list[MemoryCandidate],
        *,
        query: str,
        conversation_id: str,
        project_id: str | None,
        now: datetime,
        query_vector: tuple[float, ...] = (),
        embedding_model: str | None = None,
        limit: int = 12,
        minimum_similarity: float = 0.45,
        half_life_days: float = 180,
    ) -> list[MemoryMatch]:
        if half_life_days <= 0 or limit < 1:
            raise ValueError("half-life and result limit must be positive")
        if not -1 <= minimum_similarity <= 1:
            raise ValueError("similarity threshold must be between -1 and 1")
        eligible = [
            item
            for item in candidates
            if item.status == "confirmed"
            and item.content.strip()
            and item.superseded_by is None
            and (item.expires_at is None or item.expires_at > now)
            and (
                (item.scope == "conversation" and item.conversation_id == conversation_id)
                or (
                    item.scope == "project"
                    and project_id is not None
                    and item.project_id == project_id
                )
            )
        ]
        query_terms = terms(query)
        lexical = {
            item.memory_id: len(query_terms & terms(item.content)) / max(1, len(query_terms))
            for item in eligible
        }
        semantic = {
            item.memory_id: cosine(query_vector, item.vector)
            if embedding_model is not None and item.embedding_model == embedding_model
            else None
            for item in eligible
        }
        lexical_order = sorted(
            (item for item in eligible if lexical[item.memory_id] > 0),
            key=lambda item: (-lexical[item.memory_id], item.memory_id),
        )
        semantic_order = sorted(
            (
                item
                for item in eligible
                if semantic[item.memory_id] is not None
                and float(semantic[item.memory_id] or 0) >= minimum_similarity
            ),
            key=lambda item: (-float(semantic[item.memory_id] or 0), item.memory_id),
        )
        fused: dict[str, float] = {}
        for ordered in (lexical_order, semantic_order):
            for position, item in enumerate(ordered, 1):
                fused[item.memory_id] = fused.get(item.memory_id, 0) + 1 / (60 + position)
        matches = []
        for item in eligible:
            if not item.pinned and item.memory_id not in fused:
                continue
            age = max(0, (now - item.updated_at).total_seconds() / 86400)
            decay = 0.5 ** (age / half_life_days)
            importance = min(1, max(0, item.importance))
            score = fused.get(item.memory_id, 0) * (0.5 + 0.5 * decay) * (1 + importance)
            matches.append(
                MemoryMatch(
                    memory=item,
                    score=score,
                    semantic_score=semantic[item.memory_id],
                    lexical_score=lexical[item.memory_id],
                    reason="pinned"
                    if item.pinned
                    else "semantic_and_lexical"
                    if item in semantic_order and lexical[item.memory_id] > 0
                    else "semantic"
                    if item in semantic_order
                    else "lexical",
                )
            )
        matches.sort(
            key=lambda match: (
                not match.memory.pinned,
                -match.score,
                match.memory.memory_id,
            )
        )
        return matches[:limit]
