"""Server-owned Work task references and confirmation scope checks."""

# ruff: noqa: RUF001

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from neuroagent.application.contracts import (
    ConversationMode,
    ConversationView,
    ConversationWorkTaskView,
    PlanRevisionView,
)
from neuroagent.application.errors import ConflictError
from neuroagent.multi_agent.registry import WorkAgentRole


def work_task(
    conversation: ConversationView, role: WorkAgentRole
) -> ConversationWorkTaskView | None:
    return next((item for item in conversation.work_tasks if item.agent_role is role), None)


def require_analysis_task(
    conversation: ConversationView,
    *,
    project_id: str,
    proposal_hash: str,
    plan_revision_id: str | None = None,
) -> ConversationWorkTaskView:
    task = work_task(conversation, WorkAgentRole.ANALYSIS)
    if conversation.mode is not ConversationMode.WORK or conversation.project_id != project_id:
        raise ConflictError(
            "statistics_work_project_mismatch", "统计方案与当前 Work 会话项目不匹配。"
        )
    if conversation.foreground_agent_role is not WorkAgentRole.ANALYSIS:
        raise ConflictError(
            "work_agent_confirmation_mismatch", "当前负责 Agent 已切换，请先切回数据分析 Agent。"
        )
    if (
        task is None
        or task.proposal_hash != proposal_hash
        or not task.proposal_message_id
        or not task.draft_hash
    ):
        raise ConflictError(
            "statistics_work_proposal_stale", "统计草稿或数据来源已更新，请审查当前统计方案。"
        )
    if plan_revision_id is not None and task.plan_revision_id != plan_revision_id:
        raise ConflictError(
            "statistics_work_plan_stale", "当前正式统计方案已变化，请审查最新方案。"
        )
    return task


def require_statistical_work_plan(
    get_conversation: Callable[[str], ConversationView],
    plan: PlanRevisionView,
) -> ConversationWorkTaskView | None:
    if plan.plan.get("kind") != "statistical_design":
        return None
    context = plan.plan.get("work_context")
    if context is None:
        return None
    conversation = get_conversation(str(context["conversation_id"]))
    task = require_analysis_task(
        conversation,
        project_id=plan.project_id,
        proposal_hash=str(context["proposal_hash"]),
        plan_revision_id=plan.plan_revision_id,
    )
    if task.draft_hash != context.get("draft_hash") or task.proposal_message_id != context.get(
        "proposal_message_id"
    ):
        raise ConflictError(
            "statistics_work_proposal_stale", "统计草稿已变化，请审查当前统计方案。"
        )
    return task


def preprocessing_source_run(
    conversation: ConversationView, explicit_run_id: str | None
) -> str | None:
    if explicit_run_id:
        return explicit_run_id
    task = work_task(conversation, WorkAgentRole.PREPROCESSING)
    if task and task.run_id:
        return task.run_id
    for message in reversed(conversation.messages):
        snapshot: Any = message.payload.get("analysis_draft")
        if isinstance(snapshot, dict) and isinstance(snapshot.get("draft"), dict):
            value = snapshot["draft"].get("source_run_id")
            if isinstance(value, str):
                return value
    analysis = work_task(conversation, WorkAgentRole.ANALYSIS)
    return (
        None
        if analysis and analysis.run_id == conversation.active_run_id
        else conversation.active_run_id
    )


def run_for_agent_context(conversation: ConversationView, role: WorkAgentRole) -> str | None:
    task = work_task(conversation, role)
    if task and task.run_id:
        return task.run_id
    if role is WorkAgentRole.ANALYSIS:
        return preprocessing_source_run(conversation, None)
    analysis = work_task(conversation, WorkAgentRole.ANALYSIS)
    return (
        None
        if analysis and analysis.run_id == conversation.active_run_id
        else conversation.active_run_id
    )
