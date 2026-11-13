"""Provider-side tool-calling transport: request shape and defensive parsing."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from neuroagent.agent.models import ModelCapability, ModelProfile, ToolCall
from neuroagent.agent.providers import (
    OpenAICompatibleProvider,
    ProviderError,
    parse_tool_arguments,
    parse_tool_calls,
)
from neuroagent.agent.tool_spec import LlmTool, ToolTier

_TOOL = LlmTool(
    name="get_run_status",
    description="Read the current run state.",
    parameters={
        "type": "object",
        "properties": {"run_id": {"type": "string"}},
        "required": ["run_id"],
    },
    tier=ToolTier.READ,
)

_PRIVATE_TOOL = LlmTool(
    name="start_preprocessing",
    description="Queue a real DPABI preprocessing run.",
    tier=ToolTier.EXECUTE,
)


def _profile(*capabilities: ModelCapability) -> ModelProfile:
    return ModelProfile(
        id="tool-profile",
        provider="openai-compatible",
        base_url="https://example.org/v1",
        model="tool-model",
        api_key_env="TOOL_API_KEY",
        capabilities=frozenset(capabilities),
    )


class _Recorder:
    def __init__(self, body: dict[str, Any]) -> None:
        self.payload: dict[str, Any] = {}
        self._body = body

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.payload.update(json.loads(request.content))
        return httpx.Response(200, json=self._body)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "host,expected", [("api.deepseek.com", True), ("proxy.example.org", False)]
)
async def test_explicit_nonthinking_mode_is_only_sent_to_deepseek(
    host: str, expected: bool
) -> None:
    recorder = _Recorder({"choices": [{"message": {"content": "ok"}}]})
    selected = _profile(ModelCapability.JSON_OBJECT).model_copy(
        update={
            "base_url": f"https://{host}/v1",
            "thinking_mode": "disabled",
        }
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(recorder)) as client:
        await OpenAICompatibleProvider(client).generate(
            selected, "secret", [{"role": "user", "content": "test"}]
        )
    assert ("thinking" in recorder.payload) is expected
    if expected:
        assert recorder.payload["thinking"] == {"type": "disabled"}


def _tool_call_body(arguments: object, *, content: object = None) -> dict[str, Any]:
    return {
        "id": "chatcmpl-tools",
        "model": "tool-model",
        "choices": [
            {
                "finish_reason": "tool_calls",
                "message": {
                    "content": content,
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {
                                "name": "get_run_status",
                                "arguments": arguments,
                            },
                        }
                    ],
                },
            }
        ],
    }


@pytest.mark.asyncio
async def test_tools_are_sent_only_when_offered_and_declared() -> None:
    recorder = _Recorder({"choices": [{"message": {"content": "ok"}}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(recorder)) as client:
        provider = OpenAICompatibleProvider(client)
        await provider.generate(
            _profile(ModelCapability.JSON_OBJECT),
            "secret",
            [{"role": "user", "content": "hi"}],
        )
    assert "tools" not in recorder.payload

    async with httpx.AsyncClient(transport=httpx.MockTransport(recorder)) as client:
        provider = OpenAICompatibleProvider(client)
        await provider.generate(
            _profile(ModelCapability.TOOL_USE),
            "secret",
            [{"role": "user", "content": "hi"}],
            tools=[_TOOL],
        )
    assert recorder.payload["tool_choice"] == "auto"
    assert recorder.payload["tools"] == [
        {
            "type": "function",
            "function": {
                "name": "get_run_status",
                "description": "Read the current run state.",
                "parameters": {
                    "type": "object",
                    "properties": {"run_id": {"type": "string"}},
                    "required": ["run_id"],
                },
            },
        }
    ]


@pytest.mark.asyncio
async def test_offering_tools_requires_the_declared_capability() -> None:
    recorder = _Recorder({"choices": [{"message": {"content": "ok"}}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(recorder)) as client:
        provider = OpenAICompatibleProvider(client)
        with pytest.raises(ProviderError, match="tool use support"):
            await provider.generate(
                _profile(ModelCapability.JSON_OBJECT),
                "secret",
                [{"role": "user", "content": "hi"}],
                tools=[_TOOL],
            )
    assert "tools" not in recorder.payload


@pytest.mark.asyncio
async def test_tool_call_response_is_parsed_with_empty_content() -> None:
    recorder = _Recorder(_tool_call_body('{"run_id": "abc"}'))

    async with httpx.AsyncClient(transport=httpx.MockTransport(recorder)) as client:
        provider = OpenAICompatibleProvider(client)
        result = await provider.generate(
            _profile(ModelCapability.TOOL_USE),
            "secret",
            [{"role": "user", "content": "hi"}],
            tools=[_TOOL],
        )

    assert result.content == ""
    assert result.finish_reason == "tool_calls"
    assert result.tool_calls == (
        ToolCall(
            call_id="call_1",
            name="get_run_status",
            arguments={"run_id": "abc"},
            arguments_valid=True,
        ),
    )


@pytest.mark.asyncio
async def test_malformed_arguments_are_flagged_rather_than_failing_the_call() -> None:
    recorder = _Recorder(_tool_call_body("{not json"))

    async with httpx.AsyncClient(transport=httpx.MockTransport(recorder)) as client:
        provider = OpenAICompatibleProvider(client)
        result = await provider.generate(
            _profile(ModelCapability.TOOL_USE),
            "secret",
            [{"role": "user", "content": "hi"}],
            tools=[_TOOL],
        )

    call = result.tool_calls[0]
    assert call.arguments == {}
    assert call.arguments_valid is False


def test_tool_arguments_parsing_accepts_objects_and_rejects_junk() -> None:
    assert parse_tool_arguments(None) == ({}, True)
    assert parse_tool_arguments("") == ({}, True)
    assert parse_tool_arguments('{"a": 1}') == ({"a": 1}, True)
    # Some gateways hand back an already-decoded object.
    assert parse_tool_arguments({"a": 1}) == ({"a": 1}, True)
    # A JSON array is not a valid arguments object.
    assert parse_tool_arguments("[1, 2]") == ({}, False)
    assert parse_tool_arguments(42) == ({}, False)
    assert parse_tool_arguments("x" * 40_000) == ({}, False)


def test_tool_call_parsing_skips_entries_it_cannot_trust() -> None:
    message = {
        "tool_calls": [
            "not-a-dict",
            {"id": "", "function": {"name": "x"}},
            {"id": "c1"},
            {"id": "c2", "function": "not-a-dict"},
            {"id": "c3", "function": {"name": ""}},
            {"id": "c4", "function": {"name": "y" * 65}},
            {"id": "c5", "function": {"name": "good_tool", "arguments": "{}"}},
        ]
    }

    calls = parse_tool_calls(message)
    assert [call.name for call in calls] == ["good_tool"]


def test_tool_call_parsing_bounds_the_number_of_calls() -> None:
    message = {
        "tool_calls": [
            {"id": f"c{index}", "function": {"name": "t", "arguments": "{}"}}
            for index in range(200)
        ]
    }
    assert len(parse_tool_calls(message)) <= 32


def test_missing_tool_calls_field_is_not_an_error() -> None:
    assert parse_tool_calls({"content": "just text"}) == ()
    assert parse_tool_calls({"tool_calls": "not-a-list"}) == ()


def test_private_tool_cannot_be_serialized_through_the_catalog_helper() -> None:
    """A privileged tool must be filtered, never merely refused later."""

    from neuroagent.agent.tool_spec import build_model_visible_catalog, provider_tool_payload

    payload = provider_tool_payload(build_model_visible_catalog([_TOOL, _PRIVATE_TOOL]))
    assert [entry["function"]["name"] for entry in payload] == ["get_run_status"]
