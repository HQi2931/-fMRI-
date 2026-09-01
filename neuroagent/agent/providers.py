"""Model provider adapters. Providers never receive tools or local credentials in payloads."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

import httpx

from neuroagent.agent.models import ModelCapability, ModelProfile, ProviderResponse


class ProviderError(RuntimeError):
    pass


class RetryableProviderError(ProviderError):
    pass


class ModelProvider(Protocol):
    async def generate(
        self,
        profile: ModelProfile,
        api_key: str,
        messages: Sequence[dict[str, str]],
    ) -> ProviderResponse: ...


class OpenAICompatibleProvider:
    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self._client = client

    async def generate(
        self,
        profile: ModelProfile,
        api_key: str,
        messages: Sequence[dict[str, str]],
    ) -> ProviderResponse:
        payload: dict[str, object] = {
            "model": profile.model,
            "messages": list(messages),
            "stream": False,
        }
        if ModelCapability.JSON_OBJECT in profile.capabilities:
            payload["response_format"] = {"type": "json_object"}

        owns_client = self._client is None
        client = self._client or httpx.AsyncClient(timeout=profile.timeout_seconds)
        try:
            response = await client.post(
                f"{profile.base_url}/chat/completions",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )
            if response.status_code == 429 or response.status_code >= 500:
                raise RetryableProviderError(
                    f"provider temporarily unavailable ({response.status_code})"
                )
            if response.status_code >= 400:
                raise ProviderError(f"provider rejected request ({response.status_code})")
            body = response.json()
            content = body["choices"][0]["message"]["content"]
            if not isinstance(content, str):
                raise ProviderError("provider returned non-text content")
            usage = {
                key: int(value)
                for key, value in (body.get("usage") or {}).items()
                if isinstance(value, int)
            }
            return ProviderResponse(
                content=content,
                provider_request_id=body.get("id"),
                model=str(body.get("model") or profile.model),
                usage=usage,
            )
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            raise RetryableProviderError("provider transport failed") from exc
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise ProviderError("provider response shape is invalid") from exc
        finally:
            if owns_client:
                await client.aclose()

    async def list_models(self, base_url: str, api_key: str) -> list[str]:
        """List model ids exposed by an OpenAI-compatible provider."""
        owns_client = self._client is None
        client = self._client or httpx.AsyncClient(timeout=30.0)
        try:
            response = await client.get(
                f"{base_url}/models",
                headers={"Authorization": f"Bearer {api_key}"},
            )
            if response.status_code == 429 or response.status_code >= 500:
                raise RetryableProviderError(
                    f"provider temporarily unavailable ({response.status_code})"
                )
            if response.status_code >= 400:
                raise ProviderError(f"provider rejected model list ({response.status_code})")
            body = response.json()
            items = body.get("data")
            if not isinstance(items, list):
                raise ProviderError("provider model list shape is invalid")
            return [
                str(item["id"])
                for item in items
                if isinstance(item, dict) and isinstance(item.get("id"), str)
            ]
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            raise RetryableProviderError("provider transport failed") from exc
        except (KeyError, TypeError, ValueError) as exc:
            raise ProviderError("provider model list shape is invalid") from exc
        finally:
            if owns_client:
                await client.aclose()


class MockProvider:
    """Deterministic test provider with an explicit response queue."""

    def __init__(self, responses: Sequence[str | Exception]) -> None:
        self._responses = list(responses)
        self.requests: list[tuple[ModelProfile, Sequence[dict[str, str]]]] = []

    async def generate(
        self,
        profile: ModelProfile,
        api_key: str,
        messages: Sequence[dict[str, str]],
    ) -> ProviderResponse:
        del api_key
        self.requests.append((profile, messages))
        if not self._responses:
            raise ProviderError("mock response queue is empty")
        response = self._responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return ProviderResponse(content=response, model=profile.model)
