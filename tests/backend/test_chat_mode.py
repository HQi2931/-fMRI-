# ruff: noqa: RUF001

from __future__ import annotations

from fastapi.testclient import TestClient

from neuroagent.api.app import create_app
from neuroagent.application.services import NeuroAgentService


def create_chat(client: TestClient, key: str = "phase-one-chat-create") -> str:
    response = client.post(
        "/api/v1/conversations",
        json={"mode": "chat"},
        headers={"Idempotency-Key": key},
    )
    assert response.status_code == 201
    return response.json()["conversation_id"]


def test_chat_api_returns_stable_agent_metadata_and_persists_session(
    service: NeuroAgentService,
) -> None:
    with TestClient(create_app(service=service)) as client:
        session_id = create_chat(client)
        response = client.post(
            f"/api/v1/conversations/{session_id}/turns",
            json={"content": "ALFF 是什么？", "stream": False},
            headers={"Idempotency-Key": "phase-one-chat-turn"},
        )
        restored = client.get(f"/api/v1/conversations/{session_id}")

    assert response.status_code == 201
    body = response.json()
    assert body["assistant_message"]["payload"]["chat"]["session_id"] == session_id
    assert body["assistant_message"]["payload"]["chat"]["intent"] == "knowledge_query"
    assert body["assistant_message"]["payload"]["chat"]["metadata"]["streaming"] is False
    assert restored.status_code == 200
    assert [item["sequence"] for item in restored.json()["messages"]] == [1, 2, 3]


def test_chat_streaming_extension_fails_explicitly_until_implemented(
    service: NeuroAgentService,
) -> None:
    with TestClient(create_app(service=service)) as client:
        session_id = create_chat(client, "stream-chat-create")
        response = client.post(
            f"/api/v1/conversations/{session_id}/turns",
            json={"content": "解释 ReHo", "stream": True},
            headers={"Idempotency-Key": "stream-chat-turn"},
        )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "chat_streaming_not_implemented"


def test_chat_work_request_is_only_drafted_and_never_dispatched(
    service: NeuroAgentService,
) -> None:
    with TestClient(create_app(service=service)) as client:
        session_id = create_chat(client, "work-draft-chat-create")
        response = client.post(
            f"/api/v1/conversations/{session_id}/turns",
            json={"content": "按照这篇文献帮我计算数据的 ALFF，频段 0.01-0.08"},
            headers={"Idempotency-Key": "work-draft-chat-turn"},
        )

    assert response.status_code == 201
    body = response.json()
    draft = body["assistant_message"]["payload"]["chat"]["work_request"]
    assert draft["task"] == "calculate_alff"
    assert draft["parameters"]["frequency_band"] == [0.01, 0.08]
    assert body["assistant_message"]["payload"]["chat"]["metadata"]["work_dispatched"] is False
    assert body["conversation"]["active_run_id"] is None
    assert body["tool_call"]["tool_name"] == "work_request_draft"


def test_method_question_about_preprocessing_is_knowledge_query(
    service: NeuroAgentService,
) -> None:
    with TestClient(create_app(service=service)) as client:
        session_id = create_chat(client, "method-question-chat-create")
        response = client.post(
            f"/api/v1/conversations/{session_id}/turns",
            json={"content": "你知道怎么做 ALFF 的预处理吗？"},
            headers={"Idempotency-Key": "method-question-chat-turn"},
        )

    assert response.status_code == 201
    assert response.json()["assistant_message"]["payload"]["chat"]["intent"] == "knowledge_query"


def test_chat_unknown_session_returns_not_found(service: NeuroAgentService) -> None:
    with TestClient(create_app(service=service)) as client:
        response = client.post(
            "/api/v1/conversations/missing-session/turns",
            json={"content": "ALFF 是什么？"},
            headers={"Idempotency-Key": "missing-chat-session"},
        )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


async def test_context_reaches_gateway_once_and_is_redacted() -> None:
    import json
    from unittest.mock import AsyncMock

    from neuroagent.agent.gateway import ModelGateway
    from neuroagent.agent.models import ModelProfile, ProviderResponse
    from neuroagent.agent.redaction import OutboundContextPolicy
    from neuroagent.agent.router import ModelRouter
    from neuroagent.chat.services import GatewayLlmClient
    from neuroagent.context.interfaces import ContextMessage, ContextPacket, PinnedContext
    from neuroagent.retrieval.interfaces import RetrievedChunk

    profile = ModelProfile(
        id="context",
        provider="mock",
        model="chosen-model",
        base_url="https://example.test",
        api_key_env="TEST_KEY",
    )
    provider = AsyncMock()
    provider.generate.return_value = ProviderResponse(content="方法解释 [C1]", model="chosen-model")

    class Secrets:
        def resolve(self, name):
            return "offline-test-key"

    gateway = ModelGateway(
        ModelRouter([profile], {}),
        {"mock": provider},
        OutboundContextPolicy("test-context-redaction-salt"),
        Secrets(),
    )
    llm = GatewayLlmClient(generate=gateway.generate_chat, has_profiles=lambda: True)
    await llm.generate(
        ContextPacket(
            question="那它有什么区别？",
            recent_messages=(
                ContextMessage(role="user", content="讨论 ALFF 与 fALFF，联系 a@example.org"),
            ),
            pinned_context=(PinnedContext(key="topic", value="静息态方法"),),
            conversation_summary="此前讨论低频振幅",
            retrieval_context=(
                RetrievedChunk(
                    chunk_id="c", score=1, title="Methods", source="paper", text="ALFF evidence"
                ),
            ),
        ),
        preferred_profile_id="context",
        model="chosen-model",
        allow_remote_search=False,
    )
    call = provider.generate.call_args
    payload = json.loads(call.args[2][1]["content"])
    assert payload["question"] == "那它有什么区别？"
    assert "ALFF" in payload["recent_messages"][0]["content"]
    assert "a@example.org" not in call.args[2][1]["content"]
    assert payload["pinned_context"][0]["value"] == "静息态方法"
    assert payload["conversation_summary"] == "此前讨论低频振幅"
    assert payload["local_evidence"][0]["citation_id"] == "C1"
    assert call.args[2][1]["content"].count("那它有什么区别？") == 1


def test_followup_retrieval_and_per_message_citations_persist(service: NeuroAgentService) -> None:
    from unittest.mock import AsyncMock, Mock

    from neuroagent.chat.interfaces import LlmResult
    from neuroagent.chat.services import ModelIntentRouter
    from neuroagent.retrieval.interfaces import RetrievalResult, RetrievedChunk

    classifier = AsyncMock(
        return_value='{"intent":"knowledge_query","query":"ALFF 与 fALFF 的区别"}'
    )
    service.chat_agent._intent_router = ModelIntentRouter(classifier, has_profiles=lambda: True)
    rag = AsyncMock()
    rag.retrieve.return_value = RetrievalResult(
        chunks=(
            RetrievedChunk(
                score=1,
                chunk_id="chunk-1",
                paper_id="paper-1",
                source="paper",
                title="Methods",
                text="Evidence",
                page_start=2,
                page_end=2,
            ),
        )
    )
    service.chat_agent._rag_service = rag
    llm = Mock()
    llm.available.return_value = True
    llm.generate = AsyncMock(
        return_value=LlmResult(
            content="解释 [C1] 与无效引用 [C99]",
            model="test",
            profile_id="test",
            context_hash="hash",
        )
    )
    service.chat_agent._llm_client = llm
    with TestClient(create_app(service=service)) as client:
        session = create_chat(client, "followup-session")
        for index, question in enumerate(("ALFF 是什么？", "那它与 fALFF 呢？")):
            response = client.post(
                f"/api/v1/conversations/{session}/turns",
                json={"content": question, "paper_ids": ["paper-1"], "model": "chosen"},
                headers={"Idempotency-Key": f"followup-{index}"},
            )
            assert response.status_code == 201
        stored = client.get(f"/api/v1/conversations/{session}").json()
        calls_before_greeting = rag.retrieve.call_count
        client.post(
            f"/api/v1/conversations/{session}/turns",
            json={"content": "你好"},
            headers={"Idempotency-Key": "greeting"},
        )
    assert rag.retrieve.call_count == calls_before_greeting
    assert classifier.call_count == 2
    assert "ALFF 是什么？" in [m.content for m in classifier.call_args.args[0].recent_messages]
    assert classifier.call_args.args[0].model == "chosen"
    rag.retrieve.assert_awaited_with("ALFF 与 fALFF 的区别", filters={"paper_ids": ["paper-1"]})
    answers = [
        message
        for message in stored["messages"]
        if message["role"] == "assistant" and "chat" in message["payload"]
    ]
    for message in answers:
        assert "[C99]" not in message["content"]
        assert "引用不完整" in message["content"]
        assert [c["citation_id"] for c in message["payload"]["chat"]["citations"]] == ["C1"]
        assert message["payload"]["chat"]["citations"][0]["page_start"] == 2
