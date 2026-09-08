from __future__ import annotations

import json

import httpx
import pytest

from neuroagent.agent.models import ModelCapability, ModelProfile
from neuroagent.agent.providers import OpenAICompatibleProvider


@pytest.mark.asyncio
async def test_openai_compatible_web_search_sends_option_and_reads_citations() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-test",
                "model": "search-model",
                "choices": [
                    {
                        "message": {
                            "content": "回答 [来源]",
                            "annotations": [
                                {
                                    "type": "url_citation",
                                    "url_citation": {
                                        "url": "https://example.org/paper",
                                        "title": "A paper",
                                        "start_index": 3,
                                        "end_index": 7,
                                    },
                                }
                            ],
                        }
                    }
                ],
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = OpenAICompatibleProvider(client)
        result = await provider.generate(
            ModelProfile(
                id="search-profile",
                provider="openai-compatible",
                base_url="https://example.org/v1",
                model="search-model",
                api_key_env="SEARCH_API_KEY",
                capabilities=frozenset(
                    {ModelCapability.JSON_OBJECT, ModelCapability.WEB_SEARCH}
                ),
            ),
            "secret",
            [{"role": "user", "content": "ALFF 是什么?"}],
            web_search=True,
            json_object=False,
        )

    assert captured["web_search_options"] == {"search_context_size": "medium"}
    assert "response_format" not in captured
    assert result.citations[0].url == "https://example.org/paper"
