import pytest

from neuroagent.agent.gateway import ChatGatewayResult
from neuroagent.agent.models import ProviderResponse
from neuroagent.application.contracts import (
    ConversationCreate,
    ConversationMode,
    MemoryAction,
    MemoryCreate,
    MemoryKind,
    MemoryStatus,
    MemoryUpdate,
)
from neuroagent.application.errors import ApplicationError, ConflictError, InputValidationError


def test_duplicate_manual_memory_is_idempotent_and_conflict_is_explicit(service):
    cid = service.create_conversation(
        ConversationCreate(mode=ConversationMode.CHAT), "duplicate"
    ).conversation_id
    request = MemoryCreate(kind=MemoryKind.INSTRUCTION, key="style", content="Brief answers")
    first = service.repository.create_memory(cid, request)
    second = service.repository.create_memory(cid, request)
    assert second.memory_id == first.memory_id
    with pytest.raises(ConflictError) as error:
        service.repository.create_memory(
            cid, request.model_copy(update={"content": "Detailed answers"})
        )
    assert error.value.code == "memory_key_conflict"
    assert service.repository.get_conversation_context(cid).memories[0].content == "Brief answers"


def test_invalid_source_is_rejected(service):
    conversation = service.create_conversation(
        ConversationCreate(mode=ConversationMode.CHAT), "source"
    )
    # The welcome assistant message cannot serve as evidence of a user preference.
    with pytest.raises(InputValidationError):
        service.repository.create_memory(
            conversation.conversation_id,
            MemoryCreate(
                kind=MemoryKind.PREFERENCE,
                key="style",
                content="Brief answers",
                source_message_id=conversation.messages[0].message_id,
            ),
        )


@pytest.mark.parametrize("action", [MemoryAction.FORGET, MemoryAction.REJECT])
def test_automatic_extraction_cannot_resurrect_user_removed_memory(service, action):
    cid = service.create_conversation(
        ConversationCreate(mode=ConversationMode.CHAT), "removed"
    ).conversation_id
    _stored, source, _assistant, _tool = service.repository.append_conversation_exchange(
        cid,
        user_content="Please keep answers brief",
        assistant_content="Okay",
        assistant_payload={},
        tool=None,
        workspace_path=None,
        preferred_profile_id=None,
        project_id=None,
        active_run_id=None,
    )
    args = dict(
        kind=MemoryKind.PREFERENCE,
        key="style",
        content="Brief answers",
        status=MemoryStatus.CONFIRMED,
        pinned=True,
        confidence=1,
        source_message_id=source.message_id,
    )
    memory = service.repository.upsert_memory_candidate(cid, **args)
    removed = service.repository.update_memory(
        cid,
        memory.memory_id,
        MemoryUpdate(
            action=action,
            expected_version=memory.version,
        ),
    )
    repeated = service.repository.upsert_memory_candidate(cid, **args)
    assert repeated.status == removed.status
    assert repeated.version == removed.version
    assert service.repository.get_conversation_context(cid).memories == []


async def test_model_candidates_are_source_bound_pending_and_invalid_json_falls_back(service):
    cid = service.create_conversation(
        ConversationCreate(mode=ConversationMode.CHAT), "extract"
    ).conversation_id
    _stored, source, _assistant, _tool = service.repository.append_conversation_exchange(
        cid,
        user_content="Remember that I prefer concise tables",
        assistant_content="Okay",
        assistant_payload={},
        tool=None,
        workspace_path=None,
        preferred_profile_id=None,
        project_id=None,
        active_run_id=None,
    )

    async def valid_generator(**_kwargs):
        return ChatGatewayResult(
            response=ProviderResponse(
                content=(
                    '{"candidates":[{"kind":"preference","key":"format",'
                    '"content":"Use concise tables","scope":"project",'
                    '"confidence":0.9,"importance":0.8}]}'
                ),
                model="fixture",
            ),
            selected_profile_id="fixture",
            context_hash="hash",
            attempted_profile_ids=("fixture",),
            remote_search_used=False,
            redaction_count=0,
        )

    service.conversation_context._summary_generator = valid_generator
    candidates, metadata = await service.conversation_context._extract_memory_candidates(
        source.content,
        preferred_profile_id="fixture",
        model=None,
        project_bound=False,
    )
    assert metadata["model_used"] is True
    assert candidates[0].scope.value == "conversation"
    assert candidates[0].status is MemoryStatus.PENDING
    memory_ids = service.conversation_context.persist_memory_candidates(
        cid, candidates, source.message_id
    )
    memory = service.repository.get_conversation_context(cid).memories[0]
    assert memory.memory_id in memory_ids
    assert memory.source_message_id == source.message_id
    assert memory.status is MemoryStatus.PENDING

    async def invalid_generator(**_kwargs):
        return ChatGatewayResult(
            response=ProviderResponse(content="not-json", model="fixture"),
            selected_profile_id="fixture",
            context_hash="hash",
            attempted_profile_ids=("fixture",),
            remote_search_used=False,
            redaction_count=0,
        )

    service.conversation_context._summary_generator = invalid_generator
    candidates, metadata = await service.conversation_context._extract_memory_candidates(
        "Remember this arbitrary durable fact",
        preferred_profile_id="fixture",
        model=None,
        project_bound=False,
    )
    assert candidates == ()
    assert metadata["warning"] == "memory_extraction_unavailable"


def test_conflicting_candidate_requires_explicit_resolution(service):
    cid = service.create_conversation(
        ConversationCreate(mode=ConversationMode.CHAT), "proposal"
    ).conversation_id
    _stored, source, _assistant, _tool = service.repository.append_conversation_exchange(
        cid,
        user_content="Keep answers concise",
        assistant_content="Okay",
        assistant_payload={},
        tool=None,
        workspace_path=None,
        preferred_profile_id=None,
        project_id=None,
        active_run_id=None,
    )
    memory = service.repository.upsert_memory_candidate(
        cid,
        kind=MemoryKind.PREFERENCE,
        key="length",
        content="Concise answers",
        status=MemoryStatus.CONFIRMED,
        pinned=False,
        confidence=1,
        source_message_id=source.message_id,
    )
    service.repository.store_memory_embedding(
        cid,
        memory.memory_id,
        expected_version=memory.version,
        model_identity="fixture",
        vector=(1.0, 0.0),
    )
    _stored, changed, _assistant, _tool = service.repository.append_conversation_exchange(
        cid,
        user_content="I now prefer detailed answers",
        assistant_content="Okay",
        assistant_payload={},
        tool=None,
        workspace_path=None,
        preferred_profile_id=None,
        project_id=None,
        active_run_id=None,
    )
    proposed = service.repository.upsert_memory_candidate(
        cid,
        kind=MemoryKind.PREFERENCE,
        key="length",
        content="Detailed answers",
        status=MemoryStatus.PENDING,
        pinned=False,
        confidence=0.9,
        source_message_id=changed.message_id,
    )
    assert proposed.content == "Concise answers"
    assert proposed.proposed_content == "Detailed answers"
    assert service.repository.get_memory_embeddings(cid)[memory.memory_id][0] == proposed.version
    rejected = service.repository.update_memory(
        cid,
        memory.memory_id,
        MemoryUpdate(
            action=MemoryAction.REJECT_PROPOSAL,
            expected_version=proposed.version,
        ),
    )
    assert rejected.content == "Concise answers"
    assert rejected.proposed_content is None


def test_forget_removes_summary_effect_but_retains_raw_history(service):
    cid = service.create_conversation(
        ConversationCreate(mode=ConversationMode.CHAT), "forget-summary"
    ).conversation_id
    _stored, source, _assistant, _tool = service.repository.append_conversation_exchange(
        cid,
        user_content="Remember my private preference",
        assistant_content="Okay",
        assistant_payload={},
        tool=None,
        workspace_path=None,
        preferred_profile_id=None,
        project_id=None,
        active_run_id=None,
    )
    memory = service.repository.create_memory(
        cid,
        MemoryCreate(
            kind=MemoryKind.PREFERENCE,
            key="private",
            content="Private preference",
            source_message_id=source.message_id,
        ),
    )
    service.repository.create_context_summary(
        cid,
        content="The user has a private preference",
        covered_sequence=source.sequence,
        source_hash="a" * 64,
        method="extractive",
    )
    service.repository.update_memory(
        cid,
        memory.memory_id,
        MemoryUpdate(action=MemoryAction.FORGET, expected_version=memory.version),
    )
    assert service.repository.get_conversation_context(cid).summary is None
    raw = service.repository.get_conversation(cid)
    assert any(message.message_id == source.message_id for message in raw.messages)
    assert all(
        message.message_id != source.message_id
        for message in service.conversation_context.messages_from(raw)
    )


@pytest.mark.parametrize(
    ("action", "merged_content", "expected"),
    [
        (MemoryAction.ACCEPT_PROPOSAL, None, "Detailed answers"),
        (
            MemoryAction.MERGE_PROPOSAL,
            "Concise with optional detail",
            "Concise with optional detail",
        ),
    ],
)
def test_conflict_can_be_explicitly_accepted_or_merged(service, action, merged_content, expected):
    cid = service.create_conversation(
        ConversationCreate(mode=ConversationMode.CHAT), f"resolve-{action.value}"
    ).conversation_id
    sources = []
    for content in ("Concise answers", "Detailed answers"):
        _stored, source, _assistant, _tool = service.repository.append_conversation_exchange(
            cid,
            user_content=content,
            assistant_content="Okay",
            assistant_payload={},
            tool=None,
            workspace_path=None,
            preferred_profile_id=None,
            project_id=None,
            active_run_id=None,
        )
        sources.append(source)
    memory = service.repository.upsert_memory_candidate(
        cid,
        kind=MemoryKind.PREFERENCE,
        key="length",
        content="Concise answers",
        status=MemoryStatus.CONFIRMED,
        pinned=False,
        confidence=1,
        source_message_id=sources[0].message_id,
    )
    proposed = service.repository.upsert_memory_candidate(
        cid,
        kind=MemoryKind.PREFERENCE,
        key="length",
        content="Detailed answers",
        status=MemoryStatus.PENDING,
        pinned=False,
        confidence=0.9,
        source_message_id=sources[1].message_id,
    )
    resolved = service.repository.update_memory(
        cid,
        memory.memory_id,
        MemoryUpdate(
            action=action,
            expected_version=proposed.version,
            content=merged_content,
        ),
    )
    assert resolved.content == expected
    assert resolved.source_message_id == sources[1].message_id
    assert resolved.proposed_content is None


def test_metadata_only_updates_keep_embedding_and_missing_proposals_are_rejected(service):
    cid = service.create_conversation(
        ConversationCreate(mode=ConversationMode.CHAT), "metadata"
    ).conversation_id
    memory = service.repository.create_memory(
        cid,
        MemoryCreate(kind=MemoryKind.DECISION, key="method", content="Use ALFF"),
    )
    assert service.repository.store_memory_embedding(
        cid,
        memory.memory_id,
        expected_version=memory.version,
        model_identity="fixture",
        vector=(1.0, 0.0),
    )
    unpinned = service.repository.update_memory(
        cid,
        memory.memory_id,
        MemoryUpdate(action=MemoryAction.UNPIN, expected_version=memory.version),
    )
    assert service.repository.get_memory_embeddings(cid)[memory.memory_id][0] == unpinned.version
    important = service.repository.update_memory(
        cid,
        memory.memory_id,
        MemoryUpdate(
            action=MemoryAction.UPDATE,
            expected_version=unpinned.version,
            importance=0.9,
        ),
    )
    assert important.importance == 0.9
    assert service.repository.get_memory_embeddings(cid)[memory.memory_id][0] == important.version
    with pytest.raises(InputValidationError, match="proposal"):
        service.repository.update_memory(
            cid,
            memory.memory_id,
            MemoryUpdate(
                action=MemoryAction.ACCEPT_PROPOSAL,
                expected_version=important.version,
            ),
        )


@pytest.mark.parametrize("model_succeeds", [True, False])
async def test_long_conversation_summary_uses_model_or_safe_fallback(service, model_succeeds):
    cid = service.create_conversation(
        ConversationCreate(mode=ConversationMode.CHAT), f"summary-{model_succeeds}"
    ).conversation_id
    for index in range(12):
        service.repository.append_conversation_exchange(
            cid,
            user_content=f"Question {index}",
            assistant_content=f"Answer {index}",
            assistant_payload={},
            tool=None,
            workspace_path=None,
            preferred_profile_id=None,
            project_id=None,
            active_run_id=None,
        )

    async def generator(**_kwargs):
        if not model_succeeds:
            raise ApplicationError("unavailable", "unavailable", status_code=503)
        return ChatGatewayResult(
            response=ProviderResponse(
                content='{"summary":"Durable conversation summary"}', model="fixture"
            ),
            selected_profile_id="fixture",
            context_hash="hash",
            attempted_profile_ids=("fixture",),
            remote_search_used=False,
            redaction_count=0,
        )

    service.conversation_context._summary_generator = generator
    prepared = await service.conversation_context.prepare_chat(
        service.repository.get_conversation(cid),
        question="neutral query",
        preferred_profile_id=None,
        model=None,
    )
    assert prepared.summary is not None
    assert prepared.summary.method == ("model" if model_succeeds else "extractive")
    assert prepared.summary.covered_sequence > 0
    assert len(prepared.recent_messages) == 12
