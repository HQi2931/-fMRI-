"""Model provider adapters. Providers only ever receive model-visible tools."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from typing import Any, Protocol
from urllib.parse import urlsplit

import httpx
from pydantic import ValidationError

from neuroagent.agent.models import (
    MAX_TOOL_ARGUMENT_BYTES,
    MAX_TOOL_CALLS_PER_RESPONSE,
    ModelCapability,
    ModelProfile,
    ProviderCitation,
    ProviderResponse,
    ToolCall,
)
from neuroagent.agent.tool_spec import LlmTool, provider_tool_payload


class ProviderError(RuntimeError):
    pass


class RetryableProviderError(ProviderError):
    pass


class ProviderTimeoutError(RetryableProviderError):
    """Provider-side deadline expiry, distinct from a generic transport error."""

    timeout = True


def parse_tool_calls(message: Mapping[str, Any]) -> tuple[ToolCall, ...]:
    """Read OpenAI-compatible ``tool_calls`` defensively.

    Every entry is untrusted provider output: a non-object entry, a missing id,
    a missing function name, an oversized or non-object arguments payload or an
    over-long name is skipped or flagged rather than raising, so one malformed
    call cannot discard an otherwise usable response.
    """

    raw = message.get("tool_calls")
    if not isinstance(raw, list):
        return ()
    calls: list[ToolCall] = []
    for entry in raw[:MAX_TOOL_CALLS_PER_RESPONSE]:
        if not isinstance(entry, dict):
            continue
        call_id = entry.get("id")
        function = entry.get("function")
        if not isinstance(call_id, str) or not call_id or not isinstance(function, dict):
            continue
        name = function.get("name")
        if not isinstance(name, str) or not name or len(name) > 64:
            continue
        arguments, valid = parse_tool_arguments(function.get("arguments"))
        try:
            calls.append(
                ToolCall(
                    call_id=call_id[:128],
                    name=name,
                    arguments=arguments,
                    arguments_valid=valid,
                )
            )
        except ValidationError:
            continue
    return tuple(calls)


def parse_tool_arguments(value: object) -> tuple[dict[str, Any], bool]:
    """Parse a provider's ``arguments`` field, which is normally a JSON string."""

    if value is None or value == "":
        return {}, True
    if isinstance(value, dict):
        return value, True
    if not isinstance(value, str) or len(value) > MAX_TOOL_ARGUMENT_BYTES:
        return {}, False
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return {}, False
    if not isinstance(parsed, dict):
        return {}, False
    return parsed, True


def _completion_payload(
    profile: ModelProfile,
    messages: Sequence[Mapping[str, Any]],
    web_search: bool,
    json_object: bool | None,
    tools: Sequence[LlmTool] | None,
    tool_choice: str,
    *,
    stream: bool,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "model": profile.model,
        "messages": list(messages),
        "stream": stream,
        "max_tokens": profile.max_output_tokens,
    }
    # The DeepSeek API defaults to thinking, which consumes the completion
    # budget before tool calls or JSON appear. Only send this vendor option to
    # the official host, and only when the user explicitly selects it.
    if (
        profile.thinking_mode == "disabled"
        and urlsplit(profile.base_url).hostname == "api.deepseek.com"
    ):
        payload["thinking"] = {"type": "disabled"}
    if json_object is True or (
        json_object is None and ModelCapability.JSON_OBJECT in profile.capabilities
    ):
        payload["response_format"] = {"type": "json_object"}
    if web_search:
        if ModelCapability.WEB_SEARCH not in profile.capabilities:
            raise ProviderError("profile does not declare web search support")
        payload["web_search_options"] = {"search_context_size": "medium"}
    if tools:
        if ModelCapability.TOOL_USE not in profile.capabilities:
            raise ProviderError("profile does not declare tool use support")
        payload["tools"] = provider_tool_payload(tools)
        payload["tool_choice"] = tool_choice
    return payload


async def _sse_data(response: httpx.Response) -> AsyncIterator[str]:
    lines: list[str] = []
    size = 0
    async for line in response.aiter_lines():
        if not line:
            if lines:
                yield "\n".join(lines)
            lines, size = [], 0
        elif line.startswith("data:"):
            value = line[5:].removeprefix(" ")
            size += len(value)
            if size > 1_000_000:
                raise ProviderError("provider stream frame exceeded response limit")
            lines.append(value)
    if lines:
        yield "\n".join(lines)


def _parse_citations(annotations: Sequence[object]) -> tuple[ProviderCitation, ...]:
    citations: dict[str, ProviderCitation] = {}
    for annotation in annotations:
        if not isinstance(annotation, dict):
            continue
        citation = annotation.get("url_citation")
        if not isinstance(citation, dict):
            continue
        url, title = citation.get("url"), citation.get("title")
        if isinstance(url, str) and isinstance(title, str) and url not in citations:
            citations[url] = ProviderCitation(url=url, title=title)
    return tuple(citations.values())


class ModelProvider(Protocol):
    async def generate(
        self,
        profile: ModelProfile,
        api_key: str,
        messages: Sequence[Mapping[str, Any]],
        *,
        web_search: bool = False,
        json_object: bool | None = None,
        tools: Sequence[LlmTool] | None = None,
        tool_choice: str = "auto",
        timeout_seconds: float | None = None,
    ) -> ProviderResponse: ...


class OpenAICompatibleProvider:
    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self._client = client

    async def generate_stream(
        self,
        profile: ModelProfile,
        api_key: str,
        messages: Sequence[Mapping[str, Any]],
        *,
        on_text: Callable[[str], Awaitable[None]],
        web_search: bool = False,
        json_object: bool | None = None,
        tools: Sequence[LlmTool] | None = None,
        tool_choice: str = "auto",
        timeout_seconds: float | None = None,
    ) -> ProviderResponse:
        payload = _completion_payload(
            profile, messages, web_search, json_object, tools, tool_choice, stream=True
        )
        owns_client = self._client is None
        client = self._client or httpx.AsyncClient()
        content: list[str] = []
        content_size = 0
        calls: dict[int, dict[str, Any]] = {}
        annotations: list[object] = []
        usage: dict[str, int] = {}
        request_id: str | None = None
        model = profile.model
        finish_reason: str | None = None
        ended = False
        try:
            async with client.stream(
                "POST",
                f"{profile.base_url}/chat/completions",
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json=payload,
                timeout=timeout_seconds if timeout_seconds is not None else profile.timeout_seconds,
            ) as response:
                if response.status_code == 429 or response.status_code >= 500:
                    raise RetryableProviderError(
                        f"provider temporarily unavailable ({response.status_code})"
                    )
                if response.status_code >= 400:
                    raise ProviderError(f"provider rejected request ({response.status_code})")
                async for data in _sse_data(response):
                    if data == "[DONE]":
                        ended = True
                        break
                    body = json.loads(data)
                    if not isinstance(body, dict) or body.get("error"):
                        raise ProviderError("provider stream returned an error")
                    request_id = body.get("id") or request_id
                    model = str(body.get("model") or model)
                    usage.update(
                        {
                            key: value
                            for key, value in (body.get("usage") or {}).items()
                            if isinstance(value, int)
                        }
                    )
                    choices = body.get("choices", [])
                    for choice in choices:
                        if choice.get("index", 0) != 0:
                            continue
                        delta = choice.get("delta") or {}
                        if isinstance(choice.get("finish_reason"), str):
                            finish_reason = choice["finish_reason"]
                        for call in delta.get("tool_calls") or []:
                            index = call.get("index")
                            if (
                                not isinstance(index, int)
                                or not 0 <= index < MAX_TOOL_CALLS_PER_RESPONSE
                            ):
                                continue
                            entry = calls.setdefault(
                                index, {"id": "", "function": {"name": "", "arguments": ""}}
                            )
                            if isinstance(call.get("id"), str):
                                entry["id"] = (entry["id"] + call["id"])[:128]
                            function = call.get("function") or {}
                            for key, limit in (
                                ("name", 65),
                                ("arguments", MAX_TOOL_ARGUMENT_BYTES + 1),
                            ):
                                if isinstance(function.get(key), str):
                                    entry["function"][key] = (
                                        entry["function"][key] + function[key]
                                    )[:limit]
                        text = delta.get("content")
                        if isinstance(text, str):
                            content_size += len(text)
                            if content_size > 1_000_000:
                                raise ProviderError("provider stream exceeded response limit")
                            content.append(text)
                            # Tool-turn commentary is never the final answer.
                            if not calls:
                                await on_text(text)
                        raw_annotations = delta.get("annotations") or []
                        if isinstance(raw_annotations, list):
                            annotations.extend(raw_annotations[: max(0, 100 - len(annotations))])
                if not ended and finish_reason is None:
                    raise RetryableProviderError("provider stream ended before completion")
            tool_calls = parse_tool_calls({"tool_calls": [calls[index] for index in sorted(calls)]})
            if not content and not tool_calls:
                raise ProviderError("provider returned non-text content")
            return ProviderResponse(
                content="".join(content),
                model=model,
                provider_request_id=request_id,
                usage=usage,
                citations=_parse_citations(annotations),
                tool_calls=tool_calls,
                finish_reason=finish_reason,
            )
        except httpx.TimeoutException as exc:
            raise ProviderTimeoutError(
                "provider transport failed: request exceeded its timeout"
            ) from exc
        except (httpx.NetworkError, httpx.RemoteProtocolError) as exc:
            raise RetryableProviderError("provider transport failed") from exc
        except (KeyError, IndexError, AttributeError, TypeError, ValueError) as exc:
            raise ProviderError("provider response shape is invalid") from exc
        finally:
            if owns_client:
                await client.aclose()

    async def generate(
        self,
        profile: ModelProfile,
        api_key: str,
        messages: Sequence[Mapping[str, Any]],
        *,
        web_search: bool = False,
        json_object: bool | None = None,
        tools: Sequence[LlmTool] | None = None,
        tool_choice: str = "auto",
        timeout_seconds: float | None = None,
    ) -> ProviderResponse:
        payload = _completion_payload(
            profile, messages, web_search, json_object, tools, tool_choice, stream=False
        )

        owns_client = self._client is None
        client = self._client or httpx.AsyncClient(
            timeout=timeout_seconds if timeout_seconds is not None else profile.timeout_seconds
        )
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
            choice = body["choices"][0]
            message = choice["message"]
            tool_calls = parse_tool_calls(message)
            content = message.get("content")
            if not isinstance(content, str):
                # A tool-calling turn legitimately carries an empty content
                # field; only a response with nothing usable is a protocol error.
                if tool_calls:
                    content = ""
                else:
                    raise ProviderError("provider returned non-text content")
            usage = {
                key: int(value)
                for key, value in (body.get("usage") or {}).items()
                if isinstance(value, int)
            }
            citations: list[ProviderCitation] = []
            seen_urls: set[str] = set()
            for annotation in message.get("annotations") or []:
                if not isinstance(annotation, dict):
                    continue
                citation = annotation.get("url_citation")
                if not isinstance(citation, dict):
                    continue
                url = citation.get("url")
                title = citation.get("title")
                if not isinstance(url, str) or not isinstance(title, str) or url in seen_urls:
                    continue
                seen_urls.add(url)
                citations.append(ProviderCitation(url=url, title=title))
            return ProviderResponse(
                content=content,
                provider_request_id=body.get("id"),
                model=str(body.get("model") or profile.model),
                usage=usage,
                citations=tuple(citations),
                tool_calls=tool_calls,
                finish_reason=(
                    choice["finish_reason"]
                    if isinstance(choice.get("finish_reason"), str)
                    else None
                ),
            )
        except httpx.TimeoutException as exc:
            raise ProviderTimeoutError(
                "provider transport failed: request exceeded its timeout"
            ) from exc
        except httpx.NetworkError as exc:
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

    def __init__(self, responses: Sequence[str | ProviderResponse | Exception]) -> None:
        self._responses = list(responses)
        self.requests: list[tuple[ModelProfile, Sequence[Mapping[str, Any]]]] = []
        self.request_options: list[dict[str, bool | None]] = []
        # Recorded separately so the long-standing ``request_options`` shape
        # asserted by existing tests stays byte-identical.
        self.tool_requests: list[tuple[str, ...]] = []

    async def generate_stream(
        self,
        profile: ModelProfile,
        api_key: str,
        messages: Sequence[Mapping[str, Any]],
        *,
        on_text: Callable[[str], Awaitable[None]],
        **kwargs: Any,
    ) -> ProviderResponse:
        response = await self.generate(profile, api_key, messages, **kwargs)
        if not response.tool_calls:
            for offset in range(0, len(response.content), 8):
                await on_text(response.content[offset : offset + 8])
        return response

    async def generate(
        self,
        profile: ModelProfile,
        api_key: str,
        messages: Sequence[Mapping[str, Any]],
        *,
        web_search: bool = False,
        json_object: bool | None = None,
        tools: Sequence[LlmTool] | None = None,
        tool_choice: str = "auto",
        timeout_seconds: float | None = None,
    ) -> ProviderResponse:
        del api_key, tool_choice, timeout_seconds
        self.requests.append((profile, messages))
        self.request_options.append({"web_search": web_search, "json_object": json_object})
        self.tool_requests.append(tuple(tool.name for tool in tools or ()))
        if not self._responses:
            raise ProviderError("mock response queue is empty")
        response = self._responses.pop(0)
        if isinstance(response, Exception):
            raise response
        if isinstance(response, ProviderResponse):
            return response
        return ProviderResponse(content=response, model=profile.model)
