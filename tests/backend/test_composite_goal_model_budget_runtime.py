from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

from neuroagent.agent.gateway import ChatGatewayResult, ChatMessages, ToolTurnResult
from neuroagent.agent.loop import AgentLoopBudget
from neuroagent.agent.models import ProviderResponse
from neuroagent.application.work_model_runtime import WorkModelRuntime
from neuroagent.application.work_tools import (
    AgentExecutionContext,
    WorkToolRunner,
)


class _PersistentBudget:
    def __init__(self, status: str = "reserved") -> None:
        self.status = status
        self.estimates: list[int] = []
        self.elapsed_estimates: list[int] = []
        self.settlements: list[tuple[int | None, int]] = []

    def reserve(self, estimated_tokens: int, estimated_elapsed_seconds: int) -> Any:
        self.estimates.append(estimated_tokens)
        self.elapsed_estimates.append(estimated_elapsed_seconds)
        return SimpleNamespace(
            status=SimpleNamespace(value=self.status),
            reservation=SimpleNamespace(
                reservation_id=f"reservation-{len(self.estimates)}",
                reserved_elapsed_seconds=min(1, estimated_elapsed_seconds),
            ),
        )

    def settle(
        self,
        reserve_result: Any,
        *,
        actual_tokens: int | None,
        elapsed_seconds: int,
    ) -> None:
        reservation = reserve_result.reservation
        assert reservation.reservation_id.startswith("reservation-")
        self.settlements.append((actual_tokens, elapsed_seconds))


class _ReadOnlyDispatcher:
    catalog: tuple[()] = ()

    async def dispatch(self, call: Any, *, timeout_seconds: float | None = None) -> Any:
        raise AssertionError("No tools are offered in this test")


def _context() -> dict[str, Any]:
    return {
        "question": "解释当前任务",
        "recent_messages": [],
        "pinned_context": [],
        "conversation_summary": None,
        "work_context": {"agent_role": "analysis"},
        "context_window_tokens": 16_000,
        "max_output_tokens": 2_048,
    }


def _generated(
    tokens: int = 42,
    attempted_profile_ids: tuple[str, ...] = ("test-profile",),
) -> ChatGatewayResult:
    return ChatGatewayResult(
        response=ProviderResponse(
            content='{"answer":"已核对","action":"conversation"}',
            model="budget-test-model",
            usage={"total_tokens": tokens},
        ),
        selected_profile_id="test-profile",
        context_hash="a" * 64,
        attempted_profile_ids=attempted_profile_ids,
        remote_search_used=False,
        redaction_count=0,
    )


def _tool_runner(call_turn: Any) -> WorkToolRunner:
    async def available(_profile_id: str | None) -> bool:
        return True

    return WorkToolRunner(
        build_messages=lambda **_kwargs: ChatMessages(
            messages=[{"role": "user", "content": "解释当前任务"}],
            payload={},
            context_hash="b" * 64,
            redaction_count=0,
        ),
        call_turn=call_turn,
        dispatcher_factory=lambda _context: _ReadOnlyDispatcher(),
        probe_capability=available,
    )


def test_tool_loop_reserves_and_settles_each_provider_call() -> None:
    budget = _PersistentBudget()
    calls = 0

    async def call_turn(**_kwargs: Any) -> ToolTurnResult:
        nonlocal calls
        calls += 1
        return ToolTurnResult(
            response=ProviderResponse(
                content='{"answer":"已核对","action":"conversation"}',
                model="budget-test-model",
                usage={"total_tokens": 31},
            ),
            selected_profile_id="test-profile",
            attempted_profile_ids=("test-profile",),
        )

    runtime = WorkModelRuntime(
        generate=lambda **_kwargs: _generated(),
        tool_runner=_tool_runner(call_turn),
        model_budget=AgentLoopBudget(),
        is_valid_response=lambda _text: True,
    )
    text, metadata, _audit = asyncio.run(
        runtime.interpret(
            _context(),
            preferred_profile_id=None,
            model=None,
            execution_context=AgentExecutionContext(work_agent_role="analysis"),
            persistent_model_budget=budget,
        )
    )

    assert text.startswith("{")
    assert metadata["usage"]["total_tokens"] == 31
    assert calls == 1
    assert len(budget.estimates) == len(budget.settlements) == 1
    assert budget.estimates[0] > 0
    assert budget.elapsed_estimates[0] > 0
    assert budget.settlements[0][0] == 31


def test_tool_loop_charges_estimate_for_reported_gateway_failover_attempts() -> None:
    budget = _PersistentBudget()

    async def call_turn(**_kwargs: Any) -> ToolTurnResult:
        return ToolTurnResult(
            response=ProviderResponse(
                content='{"answer":"已核对","action":"conversation"}',
                model="budget-test-model",
                usage={"total_tokens": 31},
            ),
            selected_profile_id="fallback-profile",
            attempted_profile_ids=("primary-profile", "fallback-profile"),
        )

    runtime = WorkModelRuntime(
        generate=lambda **_kwargs: _generated(),
        tool_runner=_tool_runner(call_turn),
        model_budget=AgentLoopBudget(),
        is_valid_response=lambda _text: True,
    )
    asyncio.run(
        runtime.interpret(
            _context(),
            preferred_profile_id=None,
            model=None,
            execution_context=AgentExecutionContext(work_agent_role="analysis"),
            persistent_model_budget=budget,
        )
    )

    assert budget.settlements[0][0] == 31 + budget.estimates[0]


def test_exhausted_tool_loop_budget_stops_before_provider_and_fallback() -> None:
    budget = _PersistentBudget(status="exhausted")
    provider_calls = 0

    async def call_turn(**_kwargs: Any) -> ToolTurnResult:
        nonlocal provider_calls
        provider_calls += 1
        raise AssertionError("An exhausted budget must block the provider")

    async def generate(**_kwargs: Any) -> ChatGatewayResult:
        nonlocal provider_calls
        provider_calls += 1
        raise AssertionError("An exhausted tool loop must not trigger fallback")

    runtime = WorkModelRuntime(
        generate=generate,
        tool_runner=_tool_runner(call_turn),
        model_budget=AgentLoopBudget(),
        is_valid_response=lambda _text: True,
    )
    text, metadata, _audit = asyncio.run(
        runtime.interpret(
            _context(),
            preferred_profile_id=None,
            model=None,
            execution_context=AgentExecutionContext(work_agent_role="analysis"),
            persistent_model_budget=budget,
        )
    )

    assert text == ""
    assert metadata["stop_reason"] == "composite_goal_model_budget_exhausted"
    assert provider_calls == 0
    assert len(budget.estimates) == 1
    assert budget.settlements == []


def test_duplicate_tool_call_reservation_stops_without_claiming_budget_exhaustion() -> None:
    budget = _PersistentBudget(status="duplicate")
    provider_calls = 0

    async def call_turn(**_kwargs: Any) -> ToolTurnResult:
        nonlocal provider_calls
        provider_calls += 1
        raise AssertionError("A duplicate reservation must not dispatch the provider")

    async def generate(**_kwargs: Any) -> ChatGatewayResult:
        nonlocal provider_calls
        provider_calls += 1
        raise AssertionError("A duplicate turn must not trigger an unreserved fallback")

    runtime = WorkModelRuntime(
        generate=generate,
        tool_runner=_tool_runner(call_turn),
        model_budget=AgentLoopBudget(),
        is_valid_response=lambda _text: True,
    )
    text, metadata, _audit = asyncio.run(
        runtime.interpret(
            _context(),
            preferred_profile_id=None,
            model=None,
            execution_context=AgentExecutionContext(work_agent_role="analysis"),
            persistent_model_budget=budget,
        )
    )

    assert text == ""
    assert metadata["stop_reason"] == "composite_goal_model_call_duplicate"
    assert provider_calls == 0
    assert budget.settlements == []


def test_single_shot_fallback_uses_and_settles_the_shared_budget() -> None:
    budget = _PersistentBudget()

    async def generate(**_kwargs: Any) -> ChatGatewayResult:
        return _generated(tokens=57)

    runtime = WorkModelRuntime(
        generate=generate,
        tool_runner=None,
        model_budget=AgentLoopBudget(),
        is_valid_response=lambda _text: True,
    )
    text, metadata, _audit = asyncio.run(
        runtime.interpret(
            _context(),
            preferred_profile_id=None,
            model=None,
            allow_tools=False,
            persistent_model_budget=budget,
        )
    )

    assert text.startswith("{")
    assert metadata["usage"]["total_tokens"] == 57
    assert len(budget.estimates) == len(budget.settlements) == 1
    assert budget.estimates[0] > 0
    assert budget.elapsed_estimates[0] > 0
    assert budget.settlements[0][0] == 57
    assert budget.settlements[0][1] >= 1


def test_single_shot_charges_estimate_for_reported_gateway_failover_attempts() -> None:
    budget = _PersistentBudget()

    async def generate(**_kwargs: Any) -> ChatGatewayResult:
        return _generated(
            tokens=57,
            attempted_profile_ids=("primary-profile", "fallback-profile"),
        )

    runtime = WorkModelRuntime(
        generate=generate,
        tool_runner=None,
        model_budget=AgentLoopBudget(),
        is_valid_response=lambda _text: True,
    )

    asyncio.run(
        runtime.interpret(
            _context(),
            preferred_profile_id=None,
            model=None,
            allow_tools=False,
            persistent_model_budget=budget,
        )
    )

    assert budget.settlements[0][0] == 57 + budget.estimates[0]


def test_exhausted_single_shot_budget_never_calls_provider() -> None:
    budget = _PersistentBudget(status="exhausted")
    provider_calls = 0

    async def generate(**_kwargs: Any) -> ChatGatewayResult:
        nonlocal provider_calls
        provider_calls += 1
        raise AssertionError("An exhausted budget must block the provider")

    runtime = WorkModelRuntime(
        generate=generate,
        tool_runner=None,
        model_budget=AgentLoopBudget(),
        is_valid_response=lambda _text: True,
    )
    text, metadata, _audit = asyncio.run(
        runtime.interpret(
            _context(),
            preferred_profile_id=None,
            model=None,
            allow_tools=False,
            persistent_model_budget=budget,
        )
    )

    assert text == ""
    assert metadata["stop_reason"] == "composite_goal_model_budget_exhausted"
    assert provider_calls == 0
    assert budget.settlements == []


def test_fallback_provider_timeout_is_capped_by_reserved_goal_elapsed() -> None:
    budget = _PersistentBudget()
    provider_calls = 0

    async def generate(**_kwargs: Any) -> ChatGatewayResult:
        nonlocal provider_calls
        provider_calls += 1
        await asyncio.sleep(5)
        return _generated()

    runtime = WorkModelRuntime(
        generate=generate,
        tool_runner=None,
        model_budget=AgentLoopBudget(max_seconds=20),
        is_valid_response=lambda _text: True,
    )
    text, metadata, _audit = asyncio.run(
        runtime.interpret(
            _context(),
            preferred_profile_id=None,
            model=None,
            allow_tools=False,
            persistent_model_budget=budget,
        )
    )

    assert text == ""
    assert metadata["stop_reason"] == "provider_timeout"
    assert provider_calls == 1
    assert budget.elapsed_estimates == [20]
    assert len(budget.settlements) == 1
    assert budget.settlements[0][0] is None
    assert budget.settlements[0][1] >= 1


def test_late_fallback_result_after_cancellation_is_discarded_and_charged() -> None:
    budget = _PersistentBudget()

    async def generate(**_kwargs: Any) -> ChatGatewayResult:
        try:
            await asyncio.sleep(5)
        except asyncio.CancelledError:
            await asyncio.sleep(0.02)
            return _generated(tokens=42)
        raise AssertionError("the reserved elapsed timeout should cancel this request")

    runtime = WorkModelRuntime(
        generate=generate,
        tool_runner=None,
        model_budget=AgentLoopBudget(max_seconds=20),
        is_valid_response=lambda _text: True,
    )
    text, metadata, _audit = asyncio.run(
        runtime.interpret(
            _context(),
            preferred_profile_id=None,
            model=None,
            allow_tools=False,
            persistent_model_budget=budget,
        )
    )

    assert text == ""
    assert metadata["stop_reason"] == "provider_timeout"
    assert len(budget.settlements) == 1
    assert budget.settlements[0][0] == 42
    assert budget.settlements[0][1] >= 1


def test_tool_provider_timeout_is_capped_by_reserved_goal_elapsed() -> None:
    budget = _PersistentBudget()

    async def call_turn(**_kwargs: Any) -> ToolTurnResult:
        await asyncio.sleep(5)
        raise AssertionError("The request should time out at the reserved limit")

    runtime = WorkModelRuntime(
        generate=lambda **_kwargs: _generated(),
        tool_runner=_tool_runner(call_turn),
        model_budget=AgentLoopBudget(max_seconds=20),
        is_valid_response=lambda _text: True,
    )
    text, metadata, _audit = asyncio.run(
        runtime.interpret(
            _context(),
            preferred_profile_id=None,
            model=None,
            execution_context=AgentExecutionContext(work_agent_role="analysis"),
            persistent_model_budget=budget,
        )
    )

    assert text == ""
    assert metadata["stop_reason"] == "provider_timeout"
    assert budget.elapsed_estimates == [20]
    assert len(budget.settlements) == 1
    assert budget.settlements[0][0] is None
    assert budget.settlements[0][1] >= 1
