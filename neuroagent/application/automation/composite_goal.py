"""Exact identity links and read-only progress for one composite Work goal."""

from __future__ import annotations

from collections.abc import Callable
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from neuroagent.application.automation.analysis_outbox import (
    AnalysisOutboxStatus,
    AnalysisProposalOutbox,
)
from neuroagent.application.automation.models import TaskStatus, TaskStepStatus, WorkTask
from neuroagent.application.automation.result_interpretation import (
    ResultInterpretationStatus,
    StatisticalResultInterpretationOutbox,
)
from neuroagent.application.hashing import content_hash
from neuroagent.domain.lifecycle import PlanState, WorkflowState


class CompositeGoalStatus(StrEnum):
    WAITING_PREPROCESSING = "waiting_preprocessing"
    WAITING_REVIEW = "waiting_review"
    WAITING_RUN = "waiting_run"
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
            if any(item is not None for item in source_fields):
                raise ValueError("preprocessing goals cannot reference an external import source")
        elif (
            self.preprocessing_task_id is not None
            or any(item is None for item in source_fields)
            or self.analysis_proposal_outbox_intent_task_id is not None
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


class CompositeGoalPlanEvidence(BaseModel):
    """Minimal immutable Plan identity read alongside a CompositeGoal snapshot."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    plan_revision_id: str = Field(min_length=1, max_length=100)
    project_id: str = Field(min_length=1, max_length=100)
    state: str = Field(min_length=1, max_length=32)
    manifest_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    plan_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    input_revision: str | None = Field(default=None, min_length=1, max_length=200)
    source_external_import_id: str | None = Field(default=None, max_length=100)
    source_external_import_hash: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")


class CompositeGoalRunEvidence(BaseModel):
    """Minimal authoritative Workflow/QC identity needed to label a waiting Run."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: str = Field(min_length=1, max_length=100)
    project_id: str = Field(min_length=1, max_length=100)
    plan_revision_id: str = Field(min_length=1, max_length=100)
    state: str = Field(min_length=1, max_length=32)
    version: int = Field(ge=1)
    qc_status: str | None = Field(default=None, max_length=32)
    latest_qc_review_state: str | None = Field(default=None, max_length=20)


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
    analysis_source_valid: bool = False
    analysis_result_binding_valid: bool = False
    conversation_mode: str | None = None
    conversation_project_id: str | None = None
    preprocessing_plan_revision_id: str | None = None
    preprocessing_plan_project_id: str | None = None
    preprocessing_plan_manifest_hash: str | None = None
    preprocessing_plan: CompositeGoalPlanEvidence | None = None
    preprocessing_run: CompositeGoalRunEvidence | None = None
    analysis_plan: CompositeGoalPlanEvidence | None = None
    analysis_run: CompositeGoalRunEvidence | None = None
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
    preprocessing_run_id: str | None = None
    analysis_run_id: str | None = None
    preprocessing_status: str | None = None
    analysis_proposal_status: str | None = None
    analysis_status: str | None = None
    result_interpretation_status: str | None = None
    continuation_message_id: str | None = None
    reason_code: str | None = None


class CompositeGoalHandoffAction(StrEnum):
    NONE = "none"
    PREPARE_ANALYSIS_PROPOSAL = "prepare_analysis_proposal"
    PREPARE_RESULT_INTERPRETATION = "prepare_result_interpretation"


class CompositeGoalHandoffDecision(BaseModel):
    """Bounded read-only phase decision; actions only resume existing outboxes."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    progress: CompositeGoalProgress
    phase: CompositeGoalStatus
    action: CompositeGoalHandoffAction = CompositeGoalHandoffAction.NONE
    target_id: str | None = Field(default=None, max_length=100)

    @model_validator(mode="after")
    def action_target_matches_phase(self) -> CompositeGoalHandoffDecision:
        if self.phase is not self.progress.status:
            raise ValueError("handoff phase must match the verified progress projection")
        if (self.action is CompositeGoalHandoffAction.NONE) != (self.target_id is None):
            raise ValueError("handoff target is required only for a supported continuation action")
        if (
            self.action is CompositeGoalHandoffAction.PREPARE_ANALYSIS_PROPOSAL
            and self.phase is not CompositeGoalStatus.WAITING_ANALYSIS
        ):
            raise ValueError("analysis proposal continuation requires the analysis phase")
        if (
            self.action is CompositeGoalHandoffAction.PREPARE_RESULT_INTERPRETATION
            and self.phase is not CompositeGoalStatus.WAITING_RESULT_INTERPRETATION
        ):
            raise ValueError("result interpretation continuation requires the result phase")
        return self


def resolve_composite_goal_handoff(snapshot: CompositeGoalSnapshot) -> CompositeGoalHandoffDecision:
    """Resolve the current bounded phase and any already-registered continuation."""

    progress = project_composite_goal(snapshot)
    action = CompositeGoalHandoffAction.NONE
    target_id: str | None = None
    proposal = snapshot.analysis_proposal_outbox
    if (
        snapshot.goal.goal_kind == "preprocess_then_analyze"
        and progress.status is CompositeGoalStatus.WAITING_ANALYSIS
        and proposal is not None
        and proposal.status is AnalysisOutboxStatus.READY
        and proposal.intent_task_id == snapshot.goal.analysis_proposal_outbox_intent_task_id
    ):
        action = CompositeGoalHandoffAction.PREPARE_ANALYSIS_PROPOSAL
        target_id = proposal.intent_task_id
    interpretation = snapshot.result_interpretation_outbox
    if (
        progress.status is CompositeGoalStatus.WAITING_RESULT_INTERPRETATION
        and interpretation is not None
        and interpretation.status is ResultInterpretationStatus.READY
        and interpretation.task_id == snapshot.goal.result_interpretation_outbox_task_id
    ):
        action = CompositeGoalHandoffAction.PREPARE_RESULT_INTERPRETATION
        target_id = interpretation.task_id
    return CompositeGoalHandoffDecision(
        progress=progress,
        phase=progress.status,
        action=action,
        target_id=target_id,
    )


def project_composite_goal(snapshot: CompositeGoalSnapshot) -> CompositeGoalProgress:
    """Project progress from exact child records without copying their state."""

    goal = snapshot.goal
    prep = snapshot.preprocessing_task
    analysis_summary = _verified_analysis_summary(goal, snapshot.analysis_intent_messages)
    verified_run_ids: dict[str, str | None] = {
        "preprocessing_run_id": None,
        "analysis_run_id": None,
    }

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
            **verified_run_ids,
            preprocessing_status=prep.status.value if prep is not None else None,
            analysis_proposal_status=proposal_status,
            analysis_status=analysis_status,
            result_interpretation_status=result_status,
            continuation_message_id=continuation_message_id,
            reason_code=reason,
        )

    if goal.goal_kind == "analysis_only_external_import":
        return _project_analysis_only_external_goal(snapshot, result, verified_run_ids)

    if goal.preprocessing_task_id is None:
        if (
            prep is not None
            or goal.analysis_proposal_outbox_intent_task_id is not None
            or goal.analysis_work_task_id is not None
            or goal.result_interpretation_outbox_task_id is not None
            or snapshot.analysis_proposal_outbox is not None
            or snapshot.result_interpretation_outbox is not None
        ):
            return result(CompositeGoalStatus.INDETERMINATE, "preprocessing_binding_missing")
        return result(CompositeGoalStatus.WAITING_PREPROCESSING, "preprocessing_not_started")

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
        verified_run_ids["preprocessing_run_id"] = _verified_observable_run_id(
            prep,
            goal=goal,
            plan=snapshot.preprocessing_plan,
            run=snapshot.preprocessing_run,
            step_kind="preprocessing_run",
        )
        waiting = _task_wait_phase(
            prep,
            goal=goal,
            plan=snapshot.preprocessing_plan,
            run=snapshot.preprocessing_run,
            prefix="preprocessing",
        )
        if waiting is not None:
            return result(*waiting)
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

    verified_run_ids["preprocessing_run_id"] = _verified_observable_run_id(
        prep,
        goal=goal,
        plan=snapshot.preprocessing_plan,
        run=snapshot.preprocessing_run,
        step_kind="preprocessing_run",
        verified_source_binding=True,
    )

    if prep.status in {TaskStatus.BLOCKED, TaskStatus.CANCELLED}:
        return result(
            CompositeGoalStatus.BLOCKED,
            "preprocessing_terminal_block",
            proposal_status=outbox.status.value,
        )
    if prep.status is not TaskStatus.COMPLETED:
        waiting = _task_wait_phase(
            prep,
            goal=goal,
            plan=snapshot.preprocessing_plan,
            run=snapshot.preprocessing_run,
            prefix="preprocessing",
        )
        if waiting is not None:
            return result(*waiting, proposal_status=outbox.status.value)
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
        initial_attempts = tuple(
            item for item in snapshot.analysis_attempts if item.work_task_id == initial_task.task_id
        )
        initial_attempt = initial_attempts[0] if len(initial_attempts) == 1 else None
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
    active_attempts = tuple(
        item for item in snapshot.analysis_attempts if item.work_task_id == analysis_task.task_id
    )
    active_attempt = active_attempts[0] if len(active_attempts) == 1 else None
    if (
        analysis_task.task_id != goal.analysis_work_task_id
        or active_attempt is None
        or not snapshot.analysis_attempts
        or max(snapshot.analysis_attempts, key=lambda item: item.attempt_number).work_task_id
        != analysis_task.task_id
        or not _analysis_task_has_proposal(analysis_task, active_attempt.proposal_hash)
    ):
        return result(
            CompositeGoalStatus.INDETERMINATE,
            "analysis_task_reference_mismatch",
            proposal_status=outbox.status.value,
            analysis_status=analysis_task.status.value,
        )

    verified_run_ids["analysis_run_id"] = _verified_observable_run_id(
        analysis_task,
        goal=goal,
        plan=snapshot.analysis_plan,
        run=snapshot.analysis_run,
        step_kind="statistical_run",
    )
    if analysis_task.status in {TaskStatus.BLOCKED, TaskStatus.CANCELLED}:
        return result(
            CompositeGoalStatus.BLOCKED,
            "analysis_terminal_block",
            proposal_status=outbox.status.value,
            analysis_status=analysis_task.status.value,
        )
    waiting = _task_wait_phase(
        analysis_task,
        goal=goal,
        plan=snapshot.analysis_plan,
        run=snapshot.analysis_run,
        prefix="analysis",
    )
    if waiting is not None:
        return result(
            *waiting,
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


def _project_analysis_only_external_goal(
    snapshot: CompositeGoalSnapshot,
    result: Callable[..., CompositeGoalProgress],
    verified_run_ids: dict[str, str | None],
) -> CompositeGoalProgress:
    """Project a direct external-import analysis without inventing a prep child."""

    goal = snapshot.goal
    task = snapshot.analysis_task
    outbox = snapshot.result_interpretation_outbox
    if (
        goal.preprocessing_task_id is not None
        or goal.analysis_proposal_outbox_intent_task_id is not None
        or snapshot.preprocessing_task is not None
        or snapshot.analysis_proposal_outbox is not None
    ):
        return result(CompositeGoalStatus.INDETERMINATE, "analysis_only_child_binding_mismatch")
    if snapshot.conversation_mode != "work" or snapshot.conversation_project_id not in {
        None,
        goal.project_id,
    }:
        return result(CompositeGoalStatus.INDETERMINATE, "analysis_only_conversation_mismatch")
    if not snapshot.analysis_source_valid:
        return result(CompositeGoalStatus.BLOCKED, "analysis_only_source_changed")
    if task is None:
        if snapshot.analysis_attempts or goal.analysis_work_task_id is not None:
            return result(CompositeGoalStatus.INDETERMINATE, "analysis_task_missing")
        return result(CompositeGoalStatus.WAITING_USER, "analysis_draft_user_decision")
    if (
        task.task_id != goal.analysis_work_task_id
        or not _analysis_task_matches(task, goal)
        or len(snapshot.analysis_attempts) == 0
        or max(snapshot.analysis_attempts, key=lambda item: item.attempt_number).work_task_id
        != task.task_id
        or not _analysis_task_attempt_matches(snapshot, task)
    ):
        return result(
            CompositeGoalStatus.INDETERMINATE,
            "analysis_task_reference_mismatch",
            analysis_status=task.status.value,
        )
    verified_run_ids["analysis_run_id"] = _verified_observable_run_id(
        task,
        goal=goal,
        plan=snapshot.analysis_plan,
        run=snapshot.analysis_run,
        step_kind="statistical_run",
    )
    if task.status in {TaskStatus.BLOCKED, TaskStatus.CANCELLED}:
        return result(
            CompositeGoalStatus.BLOCKED,
            "analysis_terminal_block",
            analysis_status=task.status.value,
        )
    waiting = _task_wait_phase(
        task,
        goal=goal,
        plan=snapshot.analysis_plan,
        run=snapshot.analysis_run,
        prefix="analysis",
    )
    if waiting is not None:
        return result(*waiting, analysis_status=task.status.value)
    if task.status is not TaskStatus.COMPLETED:
        return result(
            CompositeGoalStatus.WAITING_ANALYSIS,
            "analysis_in_progress",
            analysis_status=task.status.value,
        )

    if (
        not snapshot.analysis_result_binding_valid
        or goal.result_interpretation_outbox_task_id != task.task_id
        or outbox is None
        or outbox.task_id != task.task_id
        or outbox.conversation_id != goal.conversation_id
        or outbox.project_id != goal.project_id
        or outbox.plan_revision_id != task.authority_refs.plan_revision_id
        or outbox.run_id != task.authority_refs.workflow_run_id
    ):
        return result(
            CompositeGoalStatus.INDETERMINATE,
            "result_interpretation_binding_missing",
            analysis_status=task.status.value,
            result_status=outbox.status.value if outbox is not None else None,
        )
    return _project_result_interpretation(
        goal=goal,
        task=task,
        outbox=outbox,
        message=snapshot.result_continuation_message,
        result=result,
        proposal_status=None,
    )


def _task_wait_phase(
    task: WorkTask,
    *,
    goal: CompositeGoalIdentity,
    plan: CompositeGoalPlanEvidence | None,
    run: CompositeGoalRunEvidence | None,
    prefix: str,
) -> tuple[CompositeGoalStatus, str] | None:
    """Give a narrow phase only when the WorkTask wait binds exact authority."""

    if task.status is TaskStatus.WAITING_RUN:
        run_id = task.authority_refs.workflow_run_id
        waiting = task.waiting_condition
        plan_state: PlanState | None = None
        if (
            run_id is None
            or waiting is None
            or waiting.kind.value != "workflow_run"
            or waiting.reference != run_id
            or run is None
            or run.run_id != run_id
            or run.project_id != goal.project_id
            or run.plan_revision_id != task.authority_refs.plan_revision_id
            or run.plan_revision_id is None
            or not _task_plan_binding_matches(task, goal=goal, plan=plan, run_id=run_id)
        ):
            return CompositeGoalStatus.INDETERMINATE, f"{prefix}_run_authority_mismatch"
        try:
            run_state = WorkflowState(run.state)
        except ValueError:
            return CompositeGoalStatus.INDETERMINATE, f"{prefix}_run_state_unknown"
        try:
            plan_state = PlanState(plan.state) if plan is not None else None
        except ValueError:
            return CompositeGoalStatus.INDETERMINATE, f"{prefix}_plan_state_unknown"
        if run_state not in {
            WorkflowState.QUEUED,
            WorkflowState.RUNNING,
            WorkflowState.CANCELLING,
        } or plan_state not in {PlanState.APPROVED, PlanState.SUPERSEDED}:
            return CompositeGoalStatus.INDETERMINATE, f"{prefix}_run_state_mismatch"
        return CompositeGoalStatus.WAITING_RUN, f"{prefix}_run_pending"

    if task.status is not TaskStatus.WAITING_USER:
        return None
    waiting = task.waiting_condition
    if waiting is None:
        return CompositeGoalStatus.WAITING_USER, f"{prefix}_user_decision"
    plan_id = task.authority_refs.plan_revision_id
    plan_review_references = {
        f"plan:{plan_id}" if plan_id is not None else "",
        *(
            f"step-plan:{task.task_id}:{step.step_id}:{plan_id}"
            for step in task.steps
            if plan_id is not None
        ),
    }
    if waiting.reference in plan_review_references:
        if plan is None or not _task_plan_binding_matches(task, goal=goal, plan=plan, run_id=None):
            return CompositeGoalStatus.INDETERMINATE, f"{prefix}_plan_review_authority_mismatch"
        try:
            plan_state = PlanState(plan.state)
        except ValueError:
            return CompositeGoalStatus.INDETERMINATE, f"{prefix}_plan_state_unknown"
        if plan_state in {
            PlanState.DRAFT,
            PlanState.AWAITING_APPROVAL,
        }:
            return CompositeGoalStatus.WAITING_REVIEW, f"{prefix}_plan_review"
        if plan_state is PlanState.APPROVED:
            return CompositeGoalStatus.WAITING_USER, f"{prefix}_run_start_user"
        return CompositeGoalStatus.INDETERMINATE, f"{prefix}_plan_review_state_mismatch"

    if waiting.reference.startswith("qc:"):
        run_id = waiting.reference.removeprefix("qc:")
        qc_review_pending = run is not None and (
            (
                run.qc_status in {"pending", "awaiting_review"}
                and run.latest_qc_review_state in {None, "draft"}
            )
            or (run.qc_status == "failed" and run.latest_qc_review_state == "draft")
        )
        if (
            not run_id
            or run is None
            or run_id != task.authority_refs.workflow_run_id
            or run.run_id != run_id
            or run.project_id != goal.project_id
            or run.plan_revision_id != plan_id
            or not qc_review_pending
        ):
            return CompositeGoalStatus.INDETERMINATE, f"{prefix}_qc_review_authority_mismatch"
        try:
            workflow_state = WorkflowState(run.state)
        except ValueError:
            return CompositeGoalStatus.INDETERMINATE, f"{prefix}_run_state_unknown"
        if workflow_state is not WorkflowState.QC_REVIEW or not _task_plan_binding_matches(
            task, goal=goal, plan=plan, run_id=run_id
        ):
            return CompositeGoalStatus.INDETERMINATE, f"{prefix}_qc_review_state_mismatch"
        return CompositeGoalStatus.WAITING_REVIEW, f"{prefix}_qc_review"

    return CompositeGoalStatus.WAITING_USER, f"{prefix}_user_decision"


def _task_plan_binding_matches(
    task: WorkTask,
    *,
    goal: CompositeGoalIdentity,
    plan: CompositeGoalPlanEvidence | None,
    run_id: str | None,
) -> bool:
    """Match the Task and its unique step to the exact Plan input and Run."""

    plan_id = task.authority_refs.plan_revision_id
    if (
        plan is None
        or plan_id is None
        or task.authority_refs.input_revision is None
        or plan.plan_revision_id != plan_id
        or plan.project_id != goal.project_id
        or plan.input_revision != task.authority_refs.input_revision
    ):
        return False
    matches = tuple(
        step
        for step in task.steps
        if step.authority_refs.plan_revision_id == plan_id
        and step.authority_refs.workflow_run_id == run_id
    )
    if len(matches) != 1:
        return False
    step = matches[0]
    return (
        step.authority_refs.input_revision == task.authority_refs.input_revision
        and step.kind in {"preprocessing_run", "statistical_run"}
        and (
            (run_id is None and step.status.value == "ready")
            or (run_id is not None and step.status.value == "waiting")
        )
    )


def _verified_observable_run_id(
    task: WorkTask | None,
    *,
    goal: CompositeGoalIdentity,
    plan: CompositeGoalPlanEvidence | None,
    run: CompositeGoalRunEvidence | None,
    step_kind: str,
    verified_source_binding: bool = False,
) -> str | None:
    """Expose only an exact, still-observable Run bound to this goal and step."""

    if task is None or plan is None or run is None:
        return None
    refs = task.authority_refs
    run_id = refs.workflow_run_id
    plan_id = refs.plan_revision_id
    input_revision = refs.input_revision
    task_goal_matches = (
        refs.analysis_intent_task_id == goal.intent_task_id
        and refs.analysis_request_hash == goal.analysis_request_hash
    )
    if (
        refs.conversation_id != goal.conversation_id
        or (not task_goal_matches and not verified_source_binding)
        or run_id is None
        or plan_id is None
        or input_revision is None
        or plan.plan_revision_id != plan_id
        or plan.project_id != goal.project_id
        or plan.input_revision != input_revision
        or run.run_id != run_id
        or run.project_id != goal.project_id
        or run.plan_revision_id != plan_id
    ):
        return None
    if goal.goal_kind == "analysis_only_external_import" and (
        plan.manifest_hash != goal.source_manifest_hash
        or plan.source_external_import_id != goal.source_external_import_id
        or plan.source_external_import_hash != goal.source_inclusion_hash
    ):
        return None
    try:
        plan_state = PlanState(plan.state)
        run_state = WorkflowState(run.state)
    except ValueError:
        return None
    if plan_state not in {PlanState.APPROVED, PlanState.SUPERSEDED}:
        return None
    if run_state not in {
        WorkflowState.QUEUED,
        WorkflowState.RUNNING,
        WorkflowState.CANCELLING,
        WorkflowState.QC_REVIEW,
        WorkflowState.FAILED_RETRYABLE,
        WorkflowState.FAILED_TERMINAL,
        WorkflowState.TIMED_OUT,
        WorkflowState.CANCELLED,
    }:
        return None

    referenced_steps = tuple(
        step for step in task.steps if step.authority_refs.workflow_run_id == run_id
    )
    if len(referenced_steps) != 1:
        return None
    step = referenced_steps[0]
    step_refs = step.authority_refs
    if (
        step.kind != step_kind
        or step_refs.conversation_id != refs.conversation_id
        or step_refs.analysis_intent_task_id != refs.analysis_intent_task_id
        or step_refs.analysis_request_hash != refs.analysis_request_hash
        or step_refs.plan_revision_id != plan_id
        or step_refs.workflow_run_id != run_id
        or step_refs.input_revision != input_revision
    ):
        return None

    waiting_task_states = {
        TaskStatus.WAITING_RUN,
        TaskStatus.WAITING_USER,
        TaskStatus.WAITING_DEPENDENCY,
    }
    if task.status in waiting_task_states and step.status is TaskStepStatus.WAITING:
        return run_id
    if task.status in {TaskStatus.BLOCKED, TaskStatus.CANCELLED} and step.status in {
        TaskStepStatus.FAILED,
        TaskStepStatus.BLOCKED,
        TaskStepStatus.SKIPPED,
    }:
        return run_id
    return None


def _analysis_task_attempt_matches(snapshot: CompositeGoalSnapshot, task: WorkTask) -> bool:
    """Require the analysis Run to belong to the currently selected proposal attempt."""

    matching = tuple(
        item for item in snapshot.analysis_attempts if item.work_task_id == task.task_id
    )
    if len(matching) != 1 or not snapshot.analysis_attempts:
        return False
    latest = max(snapshot.analysis_attempts, key=lambda item: item.attempt_number)
    return latest.work_task_id == task.task_id and _analysis_task_has_proposal(
        task, matching[0].proposal_hash
    )


def _project_result_interpretation(
    *,
    goal: CompositeGoalIdentity,
    task: WorkTask,
    outbox: StatisticalResultInterpretationOutbox,
    message: CompositeGoalContinuationMessage | None,
    result: Callable[..., CompositeGoalProgress],
    proposal_status: str | None,
) -> CompositeGoalProgress:
    result_status = outbox.status.value
    if result_status == "blocked":
        status, reason = CompositeGoalStatus.BLOCKED, "result_interpretation_blocked"
    elif result_status == "waiting_user":
        status, reason = CompositeGoalStatus.WAITING_USER, "result_interpretation_user_decision"
    elif result_status == "completed" and outbox.continuation_message_id:
        message_payload = message.payload if message is not None else {}
        completion = message_payload.get("statistical_result_interpretation")
        envelope = message_payload.get("analysis_intent_envelope")
        if (
            not isinstance(envelope, dict)
            or (
                envelope.get("status") != "completed"
                or envelope.get("task_id") != goal.intent_task_id
                or envelope.get("conversation_id") != goal.conversation_id
                or envelope.get("project_id") != goal.project_id
                or envelope.get("analysis_request_hash") != goal.analysis_request_hash
            )
            or not isinstance(completion, dict)
            or (
                message is None
                or message.message_id != outbox.continuation_message_id
                or message.conversation_id != goal.conversation_id
                or message.role != "assistant"
                or completion.get("task_id") != task.task_id
                or completion.get("status") != "completed"
                or completion.get("result_id") != outbox.result_id
                or completion.get("result_version") != outbox.result_version
            )
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
        analysis_status=task.status.value,
        proposal_status=proposal_status,
        result_status=result_status,
        continuation_message_id=outbox.continuation_message_id,
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
            dependency.get("task_kind") != goal.goal_kind
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
