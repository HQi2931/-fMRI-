from fastapi.testclient import TestClient

from neuroagent.api.app import create_app
from neuroagent.application.contracts import (
    ConversationCreate,
    ConversationMode,
    MemoryAction,
    MemoryCreate,
    MemoryKind,
    MemoryUpdate,
)
from neuroagent.memory.embeddings import MemoryEmbeddingError
from tests.backend.conftest import make_project


async def test_expired_pinned_memory_is_excluded_and_expiry_can_be_cleared(service):
    from datetime import UTC, datetime, timedelta

    conversation = service.create_conversation(
        ConversationCreate(mode=ConversationMode.CHAT), "expiry"
    )
    cid = conversation.conversation_id
    memory = service.repository.create_memory(
        cid,
        MemoryCreate(
            kind=MemoryKind.INSTRUCTION,
            key="temporary",
            content="temporary instruction",
            pinned=True,
            importance=0.9,
            expires_at=datetime.now(UTC) - timedelta(days=1),
        ),
    )
    assert memory.importance == 0.9
    assert not (await service.semantic_memory.recall(cid, "temporary")).matches
    assert not service.repository.store_memory_embedding(
        cid,
        memory.memory_id,
        expected_version=memory.version,
        model_identity="fixture",
        vector=(1.0,),
    )
    restored = service.repository.update_memory(
        cid,
        memory.memory_id,
        MemoryUpdate(
            action=MemoryAction.UPDATE,
            expected_version=memory.version,
            expires_at=None,
        ),
    )
    assert restored.expires_at is None
    assert (await service.semantic_memory.recall(cid, "temporary")).matches[
        0
    ].memory.memory_id == memory.memory_id


def test_embedding_write_is_version_bound_and_forgetting_purges_vector(service):
    conversation = service.create_conversation(
        ConversationCreate(mode=ConversationMode.CHAT),
        "vector-conversation",
    )
    cid = conversation.conversation_id
    memory = service.repository.create_memory(
        cid,
        MemoryCreate(
            kind=MemoryKind.INSTRUCTION,
            key="style",
            content="Keep answers concise",
        ),
    )
    assert service.repository.store_memory_embedding(
        cid,
        memory.memory_id,
        expected_version=memory.version,
        model_identity="fixture",
        vector=(1.0, 0.0),
    )
    assert memory.memory_id in service.repository.get_memory_embeddings(cid)
    updated = service.repository.update_memory(
        cid,
        memory.memory_id,
        MemoryUpdate(
            action=MemoryAction.UPDATE,
            expected_version=memory.version,
            content="Detailed answers",
        ),
    )
    assert service.repository.get_memory_embeddings(cid) == {}
    assert not service.repository.store_memory_embedding(
        cid,
        memory.memory_id,
        expected_version=memory.version,
        model_identity="fixture",
        vector=(1.0, 0.0),
    )
    assert service.repository.store_memory_embedding(
        cid,
        memory.memory_id,
        expected_version=updated.version,
        model_identity="fixture",
        vector=(0.0, 1.0),
    )


async def test_semantic_recall_reaches_chat_context_across_project_sessions(
    service,
    source_root,
    work_root,
):
    from neuroagent.application.contracts import MemoryScope

    project = make_project(service, source_root, work_root)
    first = service.create_conversation(
        ConversationCreate(mode=ConversationMode.CHAT, project_id=project.project_id),
        "first",
    )
    second = service.create_conversation(
        ConversationCreate(mode=ConversationMode.CHAT, project_id=project.project_id),
        "second",
    )
    memory = service.repository.create_memory(
        first.conversation_id,
        MemoryCreate(
            kind=MemoryKind.INSTRUCTION,
            key="style",
            content="Use concise answers",
            scope=MemoryScope.PROJECT,
            pinned=False,
        ),
    )
    repeated = service.repository.create_memory(
        second.conversation_id,
        MemoryCreate(
            kind=MemoryKind.INSTRUCTION,
            key="style",
            content="Use concise answers",
            scope=MemoryScope.PROJECT,
            pinned=False,
        ),
    )
    assert repeated.memory_id == memory.memory_id

    class Embeddings:
        identity = "semantic-fixture"

        async def embed_documents(self, texts):
            return tuple((1.0, 0.0) for text in texts)

        async def embed_query(self, text):
            return (0.99, 0.01)

    service.semantic_memory.embeddings = Embeddings()
    result = await service.semantic_memory.index(first.conversation_id)
    assert result["indexed"] == "1"
    assert (
        service.repository.get_conversation_context(first.conversation_id)
        .memories[0]
        .semantic_indexed
    )
    prepared = await service.conversation_context.prepare_chat(
        second,
        question="Keep it brief",
        preferred_profile_id=None,
        model=None,
    )
    assert prepared.memory_ids == (memory.memory_id,)
    assert prepared.pinned_context[0].value == "Use concise answers"
    assert prepared.memory_recall["matches"][0]["reason"] == "semantic"
    unrelated = service.create_conversation(ConversationCreate(mode=ConversationMode.CHAT), "other")
    assert not (await service.semantic_memory.recall(unrelated.conversation_id, "brief")).matches
    cid = first.conversation_id
    service.repository.update_memory(
        cid,
        memory.memory_id,
        MemoryUpdate(
            action=MemoryAction.FORGET,
            expected_version=memory.version,
        ),
    )
    assert service.repository.get_memory_embeddings(cid) == {}
    assert not service.repository.store_memory_embedding(
        cid,
        memory.memory_id,
        expected_version=memory.version,
        model_identity="fixture",
        vector=(0.0, 1.0),
    )


def test_memory_index_api_reports_fallback_and_safe_provider_failure(service):
    cid = service.create_conversation(
        ConversationCreate(mode=ConversationMode.CHAT), "index-api"
    ).conversation_id
    with TestClient(create_app(service=service)) as client:
        fallback = client.post(f"/api/v1/conversations/{cid}/context/index")
        assert fallback.status_code == 200
        assert fallback.json()["warning"] == "memory_embedding_not_configured"

        class FailingEmbeddings:
            identity = "failure"

            async def embed_documents(self, texts):
                raise MemoryEmbeddingError("safe_failure")

            async def embed_query(self, text):
                raise MemoryEmbeddingError("safe_failure")

        service.semantic_memory.embeddings = FailingEmbeddings()
        service.repository.create_memory(
            cid,
            MemoryCreate(kind=MemoryKind.INSTRUCTION, key="style", content="Be concise"),
        )
        failed = client.post(f"/api/v1/conversations/{cid}/context/index")
        assert failed.status_code == 503
        assert failed.json()["error"]["code"] == "safe_failure"
