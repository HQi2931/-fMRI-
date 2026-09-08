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
