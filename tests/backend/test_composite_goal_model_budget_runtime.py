from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

from neuroagent.agent.gateway import ChatGatewayResult, ChatMessages, ToolTurnResult
from neuroagent.agent.loop import AgentLoopBudget
from neuroagent.agent.models import ProviderResponse
from neuroagent.agent.tool_result import ToolCallOutcome, ToolCallStatus
from neuroagent.agent.tool_spec import ToolTier
from neuroagent.application.work_model_runtime import WorkModelRuntime
from neuroagent.application.work_models import WorkInterpretation
from neuroagent.application.work_tools import (
    AgentExecutionContext,
    WorkToolLoopResult,
    WorkToolRunner,
)
from neuroagent.context.engine import RegexTokenCounter


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


def _valid_work_json(content: str) -> bool:
    try:
        WorkInterpretation.model_validate_json(content)
    except Exception:
        return False
    return True


def _generated(
    tokens: int = 42,
    attempted_profile_ids: tuple[str, ...] = ("test-profile",),
    content: str = '{"answer":"已核对","action":"conversation"}',
    profile_id: str = "test-profile",
) -> ChatGatewayResult:
    return ChatGatewayResult(
        response=ProviderResponse(
            content=content,
            model="budget-test-model",
            usage={"total_tokens": tokens},
        ),
        selected_profile_id=profile_id,
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


def test_invalid_final_json_uses_one_explicit_no_tool_deepseek_repair_and_aggregates_budget(
) -> None:
    budget = _PersistentBudget()
    first = _generated(
        tokens=12,
        content='{"answer":"untrusted malformed result","action":"conversation","extra":"secret"}',
        profile_id="selected-profile",
    )
    repaired = _generated(
        tokens=23,
        content='{"answer":"结构已修复","action":"conversation"}',
        profile_id="deepseek",
        attempted_profile_ids=("deepseek",),
    )
    calls: list[dict[str, Any]] = []
    tool_runner = AsyncMock()
    tool_runner.available.return_value = True
    tool_runner.run.return_value = WorkToolLoopResult(
        final_text="not JSON",
        stop_reason="completed",
        steps=1,
        usage={"total_tokens": 7},
        tool_names=("read_workspace_content",),
        outcomes=(
            ToolCallOutcome(
                call_id="read-1",
                name="read_workspace_content",
                tier=ToolTier.READ,
                status=ToolCallStatus.SUCCEEDED,
                result={"content": "private-workspace-sentinel"},
            ),
        ),
    )

    async def generate(**kwargs: Any) -> ChatGatewayResult:
        calls.append(kwargs)
        return first if len(calls) == 1 else repaired

    runtime = WorkModelRuntime(
        generate=generate,
        tool_runner=tool_runner,
        model_budget=AgentLoopBudget(max_steps=3),
        is_valid_response=_valid_work_json,
        response_repair_profile_id="deepseek",
    )
    context = _context()
    context["recent_messages"] = [{"role": "user", "content": "private-history-sentinel"}]
    context["pinned_context"] = [{"content": "private-pinned-sentinel"}]
    context["conversation_summary"] = "private-summary-sentinel"
    context["work_context"] = {
        "agent_role": "analysis",
        "successful_read_evidence": ["private-workspace-sentinel"],
    }
    text, metadata, _audit = asyncio.run(
        runtime.interpret(
            context,
            preferred_profile_id="selected-profile",
            model="selected-model",
            allow_tools=True,
            execution_context=AgentExecutionContext(work_agent_role="analysis"),
            persistent_model_budget=budget,
        )
    )

    assert WorkInterpretation.model_validate_json(text).answer == "结构已修复"
    assert len(calls) == 2
    tool_runner.run.assert_awaited_once()
    assert calls[0]["preferred_profile_id"] == "selected-profile"
    assert calls[0]["model"] == "selected-model"
    assert calls[1]["preferred_profile_id"] == "deepseek"
    assert calls[1]["model"] is None
    assert calls[1]["allow_web_search"] is False
    assert calls[1]["work_mode"] is True
    assert "tools" not in calls[1]
    assert "malformed result" not in calls[1]["question"]
    assert calls[1]["recent_messages"] == []
    assert calls[1]["pinned_context"] == []
    assert calls[1]["conversation_summary"] is None
    assert calls[1]["work_context"] == {"agent_role": "analysis"}
    repair_payload = repr(calls[1])
    assert "private-history-sentinel" not in repair_payload
    assert "private-pinned-sentinel" not in repair_payload
    assert "private-summary-sentinel" not in repair_payload
    assert "private-workspace-sentinel" not in repair_payload
    assert metadata["json_repair"]["valid"] is True
    assert metadata["request_budget"]["model_steps"] == 3
    assert metadata["request_budget"]["total_tokens"] == 42
    assert _audit["json_repair_used"] is True
    assert len(budget.estimates) == len(budget.settlements) == 2
    assert [item[0] for item in budget.settlements] == [12, 23]


def test_json_repair_fails_closed_when_only_one_model_step_is_available() -> None:
    calls = 0

    async def generate(**_kwargs: Any) -> ChatGatewayResult:
        nonlocal calls
        calls += 1
        return _generated(
            tokens=12,
            content='{"answer":"bad","action":"conversation","extra":"field"}',
        )

    runtime = WorkModelRuntime(
        generate=generate,
        tool_runner=None,
        model_budget=AgentLoopBudget(max_steps=1),
        is_valid_response=_valid_work_json,
        response_repair_profile_id="deepseek",
    )
    text, metadata, _audit = asyncio.run(
        runtime.interpret(
            _context(),
            preferred_profile_id="selected-profile",
            model=None,
            allow_tools=False,
        )
    )

    assert text == ""
    assert calls == 1
    assert metadata["stop_reason"] == "budget_steps"
    assert metadata["request_budget"]["model_steps"] == 1
    assert metadata["request_budget"]["total_tokens"] == 12


def test_json_repair_is_blocked_when_its_estimated_tokens_exceed_turn_budget() -> None:
    calls = 0
    context = _context()
    context["max_output_tokens"] = 32
    context_tokens = RegexTokenCounter().count(
        {
            key: value
            for key, value in context.items()
            if key not in {"context_window_tokens", "max_output_tokens"}
        }
    )

    async def generate(**_kwargs: Any) -> ChatGatewayResult:
        nonlocal calls
        calls += 1
        return _generated(tokens=1, content='{"answer":"bad","extra":"field"}')

    runtime = WorkModelRuntime(
        generate=generate,
        tool_runner=None,
        model_budget=AgentLoopBudget(
            max_steps=2,
            max_total_tokens=context_tokens + context["max_output_tokens"],
        ),
        is_valid_response=lambda _content: False,
        response_repair_profile_id="deepseek",
    )
    text, metadata, _audit = asyncio.run(
        runtime.interpret(
            context,
            preferred_profile_id="selected-profile",
            model=None,
            allow_tools=False,
        )
    )

    assert text == ""
    assert calls == 1
    assert metadata["stop_reason"] == "budget_tokens"
    assert metadata["request_budget"]["model_steps"] == 1


def test_json_repair_is_blocked_when_single_shot_uses_the_remaining_time() -> None:
    elapsed = 0.0
    calls = 0

    async def generate(**_kwargs: Any) -> ChatGatewayResult:
        nonlocal calls, elapsed
        calls += 1
        elapsed = 1.0
        return _generated(tokens=12, content='{"answer":"bad","extra":"field"}')

    runtime = WorkModelRuntime(
        generate=generate,
        tool_runner=None,
        model_budget=AgentLoopBudget(max_steps=2, max_seconds=1.0),
        is_valid_response=lambda _content: False,
        response_repair_profile_id="deepseek",
    )
    runtime._clock = lambda: elapsed
    text, metadata, _audit = asyncio.run(
        runtime.interpret(
            _context(),
            preferred_profile_id="selected-profile",
            model=None,
            allow_tools=False,
        )
    )

    assert text == ""
    assert calls == 1
    assert metadata["stop_reason"] == "budget_time"
    assert metadata["request_budget"]["model_steps"] == 1


def test_persistent_budget_exhaustion_blocks_repair_before_provider_dispatch() -> None:
    class ExhaustOnSecondReservation(_PersistentBudget):
        def reserve(self, estimated_tokens: int, estimated_elapsed_seconds: int) -> Any:
            if len(self.estimates) == 1:
                self.status = "exhausted"
            return super().reserve(estimated_tokens, estimated_elapsed_seconds)

    budget = ExhaustOnSecondReservation()
    calls = 0

    async def generate(**_kwargs: Any) -> ChatGatewayResult:
        nonlocal calls
        calls += 1
        return _generated(tokens=12, content='{"answer":"bad","extra":"field"}')

    runtime = WorkModelRuntime(
        generate=generate,
        tool_runner=None,
        model_budget=AgentLoopBudget(max_steps=2),
        is_valid_response=lambda _content: False,
        response_repair_profile_id="deepseek",
    )
    text, metadata, _audit = asyncio.run(
        runtime.interpret(
            _context(),
            preferred_profile_id="selected-profile",
            model=None,
            allow_tools=False,
            persistent_model_budget=budget,
        )
    )

    assert text == ""
    assert calls == 1
    assert metadata["stop_reason"] == "composite_goal_model_budget_exhausted"
    assert metadata["request_budget"]["model_steps"] == 1
    assert len(budget.estimates) == 2
    assert len(budget.settlements) == 1


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
