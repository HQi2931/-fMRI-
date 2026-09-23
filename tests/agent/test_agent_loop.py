"""The bounded loop: budgets, self-correction, audit and stop reasons."""

from __future__ import annotations

import json
from typing import Any

import pytest

from neuroagent.agent.loop import (
    UNTRUSTED_TOOL_RESULT_INSTRUCTION,
    AgentLoop,
    AgentLoopBudget,
)
from neuroagent.agent.models import AgentLoopStopReason, ProviderResponse, ToolCall
from neuroagent.agent.tool_result import ToolCallOutcome, ToolCallStatus
from neuroagent.agent.tool_spec import ToolTier


def _tool_call(call_id: str, name: str, arguments: str = "{}") -> ToolCall:
    return ToolCall(call_id=call_id, name=name, arguments=json.loads(arguments))


def _response(
    *,
    content: str = "",
    tool_calls: tuple[ToolCall, ...] = (),
    usage: dict[str, int] | None = None,
    finish_reason: str | None = None,
) -> ProviderResponse:
    return ProviderResponse(
        content=content,
        model="loop-model",
        tool_calls=tool_calls,
        usage=usage or {},
        finish_reason=finish_reason,
    )


class ScriptedDispatcher:
    """Returns pre-scripted outcomes and records the calls it received."""

    def __init__(self, outcomes: list[ToolCallOutcome]) -> None:
        self._outcomes = list(outcomes)
        self.calls: list[ToolCall] = []

    async def dispatch(self, call: ToolCall) -> ToolCallOutcome:
        self.calls.append(call)
        if self._outcomes:
            return self._outcomes.pop(0)
        return ToolCallOutcome(
            call_id=call.call_id,
            name=call.name,
            tier=ToolTier.READ,
            status=ToolCallStatus.SUCCEEDED,
            result={"ok": True},
        )


def _outcome(call: ToolCall, status: ToolCallStatus, code: str | None = None) -> ToolCallOutcome:
    return ToolCallOutcome(
        call_id=call.call_id,
        name=call.name,
        tier=ToolTier.READ,
        status=status,
        error_code=code,
    )


def _messages() -> list[dict[str, Any]]:
    return [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "how is the run going?"},
    ]


@pytest.mark.asyncio
async def test_loop_returns_final_text_when_the_model_stops_calling_tools() -> None:
    call = _tool_call("c1", "get_run_status")
    responses = [
        _response(tool_calls=(call,), usage={"total_tokens": 10}, finish_reason="tool_calls"),
        _response(content="运行中。", usage={"total_tokens": 5}),
    ]
    turns: list[list[dict[str, Any]]] = []

    async def call_model(messages: list[dict[str, Any]]) -> ProviderResponse:
        turns.append(messages)
        return responses.pop(0)

    loop = AgentLoop(ScriptedDispatcher([]))
    result = await loop.run(messages=_messages(), call_model=call_model)

    assert result.stop_reason is AgentLoopStopReason.COMPLETED
    assert result.steps == 2
    assert result.final_text == "运行中。"
    assert result.tool_calls == 1
    assert result.usage == {"total_tokens": 15}
    # The second turn must carry the echoed assistant tool-call turn and its result.
    second = turns[1]
    assert second[-2]["role"] == "assistant"
    assert second[-2]["tool_calls"][0]["function"]["name"] == "get_run_status"
    assert second[-1]["role"] == "tool"
    assert second[-1]["tool_call_id"] == "c1"


@pytest.mark.asyncio
async def test_tool_result_is_wrapped_as_untrusted_data() -> None:
    call = _tool_call("c1", "get_run_status")
    responses = [_response(tool_calls=(call,)), _response(content="done")]
    captured: list[dict[str, Any]] = []

    async def call_model(messages: list[dict[str, Any]]) -> ProviderResponse:
        captured.append(list(messages))
        return responses.pop(0)

    await AgentLoop(ScriptedDispatcher([])).run(messages=_messages(), call_model=call_model)

    tool_message = captured[1][-1]
    envelope = json.loads(tool_message["content"])
    assert envelope["untrusted_content"] is True
    assert envelope["tool"] == "get_run_status"
    assert envelope["status"] == "succeeded"
    assert "instructions" in UNTRUSTED_TOOL_RESULT_INSTRUCTION


@pytest.mark.asyncio
async def test_step_budget_stops_the_loop() -> None:
    call = _tool_call("c1", "get_run_status")

    async def call_model(messages: list[dict[str, Any]]) -> ProviderResponse:
        del messages
        return _response(tool_calls=(call,))

    loop = AgentLoop(ScriptedDispatcher([]), budget=AgentLoopBudget(max_steps=3))
    result = await loop.run(messages=_messages(), call_model=call_model)

    assert result.stop_reason is AgentLoopStopReason.BUDGET_STEPS
    assert result.steps == 3
    assert result.final_text == ""


@pytest.mark.asyncio
async def test_wall_clock_budget_stops_the_loop() -> None:
    ticks = iter([0.0, 0.0, 0.0, 500.0, 500.0, 500.0])

    async def call_model(messages: list[dict[str, Any]]) -> ProviderResponse:
        del messages
        return _response(tool_calls=(_tool_call("c1", "get_run_status"),))

    loop = AgentLoop(
        ScriptedDispatcher([]),
        budget=AgentLoopBudget(max_steps=20, max_seconds=10.0),
        clock=lambda: next(ticks, 500.0),
    )
    result = await loop.run(messages=_messages(), call_model=call_model)

    assert result.stop_reason is AgentLoopStopReason.BUDGET_TIME


@pytest.mark.asyncio
async def test_token_budget_stops_the_loop() -> None:
    async def call_model(messages: list[dict[str, Any]]) -> ProviderResponse:
        del messages
        return _response(tool_calls=(_tool_call("c1", "get_run_status"),))

    loop = AgentLoop(
        ScriptedDispatcher([]),
        budget=AgentLoopBudget(max_steps=20, max_total_tokens=1),
    )
    result = await loop.run(messages=_messages(), call_model=call_model)

    assert result.stop_reason is AgentLoopStopReason.BUDGET_TOKENS
    assert result.steps == 0


@pytest.mark.asyncio
async def test_repeated_identical_failure_trips_the_breaker() -> None:
    call = _tool_call("c1", "get_run_status")
    dispatcher = ScriptedDispatcher(
        [
            _outcome(call, ToolCallStatus.REJECTED, "unknown_tool"),
            _outcome(call, ToolCallStatus.REJECTED, "unknown_tool"),
        ]
    )

    async def call_model(messages: list[dict[str, Any]]) -> ProviderResponse:
        del messages
        return _response(tool_calls=(call,))

    loop = AgentLoop(dispatcher, budget=AgentLoopBudget(max_steps=10, max_repeat_failures=2))
    result = await loop.run(messages=_messages(), call_model=call_model)

    assert result.stop_reason is AgentLoopStopReason.REPEATED_TOOL_FAILURE
    assert result.tool_calls == 2


@pytest.mark.asyncio
async def test_alternating_failures_do_not_trip_the_breaker_early() -> None:
    first = _tool_call("c1", "get_run_status")
    second = _tool_call("c2", "list_run_artifacts")
    dispatcher = ScriptedDispatcher(
        [
            _outcome(first, ToolCallStatus.FAILED, "tool_execution_failed"),
            _outcome(second, ToolCallStatus.FAILED, "tool_execution_failed"),
            _outcome(first, ToolCallStatus.SUCCEEDED),
        ]
    )

    async def call_model(messages: list[dict[str, Any]]) -> ProviderResponse:
        del messages
        return _response(tool_calls=(first,))

    loop = AgentLoop(dispatcher, budget=AgentLoopBudget(max_steps=3, max_repeat_failures=2))
    result = await loop.run(messages=_messages(), call_model=call_model)

    assert result.stop_reason is AgentLoopStopReason.BUDGET_STEPS
    assert result.tool_calls == 3


@pytest.mark.asyncio
async def test_provider_failure_stops_without_leaking_details() -> None:
    async def call_model(messages: list[dict[str, Any]]) -> ProviderResponse:
        del messages
        raise RuntimeError(r"D:\private\key.txt leaked")

    result = await AgentLoop(ScriptedDispatcher([])).run(
        messages=_messages(), call_model=call_model
    )

    assert result.stop_reason is AgentLoopStopReason.PROVIDER_ERROR
    assert result.final_text == ""
    assert "private" not in json.dumps(result.model_dump(mode="json"))


@pytest.mark.asyncio
async def test_cancellation_is_checked_before_each_turn() -> None:
    async def call_model(messages: list[dict[str, Any]]) -> ProviderResponse:
        del messages
        return _response(content="should not be reached")

    result = await AgentLoop(ScriptedDispatcher([])).run(
        messages=_messages(), call_model=call_model, is_cancelled=lambda: True
    )

    assert result.stop_reason is AgentLoopStopReason.CANCELLED
    assert result.steps == 0


@pytest.mark.asyncio
async def test_emit_receives_each_outcome_with_its_step() -> None:
    call = _tool_call("c1", "get_run_status")
    responses = [_response(tool_calls=(call,)), _response(content="done")]
    seen: list[tuple[str, int]] = []

    async def call_model(messages: list[dict[str, Any]]) -> ProviderResponse:
        del messages
        return responses.pop(0)

    loop = AgentLoop(
        ScriptedDispatcher([]), emit=lambda outcome, step: seen.append((outcome.name, step))
    )
    await loop.run(messages=_messages(), call_model=call_model)

    assert seen == [("get_run_status", 1)]
