"""Intent routing, continuation and ambiguity across the two Work roles."""

# ruff: noqa: RUF001

from __future__ import annotations

import pytest

from neuroagent.api.schemas.conversations import ConversationTurnCreate as ApiConversationTurnCreate
from neuroagent.application.automation.analysis_outbox import AnalysisProposalOutbox
from neuroagent.application.contracts.conversations import (
    ConversationCreate,
    ConversationTurnCreate,
    ConversationWorkTaskView,
    WorkAgentSelection,
)
from neuroagent.application.conversation_work import WorkActionResult
from neuroagent.application.hashing import content_hash
from neuroagent.application.work_agents import (
    analysis_dependency_for_request,
    clear_analysis_intent_envelope,
    starts_new_preprocessing_task,
    work_agent_role_for_turn,
    work_agent_routing_question,
)
from neuroagent.application.work_routing import (
    WorkAgentRouteChoice,
    WorkAgentRoutingContext,
    WorkAgentRoutingDecision,
    WorkAgentRoutingStatus,
)
from neuroagent.multi_agent.registry import WorkAgentRole

from .test_work_dialogue import configure, generated


@pytest.mark.parametrize("prior", ["preprocessing", "analysis"])
@pytest.mark.parametrize(
    ("content", "expected"),
    [
        ("计算 ALFF", "preprocessing"),
        ("分析预处理失败原因", "preprocessing"),
        ("分析为什么 ALFF 计算失败", "preprocessing"),
        ("analyze preprocessing logs", "preprocessing"),
        ("diagnose failed ReHo computation", "preprocessing"),
        ("preprocess these images", "preprocessing"),
        ("compare ASD and HC ALFF maps", "analysis"),
        ("two-sample t-test with age as a covariate", "analysis"),
        ("correlation between age and ALFF", "analysis"),
        ("multiple regression of these metrics", "analysis"),
        ("localize the significant clusters", "analysis"),
        ("DPABI two-sample t-test failed", "analysis"),
        ("DPABI FDR 校正报错", "analysis"),
        ("比较两组 ReHo 数据", "analysis"),
        ("先预处理再比较两组", "preprocessing"),
        ("first compute ALFF then compare ASD and HC", "preprocessing"),
        (
            "请核查 ALFF/fALFF 与 ReHo 是否可同批运行, 分析当前 DPARSFA 合同后, "
            "为项目准备新的待审 DPABI 预处理方案, 仅包含 ALFF/fALFF, 不做统计分析.",
            "preprocessing",
        ),
    ],
)
def test_new_intent_routes_from_either_current_role(service, prior, content, expected):
    conversation = service.conversations.create_conversation(
        ConversationCreate(mode="work"), "route"
    )
    analysis_task = ConversationWorkTaskView(
        agent_role=WorkAgentRole.ANALYSIS,
        work_task_id="previous-analysis-task",
        analysis_intent_task_id="previous-analysis-goal",
    )
    conversation = conversation.model_copy(
        update={
            "foreground_agent_role": WorkAgentRole(prior),
            "work_tasks": (analysis_task,) if prior == "analysis" else (),
        }
    )
    assert (
        work_agent_role_for_turn(content, requested=None, conversation=conversation).value
        == expected
    )
    assert work_agent_routing_question(content, requested=None, conversation=conversation) is None


@pytest.mark.parametrize("content", ["继续", "查看结果", "continue", "show results", "Next step."])
@pytest.mark.parametrize("prior", ["preprocessing", "analysis"])
def test_continuation_keeps_the_current_bound_task(service, content, prior):
    conversation = service.conversations.create_conversation(
        ConversationCreate(mode="work"), "continue"
    )
    conversation = conversation.model_copy(
        update={"foreground_agent_role": WorkAgentRole(prior), "active_run_id": "bound-run"}
    )
    assert (
        work_agent_role_for_turn(content, requested=None, conversation=conversation).value == prior
    )
    assert work_agent_routing_question(content, requested=None, conversation=conversation) is None


@pytest.mark.parametrize(
    "content", ["先预处理再比较两组", "first preprocess images then compare groups"]
)
def test_two_stage_intent_records_dependency_without_scientific_defaults(content):
    dependency = analysis_dependency_for_request(content, "project", "conversation", "key")
    assert dependency["status"] == "waiting_for_preprocessing"
    assert dependency["analysis_request"] == content
    assert "design" not in dependency and "correction" not in dependency
    assert dependency == analysis_dependency_for_request(content, "project", "conversation", "key")
    assert (
        dependency["task_id"]
        != analysis_dependency_for_request(content, "project", "other-conversation", "key")[
            "task_id"
        ]
    )
    assert (
        dependency["task_id"]
        != analysis_dependency_for_request(content, "project", "conversation", "other-key")[
            "task_id"
        ]
    )


@pytest.mark.parametrize("turn_model", [ConversationTurnCreate, ApiConversationTurnCreate])
def test_authoritative_analysis_sources_ignore_conflicting_agent_role(turn_model):
    selected_goal = turn_model(
        content="继续当前统计分析",
        analysis_intent_task_id="selected-analysis-goal",
        agent_role=WorkAgentRole.PREPROCESSING,
    )
    selected_preprocessing_source = turn_model(
        content="分析这个预处理结果",
        preprocessing_task_id="selected-preprocessing-task",
        target_run_id="selected-run",
        agent_role=WorkAgentRole.PREPROCESSING,
    )
    assert selected_goal.agent_role is WorkAgentRole.PREPROCESSING
    assert selected_preprocessing_source.preprocessing_task_id == "selected-preprocessing-task"


def test_new_preprocessing_goal_clears_intent_even_without_a_draft_payload():
    payload: dict[str, object] = {"agent_role": "preprocessing"}
    assert starts_new_preprocessing_task("请对这批数据进行常规预处理。")
    clear_analysis_intent_envelope(payload, conversation_id="conversation", project_id="project")
    assert payload["analysis_intent_envelope"] == {
        "status": "cleared",
        "conversation_id": "conversation",
        "project_id": "project",
    }


def test_same_project_same_request_outboxes_are_independent_per_conversation(service):
    text = "先预处理这批静息态 fMRI 数据，然后对 ALFF 做两组统计比较。"
    first_conversation = service.conversations.create_conversation(
        ConversationCreate(mode="work"), "conversation-a"
    )
    second_conversation = service.conversations.create_conversation(
        ConversationCreate(mode="work"), "conversation-b"
    )
    first = analysis_dependency_for_request(
        text, "project", first_conversation.conversation_id, "same-key"
    )
    second = analysis_dependency_for_request(
        text, "project", second_conversation.conversation_id, "same-key"
    )
    assert first is not None and second is not None
    assert first["task_id"] != second["task_id"]
    common = {
        "project_id": "project",
        "plan_revision_id": "plan",
        "manifest_hash": "a" * 64,
        "analysis_request_hash": content_hash(text),
    }
    first_row = service.automation._repository.ensure_analysis_outbox(
        AnalysisProposalOutbox(
            intent_task_id=str(first["task_id"]),
            conversation_id=first_conversation.conversation_id,
            preprocessing_task_id="preprocessing-a",
            run_id="run-a",
            **common,
        )
    )
    second_row = service.automation._repository.ensure_analysis_outbox(
        AnalysisProposalOutbox(
            intent_task_id=str(second["task_id"]),
            conversation_id=second_conversation.conversation_id,
            preprocessing_task_id="preprocessing-b",
            run_id="run-b",
            **common,
        )
    )
    assert first_row.intent_task_id != second_row.intent_task_id


@pytest.mark.asyncio
@pytest.mark.parametrize("content", ["继续", "continue", "分析一下", "analyze the data"])
async def test_unbound_ambiguous_request_asks_without_model_or_task_creation(
    service, monkeypatch, content
):
    conversation = service.conversations.create_conversation(
        ConversationCreate(mode="work"), "ambiguous"
    )
    model = configure(service, monkeypatch, [generated("prepare_statistical_analysis")])
    turn = await service.conversations.send_conversation_turn(
        conversation.conversation_id,
        ConversationTurnCreate(content=content, preferred_profile_id="dialogue-model"),
        "ambiguous-turn",
    )
    assert turn.assistant_message.payload["agent_routing"]["status"] == "needs_clarification"
    assert (
        "预处理" in turn.assistant_message.content and "统计分析" in turn.assistant_message.content
    )
    assert turn.conversation.foreground_agent_role is WorkAgentRole.PREPROCESSING
    assert all(
        not (task.draft_hash or task.proposal_hash or task.plan_revision_id or task.run_id)
        for task in turn.conversation.work_tasks
    )
    assert not service.runs.list_runs() and turn.tool_call is None
    model.assert_not_called()


def test_trusted_internal_role_can_resolve_ambiguous_continuation(service):
    conversation = service.conversations.create_conversation(
        ConversationCreate(mode="work"), "manual"
    )
    assert (
        work_agent_role_for_turn(
            "continue", requested=WorkAgentRole.ANALYSIS, conversation=conversation
        )
        is WorkAgentRole.ANALYSIS
    )
    assert (
        work_agent_routing_question(
            "continue", requested=WorkAgentRole.ANALYSIS, conversation=conversation
        )
        is None
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("content", "caller_role", "expected_role"),
    [
        ("准备预处理方案", WorkAgentRole.ANALYSIS, WorkAgentRole.PREPROCESSING),
        ("比较两组 ALFF 指标", WorkAgentRole.PREPROCESSING, WorkAgentRole.ANALYSIS),
    ],
)
async def test_work_auto_router_ignores_conflicting_caller_agent_role(
    service, monkeypatch, content, caller_role, expected_role
):
    conversation = service.conversations.create_conversation(
        ConversationCreate(mode="work"), "auto-route-conflicting-role"
    )
    from neuroagent.application.work_dialogue import WorkDialogue

    async def reply(_self, _conversation, request, _packet, **_kwargs):
        assert request.agent_role is expected_role
        return WorkActionResult(
            "已交给自动分派的 Agent。",
            payload={"agent_role": request.agent_role.value},
        )

    monkeypatch.setattr(WorkDialogue, "respond", reply)
    turn = await service.conversations.send_conversation_turn(
        conversation.conversation_id,
        ConversationTurnCreate(content=content, agent_role=caller_role),
        f"conflicting-role-{expected_role.value}",
    )
    assert turn.assistant_message.payload["agent_role"] == expected_role.value
    assert turn.conversation.foreground_agent_role is expected_role


@pytest.mark.asyncio
async def test_composite_root_and_shared_budget_exist_before_router_and_reach_work_generation(
    service, source_root, work_root, monkeypatch
):
    from neuroagent.application.contracts.projects import ProjectCreate
    from neuroagent.application.hashing import content_hash
    from neuroagent.application.work_dialogue import WorkDialogue

    project = service.projects.create_project(
        ProjectCreate(
            name="routing target",
            source_roots=[str(source_root)],
            work_root=str(work_root / "routing-target"),
        ),
        "routing-target-project",
    )
    conversation = service.conversations.create_conversation(
        ConversationCreate(mode="work", project_id=project.project_id), "router-composite"
    )
    text = "先预处理这批数据，再比较两组 ALFF。"
    intent = analysis_dependency_for_request(
        text,
        project.project_id,
        conversation.conversation_id,
        content_hash("router-composite-turn"),
    )
    assert intent is not None
    captured: dict[str, object] = {}

    class RecordingRouter:
        model = "mock-router"

        async def choose(
            self,
            *,
            request_text: str,
            context: WorkAgentRoutingContext,
            persistent_model_budget,
        ):
            assert request_text == text
            assert context.required_initial_role is WorkAgentRole.PREPROCESSING
            assert service.automation.find_composite_goal(str(intent["task_id"])) is not None
            assert (
                service.automation.get_composite_goal_model_budget(
                    str(intent["task_id"])
                ).used_model_calls
                == 0
            )
            captured["budget"] = persistent_model_budget
            reservation = persistent_model_budget.reserve(12, 1)
            assert reservation.status.value == "reserved"
            persistent_model_budget.settle(reservation, actual_tokens=8, elapsed_seconds=1)
            return WorkAgentRoutingDecision(
                status=WorkAgentRoutingStatus.ROUTED,
                role=WorkAgentRole.PREPROCESSING,
                choice=WorkAgentRouteChoice.PREPROCESSING,
                confidence=0.99,
                model=self.model,
            )

    service.conversations.work_agent_router = RecordingRouter()

    async def respond(_self, _conversation, request, _packet, **kwargs):
        assert request.agent_role is WorkAgentRole.PREPROCESSING
        assert kwargs["persistent_model_budget"] is captured["budget"]
        reservation = kwargs["persistent_model_budget"].reserve(12, 1)
        assert reservation.status.value == "reserved"
        kwargs["persistent_model_budget"].settle(reservation, actual_tokens=8, elapsed_seconds=1)
        return WorkActionResult(
            "已交给预处理 Agent。", payload={"agent_role": WorkAgentRole.PREPROCESSING.value}
        )

    monkeypatch.setattr(WorkDialogue, "respond", respond)
    turn = await service.conversations.send_conversation_turn(
        conversation.conversation_id,
        ConversationTurnCreate(content=text, agent_role=WorkAgentRole.ANALYSIS),
        "router-composite-turn",
    )
    assert turn.assistant_message.payload["agent_routing"]["source"] == "model"
    assert turn.assistant_message.payload["agent_routing"]["model"] == "mock-router"
    budget = service.automation.get_composite_goal_model_budget(str(intent["task_id"]))
    assert budget.used_model_calls == 2
    assert turn.conversation.foreground_agent_role is WorkAgentRole.PREPROCESSING


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("current_role", "routed_role"),
    [
        (WorkAgentRole.ANALYSIS, WorkAgentRole.PREPROCESSING),
        (WorkAgentRole.PREPROCESSING, WorkAgentRole.ANALYSIS),
    ],
)
async def test_automatic_role_switch_uses_only_the_destination_goal_budget(
    service, source_root, work_root, monkeypatch, current_role, routed_role
):
    from neuroagent.application.automation.composite_goal import CompositeGoalIdentity
    from neuroagent.application.contracts.projects import ProjectCreate
    from neuroagent.application.work_dialogue import WorkDialogue

    project = service.projects.create_project(
        ProjectCreate(
            name="budget switch",
            source_roots=[str(source_root)],
            work_root=str(work_root / "budget-switch"),
        ),
        f"budget-switch-{current_role.value}",
    )
    conversation = service.conversations.create_conversation(
        ConversationCreate(mode="work", project_id=project.project_id),
        f"budget-switch-{current_role.value}-conversation",
    )
    original_request = "先预处理，然后比较两组 ALFF。"
    intent = analysis_dependency_for_request(
        original_request,
        project.project_id,
        conversation.conversation_id,
        "budget-switch-existing-goal",
    )
    assert intent is not None
    goal = CompositeGoalIdentity(
        intent_task_id=str(intent["task_id"]),
        conversation_id=conversation.conversation_id,
        project_id=project.project_id,
        analysis_request_hash=content_hash(original_request),
        preprocessing_task_id=None,
    )
    service.automation.ensure_preprocessing_composite_goal_intent(goal)
    analysis_binding = ConversationWorkTaskView(
        agent_role=WorkAgentRole.ANALYSIS,
        work_task_id="selected-analysis-task",
        analysis_intent_task_id=goal.intent_task_id,
    )

    import neuroagent.application.service_mixins.conversations as conversation_module

    actual_work_task = conversation_module.conversation_work_task

    def work_task_for_selected_goal(view, role):
        if view.conversation_id == conversation.conversation_id and role is WorkAgentRole.ANALYSIS:
            return analysis_binding
        return actual_work_task(view, role)

    monkeypatch.setattr(conversation_module, "conversation_work_task", work_task_for_selected_goal)
    if current_role is WorkAgentRole.ANALYSIS:
        service.conversations.select_work_agent(
            conversation.conversation_id,
            WorkAgentSelection(agent_role=current_role, expected_version=conversation.version),
            f"budget-switch-select-{current_role.value}",
        )

    class FixedRouter:
        model = "mock-router"

        async def choose(self, *, request_text, context, persistent_model_budget):
            assert persistent_model_budget is None
            assert request_text == "请准备一份新的预处理方案。"
            return WorkAgentRoutingDecision(
                status=WorkAgentRoutingStatus.ROUTED,
                role=routed_role,
                choice=(
                    WorkAgentRouteChoice.ANALYSIS
                    if routed_role is WorkAgentRole.ANALYSIS
                    else WorkAgentRouteChoice.PREPROCESSING
                ),
                confidence=0.99,
                model=self.model,
            )

    service.conversations.work_agent_router = FixedRouter()
    captured: dict[str, object] = {}

    async def respond(_self, _conversation, request, _packet, **kwargs):
        assert request.agent_role is routed_role
        captured["budget"] = kwargs["persistent_model_budget"]
        return WorkActionResult(
            "已按自动分派进入目标 Agent。", payload={"agent_role": routed_role.value}
        )

    monkeypatch.setattr(WorkDialogue, "respond", respond)
    result = await service.conversations.send_conversation_turn(
        conversation.conversation_id,
        ConversationTurnCreate(content="请准备一份新的预处理方案。"),
        f"budget-switch-turn-{current_role.value}",
    )

    if routed_role is WorkAgentRole.PREPROCESSING:
        assert captured["budget"] is None
    else:
        assert isinstance(captured["budget"], conversation_module._CompositeGoalModelBudgetSession)
        assert captured["budget"]._intent_task_id == goal.intent_task_id
    assert result.assistant_message.payload["agent_role"] == routed_role.value


@pytest.mark.asyncio
async def test_direct_action_role_comes_from_action_and_server_binding_not_agent_role(
    service, monkeypatch
):
    conversation = service.conversations.create_conversation(
        ConversationCreate(mode="work"), "direct-action-role"
    )
    calls: list[object] = []

    def execute(request, **kwargs):
        calls.append(request.agent_role)
        return WorkActionResult("统计结果已查询。", payload={})

    monkeypatch.setattr(service.conversation_work, "execute", execute)
    turn = await service.conversations.send_conversation_turn(
        conversation.conversation_id,
        ConversationTurnCreate(
            content="读取已完成的统计结果",
            action="get_statistical_results",
            agent_role=WorkAgentRole.PREPROCESSING,
        ),
        "direct-action-role-turn",
    )
    assert calls == [WorkAgentRole.ANALYSIS]
    assert turn.assistant_message.payload["agent_role"] == WorkAgentRole.ANALYSIS.value
