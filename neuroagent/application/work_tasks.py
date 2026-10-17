"""Server-owned Work task references and confirmation scope checks."""

# ruff: noqa: RUF001

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from neuroagent.application.contracts.conversations import (
    ConversationMode,
    ConversationView,
    ConversationWorkTaskView,
)
from neuroagent.application.contracts.plans import PlanRevisionView
from neuroagent.application.errors import ConflictError
from neuroagent.multi_agent.registry import WorkAgentRole

if TYPE_CHECKING:
    from neuroagent.application.automation.service import WorkTaskApplicationService


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


def ensure_statistical_work_task_for_plan(
    automation: WorkTaskApplicationService,
    get_conversation: Callable[[str], ConversationView],
    plan: PlanRevisionView,
) -> str | None:
    """Ensure a durable statistical task is bound to a committed Work Plan.

    Older conversations can have a valid proposal and frozen statistical Plan
    without the newer WorkTask reference. This repairs that association within
    the caller's existing Unit of Work and rejects conflicting server links.
    """

    role_task = require_statistical_work_plan(get_conversation, plan)
    if role_task is None:
        return None
    context = plan.plan["work_context"]
    conversation_id = str(context["conversation_id"])
    proposal_hash = str(context["proposal_hash"])
    draft_hash = role_task.draft_hash
    if draft_hash is None:
        raise ConflictError(
            "statistics_work_proposal_stale", "统计草稿缺少哈希，无法绑定持久分析任务。"
        )
    conversation = get_conversation(conversation_id)
    design = plan.plan.get("design")
    test_name = design.get("test") if isinstance(design, dict) else None
    goal_description = _analysis_goal_description(
        conversation,
        role_task,
        proposal_hash=proposal_hash,
        test_name=test_name if isinstance(test_name, str) else "statistical analysis",
    )
    durable_task = automation.ensure_analysis_proposal_task(
        conversation_id=conversation_id,
        project_id=plan.project_id,
        proposal_hash=proposal_hash,
        draft_hash=draft_hash,
        goal_description=goal_description,
    )
    if role_task.work_task_id not in {None, durable_task.task_id}:
        raise ConflictError(
            "work_task_binding_conflict", "统计会话引用与提案对应的持久任务不一致。"
        )
    automation.bind_statistical_plan(
        task_id=durable_task.task_id,
        conversation_id=conversation_id,
        project_id=plan.project_id,
        proposal_hash=proposal_hash,
        plan_revision_id=plan.plan_revision_id,
    )
    return durable_task.task_id


def _analysis_goal_description(
    conversation: ConversationView,
    role_task: Any,
    *,
    proposal_hash: str,
    test_name: str,
) -> str:
    for index, message in enumerate(conversation.messages):
        if message.message_id != role_task.proposal_message_id:
            continue
        proposal = message.payload.get("statistical_design_proposal")
        if (
            message.role.value != "assistant"
            or message.payload.get("agent_role") != WorkAgentRole.ANALYSIS.value
            or not isinstance(proposal, dict)
            or proposal.get("proposal_hash") != proposal_hash
        ):
            break
        for previous in reversed(conversation.messages[:index]):
            if previous.role.value == "user":
                content = previous.content.strip()
                if content:
                    return ("用户目标：\n- " + content)[:4000]
    return (
        "依据已审查的统计设计提案准备并核验静息态 fMRI 统计结果"
        f"（{test_name}） {proposal_hash[:12]}。"
    )


def preprocessing_source_run(
    conversation: ConversationView, explicit_run_id: str | None
) -> str | None:
    if explicit_run_id:
        return explicit_run_id
    analysis = work_task(conversation, WorkAgentRole.ANALYSIS)
    if analysis and analysis.analysis_intent_task_id:
        from neuroagent.application.work_agents import latest_agent_payload

        saved = latest_agent_payload(conversation, "analysis_draft", WorkAgentRole.ANALYSIS)
        draft = saved.get("draft") if saved else None
        source = draft.get("source_run_id") if isinstance(draft, dict) else None
        if isinstance(source, str):
            return source
        sources = {
            continuation["run_id"]
            for message in conversation.messages
            if message.role.value == "assistant"
            and isinstance(
                continuation := message.payload.get("analysis_outbox_continuation"), dict
            )
            and continuation.get("intent_task_id") == analysis.analysis_intent_task_id
            and isinstance(continuation.get("run_id"), str)
        }
        return next(iter(sources)) if len(sources) == 1 else None
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
