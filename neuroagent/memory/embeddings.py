"""Validated, redacted embedding requests for semantic memory."""

from __future__ import annotations

import math
from collections.abc import Sequence

import httpx

from neuroagent.agent.models import ModelProfile
from neuroagent.agent.redaction import OutboundContextPolicy
from neuroagent.agent.secrets import SecretResolver


class MemoryEmbeddingError(RuntimeError):
    """Safe error with no provider body, secret, or memory text."""


class MemoryEmbeddings:
    def __init__(
        self,
        profile: ModelProfile,
        secrets: SecretResolver,
        policy: OutboundContextPolicy,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.profile = profile
        self._secrets = secrets
        self._policy = policy
        self._client = client

    @property
    def identity(self) -> str:
        return f"{self.profile.base_url}|{self.profile.model}"

    async def embed_documents(self, texts: Sequence[str]) -> tuple[tuple[float, ...], ...]:
        if not texts:
            return ()
        key = self._secrets.resolve(self.profile.api_key_env)
        if not key:
            raise MemoryEmbeddingError("memory_embedding_key_missing")
        safe_texts = [str(self._policy.redact({"text": text}).payload["text"]) for text in texts]
        client = self._client or httpx.AsyncClient(timeout=self.profile.timeout_seconds)
        try:
            response = await client.post(
                f"{self.profile.base_url}/embeddings",
                headers={"Authorization": f"Bearer {key}"},
                json={"model": self.profile.model, "input": safe_texts},
                timeout=self.profile.timeout_seconds,
            )
            if response.status_code >= 400:
                raise MemoryEmbeddingError("memory_embedding_unavailable")
            data = response.json()["data"]
            if not isinstance(data, list) or len(data) != len(texts):
                raise ValueError("invalid result count")
            indexed: dict[int, tuple[float, ...]] = {}
            for item in data:
                index, vector = item["index"], item["embedding"]
                if type(index) is not int or index in indexed or not 0 <= index < len(texts):
                    raise ValueError("invalid result index")
                if not isinstance(vector, list) or not 1 <= len(vector) <= 16384:
                    raise ValueError("invalid vector length")
                if not all(
                    type(value) in (float, int) and math.isfinite(value) for value in vector
                ):
                    raise ValueError("invalid vector values")
                if not any(vector):
                    raise ValueError("zero vector")
                indexed[index] = tuple(float(value) for value in vector)
            vectors = tuple(indexed[index] for index in range(len(texts)))
            if len({len(vector) for vector in vectors}) != 1:
                raise ValueError("inconsistent dimensions")
            return vectors
        except (httpx.HTTPError, KeyError, TypeError, ValueError) as exc:
            raise MemoryEmbeddingError("memory_embedding_invalid_or_unavailable") from exc
        finally:
            if self._client is None:
                await client.aclose()

    async def embed_query(self, text: str) -> tuple[float, ...]:
        return (await self.embed_documents([text]))[0]
