"""Mock-only routing, privacy, fallback, and model-budget tests."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from neuroagent.agent.gateway import WORK_AGENT_ROUTING_SYSTEM_PROMPT
from neuroagent.application.errors import ApplicationError
from neuroagent.application.work_routing import (
    WorkAgentRouteChoice,
    WorkAgentRoutingContext,
    WorkAgentRoutingStatus,
)
from neuroagent.infrastructure.model_work_agent_router import (
    ModelWorkAgentRouter,
    prepare_work_routing_text,
)
from neuroagent.multi_agent.registry import WorkAgentRole


class RecordingBudget:
    def __init__(self, *, statuses: list[str] | None = None):
        self.events: list[tuple[Any, ...]] = []
        self.statuses = statuses or []

    def reserve(self, estimated_tokens: int, estimated_elapsed_seconds: int) -> Any:
        self.events.append(("reserve", estimated_tokens, estimated_elapsed_seconds))
        status = self.statuses.pop(0) if self.statuses else "reserved"
        return SimpleNamespace(
            status=SimpleNamespace(value=status),
            reservation=SimpleNamespace(reserved_elapsed_seconds=estimated_elapsed_seconds),
        )

    def settle(self, reservation: Any, *, actual_tokens: int | None, elapsed_seconds: int) -> None:
        self.events.append(("settle", actual_tokens, elapsed_seconds))


def _result(
    role: str = "analysis",
    *,
    confidence: float = 0.94,
    malformed: bool = False,
    content: str | None = None,
    model: str = "qwen3.8-flash",
    profile_id: str = "qwen-router-small",
) -> Any:
    content = content or ("not json" if malformed else (
        '{"agent_role":' + f'"{role}","confidence":{confidence}' + "}"
    ))
    return SimpleNamespace(
        response=SimpleNamespace(
            content=content,
            model=model,
            usage={"prompt_tokens": 85, "completion_tokens": 12},
        ),
        selected_profile_id=profile_id,
        attempted_profile_ids=(profile_id,),
    )


def _context(**changes: Any) -> WorkAgentRoutingContext:
    values: dict[str, Any] = {
        "current_agent_role": WorkAgentRole.PREPROCESSING,
        "has_project": True,
        "has_bound_preprocessing_run": False,
        "has_selected_analysis_goal": False,
    }
    values.update(changes)
    return WorkAgentRoutingContext(**values)


def _router(generate, **kwargs: Any) -> ModelWorkAgentRouter:
    return ModelWorkAgentRouter(
        generate=generate,
        small_profile_id="qwen-router-small",
        fallback_profile_id="deepseek",
        **kwargs,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("text", "expected_role"),
    [
        ("请生成 DPABI 预处理方案", WorkAgentRole.PREPROCESSING),
        ("请做两组统计比较并解释 ALFF 结果", WorkAgentRole.ANALYSIS),
    ],
)
async def test_clear_requests_are_routed_by_rules_without_model_calls(text, expected_role):
    async def generate(**_kwargs: Any) -> Any:
        raise AssertionError("clear intents should not call a model")

    decision = await _router(generate).choose(
        request_text=text, context=_context(), persistent_model_budget=None
    )

    assert decision.status is WorkAgentRoutingStatus.ROUTED
    assert decision.role is expected_role
    assert decision.source == "rule"
    assert decision.model == "rules"


@pytest.mark.asyncio
async def test_composite_goal_first_stage_is_a_server_rule_and_skips_models():
    async def generate(**_kwargs: Any) -> Any:
        raise AssertionError("the server-locked first stage should not call a model")

    decision = await _router(generate).choose(
        request_text="先预处理, 再比较两组 ALFF。",
        context=_context(required_initial_role=WorkAgentRole.PREPROCESSING),
        persistent_model_budget=None,
    )

    assert decision.role is WorkAgentRole.PREPROCESSING
    assert decision.source == "rule"
    assert decision.reason == "required_initial_role"


@pytest.mark.asyncio
async def test_small_model_handles_ambiguous_request_with_work_routing_contract():
    calls: list[dict[str, Any]] = []

    async def generate(**kwargs: Any) -> Any:
        calls.append(kwargs)
        return _result()

    budget = RecordingBudget()
    decision = await _router(generate).choose(
        request_text="我想分析这些数据",
        context=_context(),
        persistent_model_budget=budget,
    )

    assert decision.role is WorkAgentRole.ANALYSIS
    assert decision.source == "small_model"
    assert decision.profile_id == "qwen-router-small"
    assert len(calls) == 1
    assert calls[0]["preferred_profile_id"] == "qwen-router-small"
    assert calls[0]["work_agent_routing"] is True
    assert "routing" not in calls[0]
    assert calls[0]["allow_web_search"] is False
    assert calls[0]["recent_messages"] == []
    assert calls[0]["pinned_context"] == []
    assert set(calls[0]["work_context"]) == {
        "current_agent",
        "has_project",
        "has_bound_preprocessing_run",
        "has_selected_analysis_goal",
        "server_required_initial_agent",
    }
    assert calls[0]["system_prompt"] == WORK_AGENT_ROUTING_SYSTEM_PROMPT
    assert '"agent_role":"preprocessing|analysis|clarify"' in WORK_AGENT_ROUTING_SYSTEM_PROMPT
    assert "agent_role" in calls[0]["system_prompt"]
    assert [event[0] for event in budget.events] == ["reserve", "settle"]


@pytest.mark.asyncio
async def test_low_confidence_small_model_uses_large_model_fallback_and_shared_budget():
    calls: list[str] = []

    async def generate(**kwargs: Any) -> Any:
        calls.append(str(kwargs["preferred_profile_id"]))
        if len(calls) == 1:
            return _result("analysis", confidence=0.42)
        return _result(
            "preprocessing",
            confidence=0.91,
            model="deepseek-v4-flash",
            profile_id="deepseek",
        )

    budget = RecordingBudget()
    decision = await _router(generate).choose(
        request_text="我想分析这些数据",
        context=_context(),
        persistent_model_budget=budget,
    )

    assert calls == ["qwen-router-small", "deepseek"]
    assert decision.role is WorkAgentRole.PREPROCESSING
    assert decision.source == "large_model_fallback"
    assert decision.reason == "large_model_fallback"
    assert decision.model == "deepseek-v4-flash"
    assert [event[0] for event in budget.events] == ["reserve", "settle", "reserve", "settle"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "small_failure",
    ["invalid_json", "invalid_schema_extra", "invalid_schema_confidence", "provider_error"],
)
async def test_invalid_or_failed_small_model_falls_back_to_large(small_failure: str):
    calls: list[str] = []

    async def generate(**kwargs: Any) -> Any:
        calls.append(str(kwargs["preferred_profile_id"]))
        if len(calls) == 1:
            if small_failure == "provider_error":
                raise ApplicationError("chat_model_unavailable", "safe error")
            if small_failure == "invalid_schema_extra":
                return _result(content='{"agent_role":"analysis","confidence":0.9,"extra":true}')
            if small_failure == "invalid_schema_confidence":
                return _result(content='{"agent_role":"analysis","confidence":1.1}')
            return _result(malformed=True)
        return _result(
            "analysis",
            confidence=0.9,
            model="deepseek-v4-flash",
            profile_id="deepseek",
        )

    decision = await _router(generate).choose(
        request_text="我想分析这些数据",
        context=_context(),
        persistent_model_budget=None,
    )

    assert calls == ["qwen-router-small", "deepseek"]
    assert decision.role is WorkAgentRole.ANALYSIS
    assert decision.source == "large_model_fallback"


@pytest.mark.asyncio
async def test_confident_small_model_clarification_skips_fallback():
    calls = 0

    async def generate(**_kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        return _result("clarify", confidence=0.99)

    decision = await _router(generate).choose(
        request_text="我想分析这些数据",
        context=_context(),
        persistent_model_budget=None,
    )

    assert calls == 1
    assert decision.status is WorkAgentRoutingStatus.NEEDS_CLARIFICATION
    assert decision.role is None
    assert decision.choice is WorkAgentRouteChoice.CLARIFY
    assert decision.reason == "model_clarify"


@pytest.mark.asyncio
async def test_uncertain_large_fallback_requests_clarification():
    calls = 0

    async def generate(**_kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        if calls == 1:
            return _result("analysis", confidence=0.42)
        return _result("clarify", confidence=0.99, model="deepseek-v4-flash", profile_id="deepseek")

    decision = await _router(generate).choose(
        request_text="我想分析这些数据",
        context=_context(),
        persistent_model_budget=None,
    )

    assert calls == 2
    assert decision.status is WorkAgentRoutingStatus.NEEDS_CLARIFICATION
    assert decision.role is None
    assert decision.choice is WorkAgentRouteChoice.CLARIFY
    assert decision.reason == "large_model_uncertain"


@pytest.mark.asyncio
async def test_both_models_failing_returns_safe_application_error():
    calls = 0

    async def generate(**_kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        raise ApplicationError("chat_model_unavailable", "sensitive provider error")

    with pytest.raises(ApplicationError) as error:
        await _router(generate).choose(
            request_text="我想分析这些数据",
            context=_context(),
            persistent_model_budget=None,
        )

    assert calls == 2
    assert error.value.code == "work_agent_router_unavailable"
    assert "sensitive provider error" not in str(error.value)


@pytest.mark.parametrize(
    "text",
    [
        "participant_id\tgroup\nage-001\tASD\nage-002\tHC",
        "sub-001 belongs to ASD",
        "data:image/png;base64," + "A" * 300,
        "copied MRI payload: " + "A" * 300,
    ],
)
def test_subject_tables_group_mappings_and_image_payloads_are_rejected(text: str):
    with pytest.raises(ApplicationError) as error:
        prepare_work_routing_text(text)
    assert error.value.code == "work_agent_router_sensitive_context_blocked"


@pytest.mark.parametrize(
    "private_path",
    [
        r"C:\Users\Jane Doe\private folder\rest.nii.gz",
        "/home/jane doe/private folder/rest.nii.gz",
    ],
)
def test_full_absolute_paths_with_spaces_are_redacted(private_path: str):
    safe = prepare_work_routing_text(f"Input: {private_path}, compare the maps.")
    assert private_path not in safe
    assert "Jane Doe" not in safe
    assert "jane doe" not in safe
    assert "rest.nii.gz" not in safe


@pytest.mark.asyncio
async def test_router_does_not_send_sensitive_text_to_model():
    called = False

    async def generate(**_kwargs: Any) -> Any:
        nonlocal called
        called = True
        return _result()

    with pytest.raises(ApplicationError):
        await _router(generate).choose(
            request_text="subject_id,group\nsub-01,ASD\nsub-02,HC",
            context=_context(),
            persistent_model_budget=None,
        )
    assert not called


@pytest.mark.asyncio
async def test_model_route_calls_share_composite_budget_and_stop_if_fallback_cannot_reserve():
    async def generate(**_kwargs: Any) -> Any:
        return _result("analysis", confidence=0.3)

    budget = RecordingBudget(statuses=["reserved", "exhausted"])
    with pytest.raises(ApplicationError) as error:
        await _router(generate).choose(
            request_text="我想分析这些数据",
            context=_context(),
            persistent_model_budget=budget,
        )

    assert error.value.code == "composite_goal_model_budget_exhausted"
    assert [event[0] for event in budget.events] == ["reserve", "settle", "reserve"]


@pytest.mark.asyncio
async def test_cancellation_settles_reserved_model_call_and_propagates():
    started = asyncio.Event()
    budget = RecordingBudget()

    async def generate(**_kwargs: Any) -> Any:
        started.set()
        await asyncio.Event().wait()

    task = asyncio.create_task(
        _router(generate, timeout_seconds=30).choose(
            request_text="我想分析这些数据",
            context=_context(),
            persistent_model_budget=budget,
        )
    )
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert [event[0] for event in budget.events] == ["reserve", "settle"]
