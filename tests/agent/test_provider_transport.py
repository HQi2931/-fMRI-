"""Transport-level behaviour of the OpenAI-compatible provider adapter.

``tests/agent/test_gateway.py`` exercises routing and fallback with the
deterministic ``MockProvider``; these tests cover the real adapter instead.  The
error classification matters: only transport, rate-limit and 5xx failures may be
retried or routed to another profile, while a rejected request or a malformed
body must surface as a non-retryable ``ProviderError`` so a bad payload is never
silently retried against every configured provider.
"""

from __future__ import annotations

import json

import httpx
import pytest

from neuroagent.agent.models import ModelCapability, ModelProfile
from neuroagent.agent.providers import (
    OpenAICompatibleProvider,
    ProviderError,
    RetryableProviderError,
)


def profile(**overrides: object) -> ModelProfile:
    values: dict[str, object] = {
        "id": "profile-1",
        "provider": "openai-compatible",
        "base_url": "https://example.org/v1",
        "model": "test-model",
        "api_key_env": "TEST_API_KEY",
        "capabilities": frozenset(),
    }
    values.update(overrides)
    return ModelProfile(**values)  # type: ignore[arg-type]


def chat_body(content: object = "ok", **extra: object) -> dict[str, object]:
    message: dict[str, object] = {"content": content}
    message.update(extra)
    return {"id": "chatcmpl-1", "model": "test-model", "choices": [{"message": message}]}


async def generate_with(handler, **overrides: object):  # type: ignore[no-untyped-def]
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = OpenAICompatibleProvider(client)
        return await provider.generate(
            profile(**overrides),
            "secret-key",
            [{"role": "user", "content": "hello"}],
        )


@pytest.mark.parametrize("status", [429, 500, 502, 503])
@pytest.mark.asyncio
async def test_rate_limit_and_server_errors_are_retryable(status: int) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"error": "nope"})

    with pytest.raises(RetryableProviderError, match=str(status)):
        await generate_with(handler)


@pytest.mark.parametrize("status", [400, 401, 403, 404])
@pytest.mark.asyncio
async def test_client_errors_are_not_retryable(status: int) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"error": "bad request"})

    with pytest.raises(ProviderError, match="rejected request") as caught:
        await generate_with(handler)
    assert not isinstance(caught.value, RetryableProviderError)


@pytest.mark.asyncio
async def test_transport_failure_is_retryable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("timed out")

    with pytest.raises(RetryableProviderError, match="transport failed"):
        await generate_with(handler)


@pytest.mark.parametrize(
    "body",
    [
        {},  # no choices
        {"choices": []},  # empty choices
        {"choices": [{}]},  # no message
        {"choices": [{"message": {}}]},  # no content
        {"choices": [{"message": {"content": 42}}]},  # non-text content
    ],
)
@pytest.mark.asyncio
async def test_malformed_response_bodies_are_rejected(body: dict[str, object]) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=body)

    with pytest.raises(ProviderError) as caught:
        await generate_with(handler)
    assert not isinstance(caught.value, RetryableProviderError)


@pytest.mark.asyncio
async def test_non_json_body_is_rejected() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>not json</html>")

    with pytest.raises(ProviderError, match="shape is invalid"):
        await generate_with(handler)


@pytest.mark.asyncio
async def test_token_usage_keeps_only_integer_counters() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = chat_body()
        body["usage"] = {"prompt_tokens": 10, "completion_tokens": 4, "note": "n/a"}
        return httpx.Response(200, json=body)

    result = await generate_with(handler)

    assert result.usage == {"prompt_tokens": 10, "completion_tokens": 4}
    assert result.model == "test-model"
    assert result.provider_request_id == "chatcmpl-1"


@pytest.mark.asyncio
async def test_annotations_are_filtered_and_deduplicated() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=chat_body(
                annotations=[
                    "not-a-dict",
                    {"type": "url_citation"},  # no url_citation payload
                    {"url_citation": {"url": "https://a.example", "title": "A"}},
                    {"url_citation": {"url": "https://a.example", "title": "A again"}},
                    {"url_citation": {"url": "https://b.example", "title": "B"}},
                    {"url_citation": {"url": "https://c.example"}},  # missing title
                ]
            ),
        )

    result = await generate_with(handler)

    assert [citation.url for citation in result.citations] == [
        "https://a.example",
        "https://b.example",
    ]


@pytest.mark.asyncio
async def test_json_object_follows_declared_capability() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(200, json=chat_body())

    await generate_with(
        handler,
        capabilities=frozenset({ModelCapability.JSON_OBJECT}),
    )
    assert captured["response_format"] == {"type": "json_object"}

    captured.clear()
    await generate_with(handler)
    assert "response_format" not in captured


@pytest.mark.asyncio
async def test_web_search_requires_the_declared_capability() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=chat_body())

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = OpenAICompatibleProvider(client)
        with pytest.raises(ProviderError, match="does not declare web search"):
            await provider.generate(
                profile(),
                "secret-key",
                [{"role": "user", "content": "hello"}],
                web_search=True,
            )


@pytest.mark.asyncio
async def test_list_models_returns_only_well_formed_ids() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/models"
        return httpx.Response(
            200,
            json={"data": [{"id": "model-a"}, {"id": "model-b"}, {"no-id": 1}, "junk"]},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = OpenAICompatibleProvider(client)
        models = await provider.list_models("https://example.org/v1", "secret-key")

    assert models == ["model-a", "model-b"]


@pytest.mark.asyncio
async def test_list_models_rejects_a_non_list_payload() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": {"id": "model-a"}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = OpenAICompatibleProvider(client)
        with pytest.raises(ProviderError, match="model list shape is invalid"):
            await provider.list_models("https://example.org/v1", "secret-key")


@pytest.mark.parametrize(
    ("status", "expected"),
    [(429, RetryableProviderError), (500, RetryableProviderError), (400, ProviderError)],
)
@pytest.mark.asyncio
async def test_list_models_maps_http_errors(status: int, expected: type[Exception]) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = OpenAICompatibleProvider(client)
        with pytest.raises(expected):
            await provider.list_models("https://example.org/v1", "secret-key")
