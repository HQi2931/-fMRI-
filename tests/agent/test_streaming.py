"""Native stream assembly and isolation of user-visible answer previews."""

# ruff: noqa: RUF001

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from neuroagent.agent.gateway import ModelGateway
from neuroagent.agent.models import ModelCapability, ModelProfile
from neuroagent.agent.providers import (
    OpenAICompatibleProvider,
    ProviderError,
    RetryableProviderError,
)
from neuroagent.agent.redaction import OutboundContextPolicy
from neuroagent.agent.router import ModelRouter
from neuroagent.agent.secrets import ProcessEnvironmentSecretResolver
from neuroagent.agent.streaming import AnswerStream, current_answer_stream, observe_answers


def profile() -> ModelProfile:
    return ModelProfile(
        id="stream-model",
        provider="openai-compatible",
        base_url="https://example.org/v1",
        model="test-model",
        api_key_env="STREAM_TEST_KEY",
        capabilities=frozenset({ModelCapability.STREAMING, ModelCapability.TOOL_USE}),
    )


def frame(delta: dict | None = None, **choice: object) -> bytes:
    body = {"id": "request-1", "choices": [{"index": 0, "delta": delta or {}, **choice}]}
    return f"data: {json.dumps(body)}\r\n\r\n".encode()


class Pieces(httpx.AsyncByteStream):
    def __init__(self, parts: list[bytes]) -> None:
        self.parts = parts

    async def __aiter__(self):
        for part in self.parts:
            yield part


@pytest.mark.asyncio
async def test_native_stream_emits_before_completion_and_preserves_usage() -> None:
    received = asyncio.Event()
    release = asyncio.Event()
    chunks: list[str] = []

    class Gated(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield frame({"content": "首段中文"})
            await release.wait()
            yield frame({"content": "，第二段"}, finish_reason="stop")
            yield b'data: {"choices": [], "usage": {"completion_tokens": 6}}\n\n'
            yield b"data: [DONE]\n\n"

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert payload["stream"] is True
        assert request.headers["authorization"] == "Bearer synthetic-key"
        return httpx.Response(200, stream=Gated())

    async def text(value: str) -> None:
        chunks.append(value)
        received.set()

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        task = asyncio.create_task(
            OpenAICompatibleProvider(client).generate_stream(
                profile(),
                "synthetic-key",
                [],
                on_text=text,
                json_object=False,
            )
        )
        await asyncio.wait_for(received.wait(), 2)
        assert chunks == ["首段中文"]
        assert not task.done()
        release.set()
        response = await task
    assert response.content == "首段中文，第二段"
    assert response.usage == {"completion_tokens": 6}
    assert response.provider_request_id == "request-1"
    assert response.finish_reason == "stop"


@pytest.mark.asyncio
async def test_stream_reassembles_tool_arguments_and_hides_tool_commentary() -> None:
    parts = [
        frame(
            {
                "tool_calls": [
                    {
                        "index": 0,
                        "id": "call-1",
                        "function": {"name": "lookup", "arguments": '{"q":'},
                    }
                ]
            }
        ),
        frame(
            {
                "content": "internal commentary",
                "tool_calls": [{"index": 0, "function": {"arguments": '"ALFF"}'}}],
            }
        ),
        frame(finish_reason="tool_calls"),
        b"data: [DONE]\n\n",
    ]
    chunks: list[str] = []

    async def text(value: str) -> None:
        chunks.append(value)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, stream=Pieces(parts)))
    ) as client:
        response = await OpenAICompatibleProvider(client).generate_stream(
            profile(),
            "synthetic-key",
            [],
            on_text=text,
        )
    assert not chunks
    assert response.tool_calls[0].call_id == "call-1"
    assert response.tool_calls[0].name == "lookup"
    assert response.tool_calls[0].arguments == {"q": "ALFF"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "parts,error",
    [
        ([frame({"content": "partial"})], RetryableProviderError),
        ([b"data: {bad}\n\n"], ProviderError),
        ([b'data: {"error": {"message": "private error"}}\n\n'], ProviderError),
    ],
)
async def test_broken_stream_is_not_a_success(parts: list[bytes], error: type[Exception]) -> None:
    async def text(value: str) -> None:
        pass

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, stream=Pieces(parts)))
    ) as client:
        with pytest.raises(error):
            await OpenAICompatibleProvider(client).generate_stream(
                profile(),
                "synthetic-key",
                [],
                on_text=text,
            )


@pytest.mark.asyncio
async def test_work_preview_decodes_split_escapes_and_ignores_nested_fields() -> None:
    events: list[tuple[str, dict[str, object]]] = []

    async def emit(event: str, data: dict[str, object]) -> None:
        events.append((event, data))

    observer = AnswerStream("work", emit)
    await observer.reset()
    chunks = [
        '{"draft_patch":{"answer":"hidden","path":"private"},"answer":"',
        r"\u4f",
        r"60\u597d\n\"",
        r"quoted\"\uD83D",
        r"\uDE00",
        '","action":"conversation"}',
    ]
    for chunk in chunks:
        await observer.text(chunk)
    assert (
        "".join(str(data["text"]) for event, data in events if event == "delta")
        == '你好\n"quoted"😀'
    )
    assert "hidden" not in str(events)
    assert "draft_patch" not in str(events)


@pytest.mark.asyncio
async def test_observer_context_is_isolated_between_concurrent_requests() -> None:
    gate = asyncio.Event()

    async def emit(event: str, data: dict[str, object]) -> None:
        pass

    async def request(mode: str) -> None:
        observer = AnswerStream(mode, emit)
        with observe_answers(observer):
            gate.set()
            await asyncio.sleep(0)
            assert current_answer_stream() is observer
        assert current_answer_stream() is None

    await asyncio.gather(request("chat"), request("work"))
    assert current_answer_stream() is None


@pytest.mark.asyncio
async def test_routing_is_buffered_while_final_answer_streams(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from neuroagent.agent.providers import MockProvider

    monkeypatch.setenv("STREAM_TEST_KEY", "synthetic-key")
    provider = MockProvider(['{"intent":"conversation"}', "模型正文流式回答分段展示"])
    gateway = ModelGateway(
        ModelRouter([profile()], {}),
        {"openai-compatible": provider},
        OutboundContextPolicy("synthetic-streaming-redaction-salt"),
        ProcessEnvironmentSecretResolver(),
    )
    events: list[tuple[str, dict[str, object]]] = []

    async def emit(event: str, data: dict[str, object]) -> None:
        events.append((event, data))

    with observe_answers(AnswerStream("chat", emit)):
        await gateway.generate_chat(
            question="hello",
            evidence=[],
            preferred_profile_id=None,
            model=None,
            allow_web_search=False,
            routing=True,
        )
        assert events == []
        await gateway.generate_chat(
            question="hello",
            evidence=[],
            preferred_profile_id=None,
            model=None,
            allow_web_search=False,
        )
    assert [event for event, data in events] == ["reset", "delta", "delta"]


@pytest.mark.asyncio
async def test_provider_fallback_resets_partial_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STREAM_TEST_KEY", "synthetic-key")
    count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal count
        count += 1
        parts = (
            [frame({"content": "未完成的回答"})]
            if count == 1
            else [frame({"content": "最终回答"}, finish_reason="stop"), b"data: [DONE]\n\n"]
        )
        return httpx.Response(200, stream=Pieces(parts))

    events: list[tuple[str, dict[str, object]]] = []

    async def emit(event: str, data: dict[str, object]) -> None:
        events.append((event, data))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        profiles = [
            profile().model_copy(update={"priority": 1}),
            profile().model_copy(update={"id": "fallback", "priority": 2}),
        ]
        gateway = ModelGateway(
            ModelRouter(profiles, {}),
            {"openai-compatible": OpenAICompatibleProvider(client)},
            OutboundContextPolicy("synthetic-streaming-redaction-salt"),
            ProcessEnvironmentSecretResolver(),
        )
        with observe_answers(AnswerStream("chat", emit)):
            result = await gateway.generate_chat(
                question="hello",
                evidence=[],
                preferred_profile_id=None,
                model=None,
                allow_web_search=False,
            )
    assert [event for event, data in events] == ["reset", "delta", "reset", "delta"]
    assert result.response.content == "最终回答"
    assert result.selected_profile_id == "fallback"
