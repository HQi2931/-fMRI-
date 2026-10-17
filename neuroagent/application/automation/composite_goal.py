"""Exact identity links and read-only progress for one composite Work goal."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from neuroagent.application.automation.analysis_outbox import AnalysisProposalOutbox
from neuroagent.application.automation.models import TaskStatus, WorkTask
from neuroagent.application.automation.result_interpretation import (
    StatisticalResultInterpretationOutbox,
)
from neuroagent.application.hashing import content_hash


class CompositeGoalStatus(StrEnum):
    WAITING_PREPROCESSING = "waiting_preprocessing"
    WAITING_ANALYSIS = "waiting_analysis"
    WAITING_RESULT_INTERPRETATION = "waiting_result_interpretation"
    WAITING_USER = "waiting_user"
    BLOCKED = "blocked"
    COMPLETED = "completed"
    INDETERMINATE = "indeterminate"


class CompositeGoalIdentity(BaseModel):
    """Stable root identity and exact child references; contains no copied state."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    intent_task_id: str = Field(min_length=1, max_length=100)
    conversation_id: str = Field(min_length=1, max_length=100)
    project_id: str = Field(min_length=1, max_length=100)
    analysis_request_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    goal_kind: str = Field(
        default="preprocess_then_analyze",
        pattern=r"^(preprocess_then_analyze|analysis_only_external_import)$",
    )
    preprocessing_task_id: str | None = Field(default=None, max_length=100)
    source_external_import_id: str | None = Field(default=None, max_length=100)
    source_import_version: int | None = Field(default=None, ge=1)
    source_tree_hash: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    source_inclusion_hash: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    source_manifest_hash: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    source_revision_hash: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    # The root exists as soon as the preprocessing Plan is committed. Its
    # proposal outbox is created later, together with the authorized Run.
    analysis_proposal_outbox_intent_task_id: str | None = Field(default=None, max_length=100)
    analysis_work_task_id: str | None = Field(default=None, max_length=100)
    result_interpretation_outbox_task_id: str | None = Field(default=None, max_length=100)

    @model_validator(mode="after")
    def references_match_stable_ids(self) -> CompositeGoalIdentity:
        source_fields = (
            self.source_external_import_id,
            self.source_import_version,
            self.source_tree_hash,
            self.source_inclusion_hash,
            self.source_manifest_hash,
            self.source_revision_hash,
        )
        if self.goal_kind == "preprocess_then_analyze":
            if self.preprocessing_task_id is None or any(
                item is not None for item in source_fields
            ):
                raise ValueError("preprocessing goals require only a preprocessing task reference")
        elif (
            self.preprocessing_task_id is not None
            or any(item is None for item in source_fields)
            or self.analysis_proposal_outbox_intent_task_id is not None
            or self.result_interpretation_outbox_task_id is not None
        ):
            raise ValueError("analysis-only goals require an exact external import source")
        if (
            self.analysis_proposal_outbox_intent_task_id is not None
            and self.analysis_proposal_outbox_intent_task_id != self.intent_task_id
        ):
            raise ValueError("proposal outbox reference must equal the stable intent ID")
        if (
            self.result_interpretation_outbox_task_id is not None
            and self.result_interpretation_outbox_task_id != self.analysis_work_task_id
        ):
            raise ValueError("result interpretation must reference the current analysis task")
        return self


class CompositeGoalAnalysisAttempt(BaseModel):
    """Identity-only association; WorkTask remains the attempt state authority."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    attempt_number: int = Field(ge=1)
    work_task_id: str = Field(min_length=1, max_length=100)
    proposal_hash: str = Field(pattern=r"^[a-f0-9]{64}$")


class CompositeGoalContinuationMessage(BaseModel):
    """Minimal persisted assistant evidence used to verify delivered completion."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    message_id: str = Field(min_length=1, max_length=100)
    conversation_id: str = Field(min_length=1, max_length=100)
    role: str = Field(min_length=1, max_length=20)
    payload: dict[str, object]


class CompositeGoalIntentMessage(BaseModel):
    """Persisted assistant evidence that may name this goal's original request."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    conversation_id: str = Field(min_length=1, max_length=100)
    role: str = Field(min_length=1, max_length=20)
    payload: dict[str, object]


class CompositeGoalSnapshot(BaseModel):
    """One consistent database read of the root and its authoritative children."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    goal: CompositeGoalIdentity
    analysis_intent_messages: tuple[CompositeGoalIntentMessage, ...] = Field(
        default=(), max_length=2
    )
    conversation_mode: str | None = None
    conversation_project_id: str | None = None
    preprocessing_plan_revision_id: str | None = None
    preprocessing_plan_project_id: str | None = None
    preprocessing_plan_manifest_hash: str | None = None
    preprocessing_task: WorkTask | None = None
    analysis_proposal_outbox: AnalysisProposalOutbox | None = None
    analysis_attempts: tuple[CompositeGoalAnalysisAttempt, ...] = Field(default=(), max_length=1000)
    proposal_analysis_task: WorkTask | None = None
    analysis_task: WorkTask | None = None
    result_interpretation_outbox: StatisticalResultInterpretationOutbox | None = None
    result_continuation_message: CompositeGoalContinuationMessage | None = None


class CompositeGoalProgress(BaseModel):
    """A freshly calculated view; child lifecycle state remains authoritative."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    goal: CompositeGoalIdentity
    analysis_summary: str | None = Field(default=None, max_length=160)
    status: CompositeGoalStatus
    preprocessing_status: str | None = None
    analysis_proposal_status: str | None = None
    analysis_status: str | None = None
    result_interpretation_status: str | None = None
    continuation_message_id: str | None = None
    reason_code: str | None = None


def project_composite_goal(snapshot: CompositeGoalSnapshot) -> CompositeGoalProgress:
    """Project progress from exact child records without copying their state."""

    goal = snapshot.goal
    prep = snapshot.preprocessing_task
    analysis_summary = _verified_analysis_summary(goal, snapshot.analysis_intent_messages)

    def result(
        status: CompositeGoalStatus,
        reason: str,
        *,
        analysis_status: str | None = None,
        proposal_status: str | None = None,
        result_status: str | None = None,
        continuation_message_id: str | None = None,
    ) -> CompositeGoalProgress:
        return CompositeGoalProgress(
            goal=goal,
            analysis_summary=analysis_summary,
            status=status,
            preprocessing_status=prep.status.value if prep is not None else None,
            analysis_proposal_status=proposal_status,
            analysis_status=analysis_status,
            result_interpretation_status=result_status,
            continuation_message_id=continuation_message_id,
            reason_code=reason,
        )

    if prep is None or prep.task_id != goal.preprocessing_task_id:
        return result(CompositeGoalStatus.INDETERMINATE, "preprocessing_task_missing")
    prep_refs = prep.authority_refs
    if (
        prep_refs.conversation_id != goal.conversation_id
        or (prep_refs.analysis_intent_task_id is None) != (prep_refs.analysis_request_hash is None)
        or prep_refs.plan_revision_id != snapshot.preprocessing_plan_revision_id
        or prep_refs.input_revision != snapshot.preprocessing_plan_manifest_hash
        or snapshot.preprocessing_plan_project_id != goal.project_id
    ):
        return result(CompositeGoalStatus.INDETERMINATE, "preprocessing_binding_mismatch")
    preprocessing_root_matches = (
        prep_refs.analysis_intent_task_id == goal.intent_task_id
        and prep_refs.analysis_request_hash == goal.analysis_request_hash
    )

    outbox = snapshot.analysis_proposal_outbox
    if goal.analysis_proposal_outbox_intent_task_id is None:
        if not preprocessing_root_matches:
            return result(CompositeGoalStatus.INDETERMINATE, "preprocessing_binding_mismatch")
        if outbox is not None or prep_refs.workflow_run_id is not None:
            return result(CompositeGoalStatus.INDETERMINATE, "proposal_outbox_binding_missing")
        if prep.status is TaskStatus.WAITING_USER:
            return result(CompositeGoalStatus.WAITING_USER, "preprocessing_user_decision")
        if prep.status in {TaskStatus.BLOCKED, TaskStatus.CANCELLED}:
            return result(CompositeGoalStatus.BLOCKED, "preprocessing_terminal_block")
        return result(CompositeGoalStatus.WAITING_PREPROCESSING, "preprocessing_not_started")

    if outbox is None:
        return result(CompositeGoalStatus.INDETERMINATE, "proposal_outbox_missing")
    if (
        outbox.intent_task_id != goal.intent_task_id
        or outbox.conversation_id != goal.conversation_id
        or outbox.project_id != goal.project_id
        or outbox.preprocessing_task_id != goal.preprocessing_task_id
        or outbox.analysis_request_hash != goal.analysis_request_hash
        or outbox.plan_revision_id != prep_refs.plan_revision_id
        or outbox.manifest_hash != prep_refs.input_revision
        or outbox.run_id != prep_refs.workflow_run_id
    ):
        return result(
            CompositeGoalStatus.INDETERMINATE,
            "proposal_outbox_authority_mismatch",
            proposal_status=outbox.status.value,
        )

    if prep.status in {TaskStatus.BLOCKED, TaskStatus.CANCELLED}:
        return result(
            CompositeGoalStatus.BLOCKED,
            "preprocessing_terminal_block",
            proposal_status=outbox.status.value,
        )
    if prep.status is not TaskStatus.COMPLETED:
        if prep.status is TaskStatus.WAITING_USER:
            return result(
                CompositeGoalStatus.WAITING_USER,
                "preprocessing_user_decision",
                proposal_status=outbox.status.value,
            )
        return result(
            CompositeGoalStatus.WAITING_PREPROCESSING,
            "preprocessing_in_progress",
            proposal_status=outbox.status.value,
        )

    if outbox.status.value == "blocked":
        return result(
            CompositeGoalStatus.BLOCKED,
            "analysis_proposal_blocked",
            proposal_status=outbox.status.value,
        )
    if outbox.status.value in {"pending", "ready"}:
        return result(
            CompositeGoalStatus.WAITING_ANALYSIS,
            "analysis_proposal_pending",
            proposal_status=outbox.status.value,
        )

    initial_task = snapshot.proposal_analysis_task
    if outbox.status.value == "proposal_ready":
        if (
            outbox.analysis_work_task_id is None
            or initial_task is None
            or initial_task.task_id != outbox.analysis_work_task_id
        ):
            return result(
                CompositeGoalStatus.INDETERMINATE,
                "proposal_analysis_task_missing",
                proposal_status=outbox.status.value,
            )
        if not _analysis_task_matches(initial_task, goal):
            return result(
                CompositeGoalStatus.INDETERMINATE,
                "proposal_analysis_task_mismatch",
                proposal_status=outbox.status.value,
            )
        initial_attempt = next(
            (
                item
                for item in snapshot.analysis_attempts
                if item.work_task_id == initial_task.task_id
            ),
            None,
        )
        if initial_attempt is None or initial_attempt.attempt_number != 1:
            return result(
                CompositeGoalStatus.INDETERMINATE,
                "proposal_analysis_attempt_missing",
                proposal_status=outbox.status.value,
            )
        if not _analysis_task_has_proposal(initial_task, initial_attempt.proposal_hash):
            return result(
                CompositeGoalStatus.INDETERMINATE,
                "proposal_analysis_attempt_mismatch",
                proposal_status=outbox.status.value,
            )
    elif outbox.status.value == "waiting_user" and outbox.analysis_work_task_id is not None:
        return result(
            CompositeGoalStatus.INDETERMINATE,
            "waiting_user_outbox_has_task",
            proposal_status=outbox.status.value,
        )

    analysis_task = snapshot.analysis_task
    if analysis_task is None:
        if outbox.status.value == "waiting_user":
            return result(
                CompositeGoalStatus.WAITING_USER,
                "analysis_decision_required",
                proposal_status=outbox.status.value,
            )
        return result(
            CompositeGoalStatus.INDETERMINATE,
            "analysis_task_link_missing",
            proposal_status=outbox.status.value,
        )
    if not _analysis_task_matches(analysis_task, goal):
        return result(
            CompositeGoalStatus.INDETERMINATE,
            "analysis_task_mismatch",
            proposal_status=outbox.status.value,
            analysis_status=analysis_task.status.value,
        )
    active_attempt = next(
        (item for item in snapshot.analysis_attempts if item.work_task_id == analysis_task.task_id),
        None,
    )
    if (
        analysis_task.task_id != goal.analysis_work_task_id
        or active_attempt is None
        or not _analysis_task_has_proposal(analysis_task, active_attempt.proposal_hash)
    ):
        return result(
            CompositeGoalStatus.INDETERMINATE,
            "analysis_task_reference_mismatch",
            proposal_status=outbox.status.value,
            analysis_status=analysis_task.status.value,
        )
    if analysis_task.status in {TaskStatus.BLOCKED, TaskStatus.CANCELLED}:
        return result(
            CompositeGoalStatus.BLOCKED,
            "analysis_terminal_block",
            proposal_status=outbox.status.value,
            analysis_status=analysis_task.status.value,
        )
    if analysis_task.status is TaskStatus.WAITING_USER:
        return result(
            CompositeGoalStatus.WAITING_USER,
            "analysis_user_decision",
            proposal_status=outbox.status.value,
            analysis_status=analysis_task.status.value,
        )
    if analysis_task.status is not TaskStatus.COMPLETED:
        return result(
            CompositeGoalStatus.WAITING_ANALYSIS,
            "analysis_in_progress",
            proposal_status=outbox.status.value,
            analysis_status=analysis_task.status.value,
        )

    interpretation = snapshot.result_interpretation_outbox
    if (
        goal.result_interpretation_outbox_task_id != analysis_task.task_id
        or interpretation is None
        or interpretation.task_id != analysis_task.task_id
        or interpretation.conversation_id != goal.conversation_id
        or interpretation.project_id != goal.project_id
        or interpretation.plan_revision_id != analysis_task.authority_refs.plan_revision_id
        or interpretation.run_id != analysis_task.authority_refs.workflow_run_id
    ):
        return result(
            CompositeGoalStatus.INDETERMINATE,
            "result_interpretation_binding_missing",
            proposal_status=outbox.status.value,
            analysis_status=analysis_task.status.value,
            result_status=interpretation.status.value if interpretation is not None else None,
        )
    result_status = interpretation.status.value
    if result_status == "blocked":
        status, reason = CompositeGoalStatus.BLOCKED, "result_interpretation_blocked"
    elif result_status == "waiting_user":
        status, reason = CompositeGoalStatus.WAITING_USER, "result_interpretation_user_decision"
    elif result_status == "completed" and interpretation.continuation_message_id:
        message = snapshot.result_continuation_message
        message_payload = message.payload if message is not None else {}
        completion = message_payload.get("statistical_result_interpretation")
        if not isinstance(completion, dict) or (
            message is None
            or message.message_id != interpretation.continuation_message_id
            or message.conversation_id != goal.conversation_id
            or message.role != "assistant"
            or completion.get("task_id") != analysis_task.task_id
            or completion.get("status") != "completed"
            or completion.get("result_id") != interpretation.result_id
            or completion.get("result_version") != interpretation.result_version
        ):
            status, reason = CompositeGoalStatus.INDETERMINATE, "result_continuation_mismatch"
        else:
            status, reason = CompositeGoalStatus.COMPLETED, "result_interpretation_delivered"
    elif result_status == "ready":
        status, reason = (
            CompositeGoalStatus.WAITING_RESULT_INTERPRETATION,
            "result_interpretation_pending",
        )
    else:
        status, reason = CompositeGoalStatus.INDETERMINATE, "result_interpretation_incomplete"
    return result(
        status,
        reason,
        analysis_status=analysis_task.status.value,
        proposal_status=outbox.status.value,
        result_status=result_status,
        continuation_message_id=interpretation.continuation_message_id,
    )


def _analysis_task_matches(task: WorkTask, goal: CompositeGoalIdentity) -> bool:
    return (
        task.authority_refs.conversation_id == goal.conversation_id
        and task.authority_refs.analysis_intent_task_id == goal.intent_task_id
        and task.authority_refs.analysis_request_hash == goal.analysis_request_hash
    )


def _verified_analysis_summary(
    goal: CompositeGoalIdentity,
    messages: tuple[CompositeGoalIntentMessage, ...],
) -> str | None:
    """Return bounded human text only from the exact persisted intent message."""

    matches: set[str] = set()
    for message in messages:
        if message.conversation_id != goal.conversation_id or message.role != "assistant":
            continue
        dependency = message.payload.get("analysis_dependency")
        if not isinstance(dependency, dict):
            continue
        request = dependency.get("analysis_request")
        if (
            dependency.get("task_kind") != "preprocess_then_analyze"
            or dependency.get("task_id") != goal.intent_task_id
            or dependency.get("conversation_id") != goal.conversation_id
            or dependency.get("project_id") != goal.project_id
            or not isinstance(request, str)
            or content_hash(request) != goal.analysis_request_hash
        ):
            continue
        matches.add(request)
    if len(matches) != 1:
        return None
    summary = " ".join(next(iter(matches)).split())
    if not summary:
        return None
    if len(summary) > 160:
        summary = summary[:157].rstrip() + "…"
    return summary


def _analysis_task_has_proposal(task: WorkTask, proposal_hash: str) -> bool:
    statistical_steps = tuple(step for step in task.steps if step.kind == "statistical_run")
    return len(statistical_steps) == 1 and statistical_steps[0].input_version == proposal_hash
