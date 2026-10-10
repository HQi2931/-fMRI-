from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from neuroagent.agent.gateway import ChatGatewayResult
from neuroagent.agent.loop import AgentLoopBudget
from neuroagent.agent.models import ProviderResponse
from neuroagent.application.work_tools import AgentExecutionContext, WorkToolLoopResult


def _context(role):
    return {
        "question": "Discuss the current task",
        "recent_messages": [],
        "pinned_context": [],
        "conversation_summary": None,
        "work_context": {"agent_role": role},
        "context_window_tokens": 16000,
        "max_output_tokens": 2048,
    }


def _generated(tokens=200):
    return ChatGatewayResult(
        response=ProviderResponse(
            content='{"answer":"Review","action":"conversation"}',
            model="test-model",
            usage={"total_tokens": tokens},
        ),
        selected_profile_id="test-profile",
        context_hash="a" * 64,
        attempted_profile_ids=("test-profile",),
        remote_search_used=False,
        redaction_count=0,
    )


@pytest.mark.parametrize("role", ["preprocessing", "analysis"])
@pytest.mark.parametrize("limit", ["steps", "tokens"])
def test_both_roles_share_budget_with_structural_fallback(service, role, limit):
    runner = AsyncMock()
    runner.available.return_value = True
    runner.run.return_value = WorkToolLoopResult(
        final_text="Unstructured response",
        stop_reason="completed",
        steps=1,
        usage={"total_tokens": 9500 if limit == "tokens" else 100},
    )
    service.work_dialogue.tool_runner = runner
    service.work_dialogue.generate = AsyncMock(side_effect=AssertionError("No new budget"))
    service.work_dialogue.model_budget = AgentLoopBudget(
        max_steps=1 if limit == "steps" else 4,
        max_total_tokens=10000,
    )
    raw, metadata, audit = asyncio.run(
        service.work_dialogue._interpret(
            _context(role),
            preferred_profile_id=None,
            model=None,
            execution_context=AgentExecutionContext(work_agent_role=role),
        )
    )
    assert raw == "" and metadata["stop_reason"] == f"budget_{limit}"
    assert audit["steps"] == 1 and audit["fallback_used"] is False
    service.work_dialogue.generate.assert_not_called()
    assert runner.run.call_args.kwargs["request_budget"].max_total_tokens == 10000


def test_fallback_uses_remaining_deadline_and_preserves_tool_evidence(service):
    runner = AsyncMock()
    runner.available.return_value = True

    async def loop(**kwargs):
        await asyncio.sleep(0.01)
        return WorkToolLoopResult(
            final_text="Unstructured response",
            stop_reason="completed",
            steps=1,
            usage={"total_tokens": 100},
        )

    async def generation(**kwargs):
        await asyncio.sleep(1)
        raise AssertionError("Fallback should have been cancelled")

    runner.run.side_effect = loop
    service.work_dialogue.tool_runner = runner
    service.work_dialogue.generate = AsyncMock(side_effect=generation)
    service.work_dialogue.model_budget = AgentLoopBudget(max_seconds=0.05)
    raw, metadata, audit = asyncio.run(
        service.work_dialogue._interpret(
            _context("analysis"),
            preferred_profile_id=None,
            model=None,
            execution_context=AgentExecutionContext(work_agent_role="analysis"),
        )
    )
    assert raw == "" and metadata["stop_reason"] == "provider_timeout"
    assert audit["steps"] == 1
    assert service.work_dialogue.generate.await_count == 1
    assert runner.run.call_args.kwargs["request_budget"].max_seconds <= 0.05


def test_successful_fallback_records_combined_cost_and_one_additional_turn(service):
    runner = AsyncMock()
    runner.available.return_value = True
    runner.run.return_value = WorkToolLoopResult(
        final_text="Unstructured", stop_reason="completed", steps=2, usage={"total_tokens": 100}
    )
    service.work_dialogue.tool_runner = runner
    service.work_dialogue.generate = AsyncMock(return_value=_generated())
    raw, metadata, audit = asyncio.run(
        service.work_dialogue._interpret(
            _context("analysis"),
            preferred_profile_id=None,
            model=None,
            execution_context=AgentExecutionContext(work_agent_role="analysis"),
        )
    )
    assert raw and audit["fallback_used"] is True
    assert audit["request_model_steps"] == 3 and audit["request_total_tokens"] == 300
    assert metadata["request_budget"]["total_tokens"] == 300


def test_single_shot_budget_is_checked_before_provider_call(service):
    service.work_dialogue.tool_runner = None
    service.work_dialogue.generate = AsyncMock(side_effect=AssertionError("Budget too small"))
    service.work_dialogue.model_budget = AgentLoopBudget(max_total_tokens=100)
    raw, metadata, audit = asyncio.run(
        service.work_dialogue._interpret(
            _context("preprocessing"),
            preferred_profile_id=None,
            model=None,
            execution_context=AgentExecutionContext(work_agent_role="preprocessing"),
        )
    )
    assert not raw and metadata["stop_reason"] == "budget_tokens" and audit is None
    service.work_dialogue.generate.assert_not_called()


def test_capability_lookup_is_inside_the_same_request_deadline(service):
    runner = AsyncMock()

    async def availability(profile):
        await asyncio.sleep(1)
        return True

    runner.available.side_effect = availability
    service.work_dialogue.tool_runner = runner
    service.work_dialogue.model_budget = AgentLoopBudget(max_seconds=0.05)
    service.work_dialogue.generate = AsyncMock(side_effect=AssertionError("No reset"))
    raw, metadata, audit = asyncio.run(
        service.work_dialogue._interpret(
            _context("analysis"),
            preferred_profile_id=None,
            model=None,
            execution_context=AgentExecutionContext(work_agent_role="analysis"),
        )
    )
    assert not raw and metadata["stop_reason"] == "budget_time" and audit is None
    runner.run.assert_not_called()
    service.work_dialogue.generate.assert_not_called()
