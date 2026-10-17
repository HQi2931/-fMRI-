"""Frozen analysis turns can explain a selected goal without changing it."""

# ruff: noqa: RUF001

from __future__ import annotations

import asyncio
import json

import pytest

from neuroagent.application.contracts.conversations import (
    ConversationAction,
    ConversationTurnCreate,
)
from neuroagent.application.errors import ConflictError
from neuroagent.application.work_tasks import work_task
from neuroagent.multi_agent.registry import WorkAgentRole
from tests.backend.test_composite_goal_selection import _select
from tests.backend.test_frozen_analysis_goal_selection import _frozen
from tests.backend.test_work_analysis_handoffs import _database_table_counts


def _reply(
    answer: str, *, action: str = "conversation", **fields: object
) -> tuple[str, dict, None]:
    return (
        json.dumps({"answer": answer, "action": action, **fields}),
        {"finish_reason": "stop"},
        None,
    )


def _latest_analysis_message(conversation):
    return next(
        message
        for message in reversed(conversation.messages)
        if message.payload.get("agent_role") == "analysis"
    )


def test_frozen_plan_without_run_returns_readonly_status(
    service, source_root, work_root, monkeypatch
):
    conversation_id, source, plan, run, interpreter = _frozen(
        service, source_root, work_root, monkeypatch, "draft"
    )
    assert run is None
    before = service.conversations.get_conversation(conversation_id)
    before_task = work_task(before, WorkAgentRole.ANALYSIS)
    task = service.automation.get_task(before_task.work_task_id)
    counts = _database_table_counts(service)
    monkeypatch.setattr(
        service.work_dialogue,
        "get_result_facts",
        lambda *_: pytest.fail("there is no Run from which to read facts"),
    )
    interpreter.return_value = _reply(
        "Unsupported proposal.",
        action="prepare_statistical_analysis",
        analysis_patch={"correction_parameters": {"q_threshold": 0.01}},
    )

    response = asyncio.run(
        service.conversations.send_conversation_turn(
            conversation_id,
            ConversationTurnCreate(
                content="这个方案目前有什么结果？",
                agent_role="analysis",
                action=ConversationAction.GET_STATISTICAL_RESULTS,
            ),
            "frozen-readout-no-run",
        )
    )

    assert "尚未启动统计运行" in response.assistant_message.content
    assert "one_sample_t" in response.assistant_message.content
    assert response.assistant_message.payload["statistics_run_not_started"] is True
    assert response.assistant_message.payload["analysis_draft_unchanged"] is True
    assert (
        response.assistant_message.payload["analysis_draft"]
        == _latest_analysis_message(before).payload["analysis_draft"]
    )
    assert response.assistant_message.payload["frozen_analysis_readout"] == {
        "intent_task_id": source.intent_task_id,
        "plan_revision_id": plan.plan_revision_id,
        "run_id": None,
        "read_only": True,
    }
    assert interpreter.call_args.kwargs["allow_tools"] is False
    assert interpreter.call_args.kwargs["execution_context"].allowed_run_ids == frozenset()
    model_input = json.dumps(interpreter.call_args.args[0], ensure_ascii=False)
    assert source_root.as_posix() not in model_input
    assert "design_matrix" not in model_input
    after = service.conversations.get_conversation(conversation_id)
    assert work_task(after, WorkAgentRole.ANALYSIS) == before_task
    assert service.automation.get_task(task.task_id) == task
    assert service.plans.get_plan(plan.plan_revision_id) == plan
    assert _database_table_counts(service)["workflow_runs"] == counts["workflow_runs"]
    assert len(service.runs.list_runs(project_id=source.project_id)) == 1


def test_frozen_run_readout_is_scoped_and_rejects_another_run(
    service, source_root, work_root, monkeypatch
):
    conversation_id, source, plan, run, interpreter = _frozen(
        service, source_root, work_root, monkeypatch, "queued"
    )
    assert run is not None
    before = service.conversations.get_conversation(conversation_id)
    run_count = len(service.runs.list_runs(project_id=source.project_id))
    task = service.automation.get_task(work_task(before, WorkAgentRole.ANALYSIS).work_task_id)
    list_calls: list[tuple[str | None, str | None]] = []
    fact_calls: list[tuple[str, bool]] = []
    monkeypatch.setattr(
        service.conversation_work,
        "_list_statistical_results",
        lambda project_id, run_id: list_calls.append((project_id, run_id)) or [],
    )
    monkeypatch.setattr(
        service.work_dialogue,
        "get_result_facts",
        lambda run_id, spatial: (
            fact_calls.append((run_id, spatial)) or [{"run_id": run_id, "untrusted_content": True}]
        ),
    )
    interpreter.return_value = _reply("只能说明已核验的结果。")

    response = asyncio.run(
        service.conversations.send_conversation_turn(
            conversation_id,
            ConversationTurnCreate(
                content="请解释已完成的结果。",
                agent_role="analysis",
                action=ConversationAction.GET_STATISTICAL_RESULTS,
            ),
            "frozen-readout-exact-run",
        )
    )

    assert list_calls == [(source.project_id, run.run_id)]
    assert fact_calls == [(run.run_id, True)]
    assert interpreter.call_args.kwargs["allow_tools"] is False
    assert interpreter.call_args.kwargs["execution_context"].allowed_run_ids == frozenset(
        {run.run_id}
    )
    model_input = json.dumps(interpreter.call_args.args[0], ensure_ascii=False)
    assert '"run_id": "' + run.run_id + '"' in model_input
    assert response.assistant_message.payload["frozen_analysis_readout"]["run_id"] == run.run_id
    assert response.assistant_message.payload["analysis_draft_unchanged"] is True
    assert service.automation.get_task(task.task_id) == task
    assert service.plans.get_plan(plan.plan_revision_id) == plan
    assert len(service.runs.list_runs(project_id=source.project_id)) == run_count

    before_conflict = service.conversations.get_conversation(conversation_id)
    interpreter.reset_mock()
    with pytest.raises(ConflictError) as failure:
        asyncio.run(
            service.conversations.send_conversation_turn(
                conversation_id,
                ConversationTurnCreate(
                    content="读取另一运行",
                    agent_role="analysis",
                    action=ConversationAction.GET_STATISTICAL_RESULTS,
                    target_run_id="another-run",
                ),
                "frozen-readout-other-run",
            )
        )
    assert failure.value.code == "analysis_goal_selection_conflict"
    interpreter.assert_not_called()
    assert service.conversations.get_conversation(conversation_id) == before_conflict
    assert list_calls == [(source.project_id, run.run_id)]


def test_explicit_progress_preserves_selected_analysis_and_qc_cannot_cross_run(
    service, source_root, work_root, monkeypatch
):
    conversation_id, source, _, _, interpreter = _frozen(
        service, source_root, work_root, monkeypatch, "draft"
    )
    before = service.conversations.get_conversation(conversation_id)
    selected_analysis = work_task(before, WorkAgentRole.ANALYSIS)
    preprocessing = work_task(before, WorkAgentRole.PREPROCESSING)
    interpreter.return_value = _reply("尚未启动统计运行。")

    progress = asyncio.run(
        service.conversations.send_conversation_turn(
            conversation_id,
            ConversationTurnCreate(
                content="当前进展如何？", action=ConversationAction.GET_PROGRESS
            ),
            "frozen-explicit-progress",
        )
    )

    assert progress.assistant_message.payload["agent_role"] == "analysis"
    assert work_task(progress.conversation, WorkAgentRole.ANALYSIS) == selected_analysis
    assert work_task(progress.conversation, WorkAgentRole.PREPROCESSING) == preprocessing
    assert progress.conversation.foreground_agent_role is WorkAgentRole.ANALYSIS
    assert progress.conversation.active_run_id is None

    before_qc = service.conversations.get_conversation(conversation_id)
    interpreter.reset_mock()
    with pytest.raises(ConflictError) as failure:
        asyncio.run(
            service.conversations.send_conversation_turn(
                conversation_id,
                ConversationTurnCreate(
                    content="读取 QC",
                    agent_role="analysis",
                    action=ConversationAction.GET_QC_STATUS,
                    qc_review_id="review-from-another-run",
                ),
                "frozen-no-run-other-qc",
            )
        )
    assert failure.value.code == "analysis_goal_selection_conflict"
    interpreter.assert_not_called()
    assert service.conversations.get_conversation(conversation_id) == before_qc
    assert source.run_id == preprocessing.run_id


def test_completed_goal_can_be_reselected_and_read_without_claiming_fixture_results(
    service, source_root, work_root, monkeypatch
):
    conversation_id, source, plan, run, interpreter = _frozen(
        service, source_root, work_root, monkeypatch, "completed"
    )
    assert run is not None
    selected = _select(service, conversation_id, source.intent_task_id, "reselect-completed")
    bound = work_task(selected, WorkAgentRole.ANALYSIS)
    assert bound is not None and bound.run_id == run.run_id
    goal = service.automation.get_composite_goal(conversation_id, source.intent_task_id)
    task = service.automation.get_task(goal.analysis_work_task_id)
    fact_calls: list[tuple[str, bool]] = []
    monkeypatch.setattr(
        service.work_dialogue,
        "get_result_facts",
        lambda run_id, spatial: fact_calls.append((run_id, spatial)) or [],
    )
    interpreter.return_value = _reply("运行状态为成功。")

    response = asyncio.run(
        service.conversations.send_conversation_turn(
            conversation_id,
            ConversationTurnCreate(
                content="统计运行状态如何？",
                agent_role="analysis",
                action=ConversationAction.GET_PROGRESS,
            ),
            "completed-goal-read-progress",
        )
    )

    assert "已完成" in response.assistant_message.content
    assert response.assistant_message.payload["frozen_analysis_readout"]["run_id"] == run.run_id
    assert response.assistant_message.payload["analysis_draft_unchanged"] is True
    assert fact_calls == [(run.run_id, False)]
    assert interpreter.call_args.kwargs["execution_context"].allowed_run_ids == frozenset(
        {run.run_id}
    )
    after = service.conversations.get_conversation(conversation_id)
    assert work_task(after, WorkAgentRole.ANALYSIS) == bound
    assert service.automation.get_task(task.task_id) == task
    assert service.plans.get_plan(plan.plan_revision_id) == plan
