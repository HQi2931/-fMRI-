import json

import httpx
import pytest

from neuroagent.agent.models import ModelProfile
from neuroagent.agent.redaction import OutboundContextPolicy
from neuroagent.memory.embeddings import MemoryEmbeddingError, MemoryEmbeddings


class Secrets:
    def resolve(self, name):
        return "fixture-key"


def adapter(client):
    return MemoryEmbeddings(
        ModelProfile(
            id="memory",
            provider="openai-compatible",
            model="embedding-fixture",
            base_url="https://example.test/v1",
            api_key_env="MEMORY_KEY",
        ),
        Secrets(),
        OutboundContextPolicy("memory-test-redaction"),
        client=client,
    )


async def test_embedding_redacts_text_and_restores_input_order():
    def handler(request):
        body = json.loads(request.content)
        assert "person@example.org" not in request.content.decode()
        assert len(body["input"]) == 2
        return httpx.Response(
            200,
            json={
                "data": [
                    {"index": 1, "embedding": [0, 1]},
                    {"index": 0, "embedding": [1, 0]},
                ]
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        assert await adapter(client).embed_documents(["person@example.org", "second"]) == (
            (1.0, 0.0),
            (0.0, 1.0),
        )


@pytest.mark.parametrize(
    "data",
    [
        [],
        [{"index": 0, "embedding": [0, 0]}],
        [{"index": 1, "embedding": [1]}],
        [{"index": 0, "embedding": [True]}],
        [{"index": 0, "embedding": ["secret response"]}],
    ],
)
async def test_invalid_embeddings_fail_closed(data):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"data": data}),
        )
    ) as client:
        with pytest.raises(MemoryEmbeddingError):
            await adapter(client).embed_query("question")


async def test_provider_failure_does_not_expose_response():
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(503, text="private provider body"),
        )
    ) as client:
        with pytest.raises(MemoryEmbeddingError, match=r"^memory_embedding_unavailable$"):
            await adapter(client).embed_query("question")
