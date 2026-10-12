"""Conversation SSE completion, idempotent replay, and disconnect recovery."""

# ruff: noqa: RUF001

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request

from neuroagent.agent.models import ModelCapability, ModelProfile, ProviderResponse, ToolCall
from neuroagent.agent.providers import MockProvider
from neuroagent.api.app import create_app
from neuroagent.api.conversation_stream import conversation_stream_response
from neuroagent.application.contracts.conversations import (
    ConversationCreate,
    ConversationTurnCreate,
)
from neuroagent.application.errors import InputValidationError
from neuroagent.application.services import ApplicationServices
from neuroagent.bootstrap import build_service


@pytest.fixture
def streaming_service(service: ApplicationServices) -> Iterator[ApplicationServices]:
    configured = build_service(
        service.settings.model_copy(update={"redaction_salt": "synthetic-streaming-redaction-salt"})
    )
    try:
        yield configured
    finally:
        configured.close()


def configure(
    service: ApplicationServices, monkeypatch: pytest.MonkeyPatch, answer: str
) -> MockProvider:
    service.settings = service.settings.model_copy(
        update={"redaction_salt": "synthetic-streaming-redaction-salt"}
    )
    monkeypatch.setenv("STREAM_TEST_API_KEY", "synthetic-secret")
    service.repository.create_model_profile(
        ModelProfile(
            id="stream-test",
            provider="openai-compatible",
            base_url="https://example.org/v1",
            model="stream-model",
            api_key_env="STREAM_TEST_API_KEY",
            context_window_tokens=32768,
            capabilities=frozenset({ModelCapability.STREAMING}),
        )
    )

    class Provider(MockProvider):
        async def generate(self, profile, api_key, messages, **kwargs):
            system = messages[0]["content"]
            if "Extract only" in system:
                return ProviderResponse(content='{"candidates":[]}', model=profile.model)
            if "独立检索问题" in system:
                return ProviderResponse(
                    content='{"intent":"conversation","query":"hello","work_request":null}',
                    model=profile.model,
                )
            return await super().generate(profile, api_key, messages, **kwargs)

    provider = Provider([answer])
    service.providers = {"openai-compatible": provider}
    return provider


def decode_frames(text: str) -> list[tuple[str, dict[str, Any]]]:
    frames = []
    for frame in text.split("\n\n"):
        lines = frame.splitlines()
        event = next((line[7:] for line in lines if line.startswith("event: ")), None)
        data = next((line[6:] for line in lines if line.startswith("data: ")), None)
        if event and data:
            frames.append((event, json.loads(data)))
    return frames


@pytest.mark.parametrize("mode", ["chat", "work"])
def test_stream_final_is_durable_and_same_key_replays_without_provider(
    service: ApplicationServices,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    answer = "你好，这是逐步到达的中文回答。"
    raw = (
        json.dumps(
            {"answer": answer, "action": "conversation", "draft_patch": {}}, ensure_ascii=False
        )
        if mode == "work"
        else answer
    )
    provider = configure(service, monkeypatch, raw)
    with TestClient(create_app(service=service)) as client:
        conversation = client.post(
            "/api/v1/conversations",
            json={"mode": mode},
            headers={"Idempotency-Key": "create-stream"},
        ).json()
        url = f"/api/v1/conversations/{conversation['conversation_id']}/turns"
        body = {"content": "你好", "stream": True, "preferred_profile_id": "stream-test"}
        response = client.post(url, json=body, headers={"Idempotency-Key": "stream-key"})
        assert response.status_code == 201, response.text
        frames = decode_frames(response.text)
        deltas = [data["text"] for event, data in frames if event == "delta"]
        if mode == "chat":
            assert len(deltas) >= 2
            assert "".join(deltas) == answer
        else:
            assert deltas == []
            assert any(
                event == "status" and data["message"] == "正在整理方案与回答…"
                for event, data in frames
            )
        assert frames[-1][0] == "final", frames
        final = frames[-1][1]
        assert final["assistant_message"]["content"] == answer
        stored = client.get(f"/api/v1/conversations/{conversation['conversation_id']}").json()
        assert stored == final["conversation"]
        replay = client.post(url, json=body, headers={"Idempotency-Key": "stream-key"})
        assert decode_frames(replay.text)[-1] == ("final", final)
        assert len(provider.requests) == 1
        assert len(stored["messages"]) == 3
        if mode == "work":
            assert "draft_patch" not in "".join(deltas)
        else:
            assert final["assistant_message"]["payload"]["chat"]["metadata"]["streaming"] is True


def test_stream_tool_loop_keeps_durable_steps_and_only_previews_answer(
    streaming_service: ApplicationServices,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = streaming_service
    provider = configure(
        service, monkeypatch, '{"answer":"校正方法需要核验能力。","action":"conversation"}'
    )
    selected = service.repository.get_model_profile("stream-test").profile
    service.repository.create_model_profile(
        selected.model_copy(
            update={
                "capabilities": frozenset({ModelCapability.STREAMING, ModelCapability.TOOL_USE})
            }
        )
    )
    provider._responses.insert(
        0,
        ProviderResponse(
            content="",
            model="stream-model",
            tool_calls=(
                ToolCall(
                    call_id="lookup-1",
                    name="describe_statistical_capabilities",
                    arguments={},
                ),
            ),
            finish_reason="tool_calls",
        ),
    )
    with TestClient(create_app(service=service)) as client:
        conversation = client.post(
            "/api/v1/conversations",
            json={"mode": "work"},
            headers={"Idempotency-Key": "create-loop"},
        ).json()
        response = client.post(
            f"/api/v1/conversations/{conversation['conversation_id']}/turns",
            json={"content": "解释校正能力", "stream": True},
            headers={"Idempotency-Key": "stream-loop"},
        )
    frames = decode_frames(response.text)
    assert frames[-1][0] == "final", frames
    assert not any(event == "delta" for event, _ in frames)
    assert frames[-1][1]["assistant_message"]["content"] == "校正方法需要核验能力。"
    assert any(
        event == "status" and data["message"] == "正在检查任务信息…" for event, data in frames
    )
    assert frames[-1][1]["tool_calls"][0]["tool_name"] == "describe_statistical_capabilities"
    assert frames[-1][1]["tool_calls"][0]["status"] == "succeeded"
    assert len(provider.requests) == 2


def test_stream_does_not_authorize_a_model_selected_execution_action(
    service: ApplicationServices,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from unittest.mock import Mock

    configure(service, monkeypatch, '{"answer":"模型声称已启动。","action":"start_preprocessing"}')
    execute = Mock(side_effect=AssertionError("model cannot dispatch execution"))
    monkeypatch.setattr(service.conversation_work, "execute", execute)
    with TestClient(create_app(service=service)) as client:
        conversation = client.post(
            "/api/v1/conversations",
            json={"mode": "work"},
            headers={"Idempotency-Key": "create-invalid"},
        ).json()
        response = client.post(
            f"/api/v1/conversations/{conversation['conversation_id']}/turns",
            json={"content": "你好", "stream": True},
            headers={"Idempotency-Key": "stream-invalid"},
        )
    frames = decode_frames(response.text)
    assert frames[-1][0] == "final"
    assert frames[-1][1]["assistant_message"]["content"] != "模型声称已启动。"
    assert "模型声称已启动。" not in response.text
    assert frames[-1][1]["conversation"]["active_run_id"] is None
    execute.assert_not_called()


def test_stream_error_has_stable_envelope_and_no_final(
    service: ApplicationServices,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fail(*args: object) -> None:
        raise InputValidationError("synthetic_error", "合成输入错误。")

    conversation = service.conversations.create_conversation(
        ConversationCreate(mode="chat"), "create-error"
    )
    monkeypatch.setattr(service.conversations, "send_conversation_turn", fail)
    with TestClient(create_app(service=service)) as client:
        response = client.post(
            f"/api/v1/conversations/{conversation.conversation_id}/turns",
            json={"content": "你好", "stream": True},
            headers={"Idempotency-Key": "error-key", "X-Trace-ID": "synthetic-trace"},
        )
    event, error = decode_frames(response.text)[-1]
    assert event == "error"
    assert error["status"] == 422
    assert error["error"]["code"] == "synthetic_error"
    assert error["error"]["trace_id"] == "synthetic-trace"
    assert len(service.conversations.get_conversation(conversation.conversation_id).messages) == 1


@pytest.mark.asyncio
async def test_disconnected_stream_finishes_and_replays_without_duplicate_request(
    service: ApplicationServices,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = configure(service, monkeypatch, "最终回答。")
    entered, release = asyncio.Event(), asyncio.Event()
    original = provider.generate_stream

    async def gated(*args: Any, on_text: Any, **kwargs: Any) -> ProviderResponse:
        await on_text("部分预览")
        entered.set()
        await release.wait()
        return await original(*args, on_text=on_text, **kwargs)

    monkeypatch.setattr(provider, "generate_stream", gated)
    conversation = service.conversations.create_conversation(
        ConversationCreate(mode="chat"), "disconnect-create"
    )
    body = ConversationTurnCreate(content="你好", stream=True, preferred_profile_id="stream-test")
    response = conversation_stream_response(
        service, conversation.conversation_id, body, "disconnect-key", Request({"type": "http"})
    )
    reader = response.body_iterator
    assert "status" in await anext(reader)
    await entered.wait()
    assert "reset" in await anext(reader)
    assert "delta" in await anext(reader)
    await reader.aclose()
    release.set()
    for _ in range(100):
        if len(service.conversations.get_conversation(conversation.conversation_id).messages) == 3:
            break
        await asyncio.sleep(0.01)
    result = await service.conversations.send_conversation_turn(
        conversation.conversation_id, body, "disconnect-key"
    )
    assert result.assistant_message.content == "最终回答。"
    assert len(provider.requests) == 1
