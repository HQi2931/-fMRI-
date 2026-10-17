"""Restore an exact, unexecuted analysis draft without reviving an old proposal."""

from __future__ import annotations

from typing import NoReturn

from pydantic import ValidationError

from neuroagent.application.automation.analysis_outbox import AnalysisProposalOutbox
from neuroagent.application.automation.composite_goal import CompositeGoalIdentity
from neuroagent.application.automation.models import TaskStatus, WorkTask
from neuroagent.application.contracts.conversations import (
    ConversationRole,
    ConversationView,
    ConversationWorkTaskView,
)
from neuroagent.application.errors import ConflictError
from neuroagent.application.statistics_draft import (
    StatisticalDesignProposal,
    StatisticsDraft,
    statistics_draft_hash,
)
from neuroagent.multi_agent.registry import WorkAgentRole


def analysis_selection(
    conversation: ConversationView,
    goal: CompositeGoalIdentity,
    outbox: AnalysisProposalOutbox,
    task: WorkTask | None,
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
        or task.status is not TaskStatus.WAITING_USER
        or task.authority_refs.conversation_id != goal.conversation_id
        or task.authority_refs.analysis_intent_task_id != goal.intent_task_id
        or task.authority_refs.analysis_request_hash != goal.analysis_request_hash
        or task.authority_refs.plan_revision_id is not None
        or task.authority_refs.workflow_run_id is not None
    ):
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
