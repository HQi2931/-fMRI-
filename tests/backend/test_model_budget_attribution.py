"""Ledger attribution for ordinary Work, routing, replay and exhaustion."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from neuroagent.application.automation.composite_goal import CompositeGoalIdentity
from neuroagent.application.automation.model_budget import CompositeGoalModelCallDecisionStatus
from neuroagent.application.automation.model_budget_attribution import (
    MODEL_CALL_ATTRIBUTION,
    ModelBudgetLedger,
    attribution_by_entry,
)
from neuroagent.application.contracts.conversations import (
    ConversationCreate,
    ConversationTurnCreate,
    ConversationWorkTaskView,
    WorkAgentSelection,
)
from neuroagent.application.conversation_work import WorkActionResult
from neuroagent.application.hashing import content_hash
from neuroagent.application.work_agents import analysis_dependency_for_request
from neuroagent.application.work_dialogue import WorkDialogue
from neuroagent.infrastructure.model_work_agent_router import ModelWorkAgentRouter
from neuroagent.multi_agent.registry import WorkAgentRole
from neuroagent.shared.errors import NotFoundError

from .test_work_dialogue import configure, generated


def test_attribution_table_names_each_production_entry_once() -> None:
    entries = attribution_by_entry()
    assert set(entries) == {item.entry for item in MODEL_CALL_ATTRIBUTION}
    assert len(entries) == len(MODEL_CALL_ATTRIBUTION)
    ordinary = entries["ordinary_work_without_composite_goal"]
    leaving = entries["auto_route_while_existing_goal_may_be_left"]
    admitted = entries["auto_route_admitted_preprocess_then_analyze"]
    assert ordinary.ledgers == (ModelBudgetLedger.TURN,)
    assert leaving.ledgers == (ModelBudgetLedger.TURN,)
    assert leaving.charges_goal_before_role_decision is False
    assert admitted.charges_goal_before_role_decision is True
    assert ModelBudgetLedger.GOAL in admitted.ledgers
    for item in MODEL_CALL_ATTRIBUTION:
        assert ModelBudgetLedger.TURN in item.ledgers


@pytest.mark.asyncio
async def test_ordinary_work_uses_turn_budget_and_does_not_open_a_goal_ledger(
    service, source_root, work_root, monkeypatch
):
    from neuroagent.application.contracts.projects import ProjectCreate

    project = service.projects.create_project(
        ProjectCreate(
            name="ordinary budget",
            source_roots=[str(source_root)],
            work_root=str(work_root / "ordinary-budget"),
        ),
        "ordinary-budget-project",
    )
    conversation = service.conversations.create_conversation(
        ConversationCreate(mode="work", project_id=project.project_id),
        "ordinary-budget-conversation",
    )
    router_generate = AsyncMock(
        side_effect=AssertionError("a clear rule must not call a router model")
    )
    service.conversations.work_agent_router = ModelWorkAgentRouter(
        generate=router_generate,
        small_profile_id="small",
        fallback_profile_id="fallback",
    )
    captured: dict[str, object] = {}

    async def respond(_self, _conversation, request, _packet, **kwargs):
        captured["budget"] = kwargs["persistent_model_budget"]
        captured["role"] = request.agent_role
        return WorkActionResult("已按预处理规则接收。", payload={})

    monkeypatch.setattr(WorkDialogue, "respond", respond)
    await service.conversations.send_conversation_turn(
        conversation.conversation_id,
        ConversationTurnCreate(content="准备新的预处理方案"),
        "ordinary-budget-turn",
    )

    assert captured["budget"] is None
    assert captured["role"] is WorkAgentRole.PREPROCESSING
    router_generate.assert_not_called()
    with pytest.raises(NotFoundError):
        service.automation.get_composite_goal_model_budget("ordinary-budget-turn")


def _selected_analysis_goal(service, source_root, work_root, name: str):
    from neuroagent.application.contracts.projects import ProjectCreate

    project = service.projects.create_project(
        ProjectCreate(
            name=name,
            source_roots=[str(source_root)],
            work_root=str(work_root / name),
        ),
        f"{name}-project",
    )
    conversation = service.conversations.create_conversation(
        ConversationCreate(mode="work", project_id=project.project_id),
        f"{name}-conversation",
    )
    original = "先预处理，然后比较两组 ALFF。"
    intent = analysis_dependency_for_request(
        original,
        project.project_id,
        conversation.conversation_id,
        f"{name}-goal",
    )
    assert intent is not None
    goal = CompositeGoalIdentity(
        intent_task_id=str(intent["task_id"]),
        conversation_id=conversation.conversation_id,
        project_id=project.project_id,
        analysis_request_hash=content_hash(original),
    )
    service.automation.ensure_preprocessing_composite_goal_intent(goal)
    import neuroagent.application.service_mixins.conversations as conversation_module

    actual = conversation_module.conversation_work_task
    binding = ConversationWorkTaskView(
        agent_role=WorkAgentRole.ANALYSIS,
        work_task_id=f"{name}-analysis-task",
        analysis_intent_task_id=goal.intent_task_id,
    )

    def selected(view, role):
        if view.conversation_id == conversation.conversation_id and role is WorkAgentRole.ANALYSIS:
            return binding
        return actual(view, role)

    return conversation, goal, selected


@pytest.mark.asyncio
async def test_same_turn_key_replays_goal_reservation_without_a_second_provider_call(
    service, source_root, work_root, monkeypatch
):
    conversation, goal, selected = _selected_analysis_goal(
        service, source_root, work_root, "budget-replay"
    )
    monkeypatch.setattr(
        "neuroagent.application.service_mixins.conversations.conversation_work_task",
        selected,
    )
    service.conversations.select_work_agent(
        conversation.conversation_id,
        WorkAgentSelection(
            agent_role=WorkAgentRole.ANALYSIS, expected_version=conversation.version
        ),
        "budget-replay-select",
    )
    service.conversations.work_agent_router = ModelWorkAgentRouter(
        generate=AsyncMock(side_effect=AssertionError("continuation is a local rule")),
        small_profile_id="small",
        fallback_profile_id="fallback",
    )
    monkeypatch.setattr(
        "neuroagent.application.work_dialogue._consumed_analysis_intent",
        lambda *_args, **_kwargs: None,
    )
    model = configure(
        service, monkeypatch, [generated("conversation", answer="已保留当前分析目标。")]
    )

    request = ConversationTurnCreate(content="继续")
    first = await service.conversations.send_conversation_turn(
        conversation.conversation_id, request, "budget-replay-turn"
    )
    charged = service.automation.get_composite_goal_model_budget(goal.intent_task_id)
    assert charged.used_model_calls >= 1
    calls_after_first = charged.used_model_calls
    tokens_after_first = charged.used_tokens

    second = await service.conversations.send_conversation_turn(
        conversation.conversation_id, request, "budget-replay-turn"
    )
    replayed = service.automation.get_composite_goal_model_budget(goal.intent_task_id)

    assert model.await_count == 1
    assert replayed.used_model_calls == calls_after_first
    assert replayed.used_tokens == tokens_after_first
    assert second.assistant_message.content == first.assistant_message.content
    assert second.assistant_message.message_id == first.assistant_message.message_id


@pytest.mark.asyncio
async def test_exhausted_goal_budget_does_not_dispatch_foreground_generation(
    service, source_root, work_root, monkeypatch
):
    conversation, goal, selected = _selected_analysis_goal(
        service, source_root, work_root, "budget-exhausted"
    )
    monkeypatch.setattr(
        "neuroagent.application.service_mixins.conversations.conversation_work_task",
        selected,
    )
    service.conversations.select_work_agent(
        conversation.conversation_id,
        WorkAgentSelection(
            agent_role=WorkAgentRole.ANALYSIS, expected_version=conversation.version
        ),
        "budget-exhausted-select",
    )
    filled = service.automation.reserve_composite_goal_model_call(
        goal.intent_task_id,
        reservation_id=content_hash({"reservation": goal.intent_task_id}),
        call_id=content_hash({"call": goal.intent_task_id}),
        estimated_tokens=200_000,
    )
    assert filled.status is CompositeGoalModelCallDecisionStatus.RESERVED
    service.conversations.work_agent_router = ModelWorkAgentRouter(
        generate=AsyncMock(side_effect=AssertionError("continuation is a local rule")),
        small_profile_id="small",
        fallback_profile_id="fallback",
    )
    monkeypatch.setattr(
        "neuroagent.application.work_dialogue._consumed_analysis_intent",
        lambda *_args, **_kwargs: None,
    )
    model = configure(service, monkeypatch, [generated("conversation")])

    turn = await service.conversations.send_conversation_turn(
        conversation.conversation_id,
        ConversationTurnCreate(content="继续"),
        "budget-exhausted-turn",
    )
    budget = service.automation.get_composite_goal_model_budget(goal.intent_task_id)

    model.assert_not_called()
    assert budget.used_model_calls == 1
    assert budget.used_tokens == 200_000
    assert "累计模型预算已用完" in turn.assistant_message.content
