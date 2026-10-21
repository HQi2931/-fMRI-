"""Intent routing, continuation and ambiguity across the two Work roles."""

from __future__ import annotations

import pytest

from neuroagent.application.automation.analysis_outbox import AnalysisProposalOutbox
from neuroagent.application.contracts.conversations import (
    ConversationCreate,
    ConversationTurnCreate,
    ConversationWorkTaskView,
)
from neuroagent.application.hashing import content_hash
from neuroagent.application.work_agents import (
    analysis_dependency_for_request,
    clear_analysis_intent_envelope,
    starts_new_preprocessing_task,
    work_agent_role_for_turn,
    work_agent_routing_question,
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
    text = "先预处理这批静息态 fMRI 数据，然后对 ALFF 做两组统计比较。"  # noqa: RUF001
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


def test_explicit_role_selection_is_respected_and_no_clarification_is_needed(service):
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
