"""Restore exact analysis review and frozen execution context from authority records."""

from __future__ import annotations

from typing import NoReturn

from pydantic import ValidationError

from neuroagent.application.automation.analysis_outbox import AnalysisProposalOutbox
from neuroagent.application.automation.composite_goal import CompositeGoalIdentity
from neuroagent.application.automation.models import TaskStatus, TaskStepStatus, WorkTask
from neuroagent.application.contracts.conversations import (
    ConversationRole,
    ConversationView,
    ConversationWorkTaskView,
)
from neuroagent.application.contracts.plans import PlanRevisionView
from neuroagent.application.contracts.runs import RunView
from neuroagent.application.errors import ConflictError
from neuroagent.application.hashing import content_hash
from neuroagent.application.statistics_draft import (
    StatisticalDesignProposal,
    StatisticsDraft,
    statistics_draft_hash,
)
from neuroagent.application.statistics_draft import (
    proposal_hash as calculate_proposal_hash,
)
from neuroagent.domain.fmri.statistics import design_matrix
from neuroagent.domain.lifecycle import PlanState, WorkflowState
from neuroagent.multi_agent.registry import WorkAgentRole


def analysis_selection(
    conversation: ConversationView,
    goal: CompositeGoalIdentity,
    outbox: AnalysisProposalOutbox,
    task: WorkTask | None,
    *,
    plan: PlanRevisionView | None = None,
    run: RunView | None = None,
) -> ConversationWorkTaskView:
    """Validate the newest goal-owned snapshot, including incomplete revisions."""

    def reject() -> NoReturn:
        raise ConflictError(
            "analysis_goal_selection_conflict",
            "当前分析目标尚不可恢复, 或来源、草稿及任务记录不一致。请刷新进度后核对。",
        )

    if (
        outbox.intent_task_id != goal.intent_task_id
        or outbox.conversation_id != conversation.conversation_id
        or outbox.project_id != conversation.project_id
        or outbox.analysis_request_hash != goal.analysis_request_hash
        or outbox.status.value not in {"waiting_user", "proposal_ready"}
    ):
        reject()
    if task is not None and (
        task.task_id != goal.analysis_work_task_id
        or task.status in {TaskStatus.BLOCKED, TaskStatus.CANCELLED}
        or task.authority_refs.conversation_id != goal.conversation_id
        or task.authority_refs.analysis_intent_task_id != goal.intent_task_id
        or task.authority_refs.analysis_request_hash != goal.analysis_request_hash
        or task.authority_refs.plan_revision_id != (plan.plan_revision_id if plan else None)
        or task.authority_refs.workflow_run_id != (run.run_id if run else None)
    ):
        reject()
    if plan is not None:
        if task is None:
            reject()
        return _frozen_selection(conversation, goal, outbox, task, plan, run)
    if run is not None or (task is not None and task.status is not TaskStatus.WAITING_USER):
        reject()
    continuation_message = next(
        (
            message
            for message in conversation.messages
            if message.message_id == outbox.continuation_message_id
            and message.role is ConversationRole.ASSISTANT
        ),
        None,
    )
    continuation = (
        continuation_message.payload.get("analysis_outbox_continuation")
        if continuation_message is not None
        else None
    )
    if not isinstance(continuation, dict) or any(
        continuation.get(field) != expected
        for field, expected in (
            ("intent_task_id", goal.intent_task_id),
            ("analysis_request_hash", goal.analysis_request_hash),
            ("run_id", outbox.run_id),
            ("plan_revision_id", outbox.plan_revision_id),
            ("manifest_hash", outbox.manifest_hash),
        )
    ):
        reject()
    for message in reversed(conversation.messages):
        saved = message.payload.get("analysis_draft")
        if (
            message.role is not ConversationRole.ASSISTANT
            or message.payload.get("agent_role") != WorkAgentRole.ANALYSIS.value
            or not isinstance(saved, dict)
            or saved.get("analysis_intent_task_id") != goal.intent_task_id
            or message.payload.get("analysis_draft_unchanged") is True
        ):
            continue
        # Never fall back to an older valid proposal after a newer invalid draft.
        try:
            draft = StatisticsDraft.model_validate(saved.get("draft"))
        except ValidationError:
            reject()
        if (
            saved.get("analysis_request_hash") != goal.analysis_request_hash
            or saved.get("hash") != statistics_draft_hash(draft)
            or draft.source_run_id not in {None, outbox.run_id}
            or draft.external_import_id is not None
            or saved.get("source_run_id") not in {None, outbox.run_id}
        ):
            reject()
        selected = ConversationWorkTaskView(
            agent_role=WorkAgentRole.ANALYSIS,
            analysis_intent_task_id=goal.intent_task_id,
            draft_hash=statistics_draft_hash(draft),
        )
        raw_proposal = message.payload.get("statistical_design_proposal")
        if raw_proposal is None:
            return selected
        try:
            proposal = StatisticalDesignProposal.model_validate(raw_proposal)
        except ValidationError:
            reject()
        if (
            task is None
            or proposal.project_id != goal.project_id
            or proposal.source_run_id != outbox.run_id
            or proposal.input_manifest_hash != outbox.manifest_hash
            or len(task.steps) != 1
            or task.steps[0].kind != "statistical_run"
            or task.steps[0].input_version != proposal.proposal_hash
            or task.authority_refs.input_revision != selected.draft_hash
            or not _proposal_hash_matches(proposal)
        ):
            reject()
        return selected.model_copy(
            update={
                "proposal_hash": proposal.proposal_hash,
                "proposal_message_id": message.message_id,
                "work_task_id": task.task_id if task is not None else None,
            }
        )
    reject()


def _selection_conflict() -> NoReturn:
    raise ConflictError(
        "analysis_goal_selection_conflict",
        "分析目标的冻结方案、提案消息或运行身份无法核验, 请刷新后核对。",
    )


def _proposal_hash_matches(proposal: StatisticalDesignProposal) -> bool:
    return proposal.proposal_hash == calculate_proposal_hash(
        proposal.design,
        proposal.correction,
        demographics_revision_id=proposal.demographics_revision_id,
        demographics_hash=proposal.demographics_hash,
    )


def _frozen_selection(
    conversation: ConversationView,
    goal: CompositeGoalIdentity,
    outbox: AnalysisProposalOutbox,
    task: WorkTask,
    plan: PlanRevisionView,
    run: RunView | None,
) -> ConversationWorkTaskView:
    """Validate a frozen Plan's original message, without probing today's environment."""
    context = plan.plan.get("work_context")
    if (
        plan.project_id != goal.project_id
        or plan.plan.get("kind") != "statistical_design"
        or plan.state not in {PlanState.DRAFT, PlanState.AWAITING_APPROVAL, PlanState.APPROVED}
        or plan.manifest_hash != outbox.manifest_hash
        or plan.plan_hash
        != content_hash(
            {
                "plan": plan.plan,
                "manifest_hash": plan.manifest_hash,
                "environment_hash": plan.environment_hash,
            }
        )
        or not isinstance(context, dict)
        or context.get("conversation_id") != conversation.conversation_id
    ):
        _selection_conflict()
    message = next(
        (
            item
            for item in conversation.messages
            if item.message_id == context.get("proposal_message_id")
        ),
        None,
    )
    if (
        message is None
        or message.role is not ConversationRole.ASSISTANT
        or message.payload.get("agent_role") != WorkAgentRole.ANALYSIS.value
    ):
        _selection_conflict()
    saved = message.payload.get("analysis_draft")
    if not isinstance(saved, dict):
        _selection_conflict()
    try:
        draft = StatisticsDraft.model_validate(saved.get("draft"))
        proposal = StatisticalDesignProposal.model_validate(
            message.payload.get("statistical_design_proposal")
        )
    except ValidationError:
        _selection_conflict()
    draft_hash = statistics_draft_hash(draft)
    if (
        saved.get("analysis_intent_task_id") != goal.intent_task_id
        or saved.get("analysis_request_hash") != goal.analysis_request_hash
        or saved.get("hash") != draft_hash
        or saved.get("source_run_id") not in {None, outbox.run_id}
        or draft.source_run_id != outbox.run_id
        or draft.external_import_id is not None
        or context.get("draft_hash") != draft_hash
        or context.get("proposal_hash") != proposal.proposal_hash
        or not _proposal_hash_matches(proposal)
        or proposal.project_id != goal.project_id
        or proposal.source_run_id != outbox.run_id
        or proposal.input_manifest_hash != outbox.manifest_hash
        or proposal.design.model_dump(mode="json") != plan.plan.get("design")
        or (proposal.correction.model_dump(mode="json") if proposal.correction else None)
        != plan.plan.get("correction")
        or [list(row) for row in design_matrix(proposal.design)] != plan.plan.get("design_matrix")
        or len(task.steps) != 1
    ):
        _selection_conflict()
    step = task.steps[0]
    if (
        step.kind != "statistical_run"
        or step.sequence != 0
        or step.input_version != proposal.proposal_hash
        or any(
            getattr(refs, field) != expected
            for refs in (task.authority_refs, step.authority_refs)
            for field, expected in (
                ("conversation_id", goal.conversation_id),
                ("analysis_intent_task_id", goal.intent_task_id),
                ("analysis_request_hash", goal.analysis_request_hash),
                ("input_revision", draft_hash),
                ("plan_revision_id", plan.plan_revision_id),
                ("workflow_run_id", run.run_id if run else None),
            )
        )
    ):
        _selection_conflict()
    if run is None:
        if task.status is not TaskStatus.WAITING_USER or step.status is not TaskStepStatus.READY:
            _selection_conflict()
    elif (
        run.project_id != goal.project_id
        or run.plan_revision_id != plan.plan_revision_id
        or plan.state is not PlanState.APPROVED
        or task.status
        not in {
            TaskStatus.WAITING_USER,
            TaskStatus.WAITING_RUN,
            TaskStatus.WAITING_DEPENDENCY,
            TaskStatus.COMPLETED,
        }
        or step.status
        is not (
            TaskStepStatus.SUCCEEDED
            if task.status is TaskStatus.COMPLETED
            else TaskStepStatus.WAITING
        )
        or (task.status is TaskStatus.COMPLETED and run.state is not WorkflowState.SUCCEEDED)
    ):
        _selection_conflict()
    # Read-only snapshots do not constitute a new draft. Every real newer
    # revision must retain the same complete proposal and frozen draft identity.
    original_index = conversation.messages.index(message)
    for newer in conversation.messages[original_index + 1 :]:
        candidate = newer.payload.get("analysis_draft")
        if (
            newer.role is not ConversationRole.ASSISTANT
            or newer.payload.get("agent_role") != WorkAgentRole.ANALYSIS.value
            or not isinstance(candidate, dict)
            or candidate.get("analysis_intent_task_id") != goal.intent_task_id
            or newer.payload.get("analysis_draft_unchanged") is True
        ):
            continue
        try:
            newer_draft = StatisticsDraft.model_validate(candidate.get("draft"))
            newer_proposal = StatisticalDesignProposal.model_validate(
                newer.payload.get("statistical_design_proposal")
            )
        except ValidationError:
            _selection_conflict()
        if (
            candidate.get("analysis_request_hash") != goal.analysis_request_hash
            or candidate.get("hash") != draft_hash
            or statistics_draft_hash(newer_draft) != draft_hash
            or candidate.get("source_run_id") not in {None, outbox.run_id}
            or newer_proposal != proposal
        ):
            _selection_conflict()
    return ConversationWorkTaskView(
        agent_role=WorkAgentRole.ANALYSIS,
        analysis_intent_task_id=goal.intent_task_id,
        draft_hash=draft_hash,
        proposal_hash=proposal.proposal_hash,
        proposal_message_id=message.message_id,
        work_task_id=task.task_id,
        plan_revision_id=plan.plan_revision_id,
        run_id=run.run_id if run else None,
    )
