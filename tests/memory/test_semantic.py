from dataclasses import replace
from datetime import UTC, datetime, timedelta

from neuroagent.memory.semantic import MemoryCandidate, SemanticMemoryRanker, cosine

NOW = datetime(2026, 9, 9, tzinfo=UTC)


def candidate(identifier: str = "memory", **changes):
    base = MemoryCandidate(
        memory_id=identifier,
        content="Use concise answers",
        conversation_id="older-chat",
        project_id="project-a",
        scope="project",
        status="confirmed",
        pinned=False,
        updated_at=NOW,
        vector=(1.0, 0.0),
        embedding_model="fixture-v1",
    )
    return replace(base, **changes)


def recall(items, **changes):
    arguments = dict(
        query="Keep it brief",
        conversation_id="new-chat",
        project_id="project-a",
        now=NOW,
        query_vector=(0.99, 0.01),
        embedding_model="fixture-v1",
    )
    arguments.update(changes)
    return SemanticMemoryRanker().rank(items, **arguments)


def test_synonym_without_shared_words_recalls_across_project_conversations():
    matches = recall([candidate()])
    assert len(matches) == 1
    assert matches[0].lexical_score == 0
    assert matches[0].reason == "semantic"
    assert matches[0].semantic_score > 0.99


def test_scope_and_lifecycle_are_enforced_before_vector_ranking():
    invisible = [
        candidate("private", scope="conversation"),
        candidate("other-project", project_id="project-b"),
        candidate("pending", status="pending"),
        candidate("forgotten", status="forgotten"),
        candidate("rejected", status="rejected"),
        candidate("expired", expires_at=NOW),
        candidate("replaced", superseded_by="replacement"),
    ]
    assert recall(invisible) == []
    assert recall([candidate()], project_id=None) == []


def test_unpinned_memories_require_relevance_and_pins_do_not_require_a_vector():
    assert recall([candidate(vector=(0.0, 1.0))]) == []
    assert recall([candidate(pinned=True, vector=())])[0].reason == "pinned"
    assert recall([candidate()], query_vector=(), query="concise")[0].reason == "lexical"
    assert recall([candidate()], embedding_model="different-version") == []


def test_recency_importance_and_limit_affect_ranking():
    older = candidate("a", updated_at=NOW - timedelta(days=720), importance=0)
    newer = candidate("b", importance=1)
    matches = recall([older, newer], limit=1)
    assert matches[0].memory.memory_id == "b"


def test_invalid_vectors_never_produce_semantic_hits():
    assert cosine((float("nan"),), (1.0,)) is None
    assert cosine((0.0,), (1.0,)) is None
    assert cosine((1.0,), (1.0, 0.0)) is None
