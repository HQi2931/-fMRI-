from __future__ import annotations

import pytest

from neuroagent.application.automation.analysis_outbox import (
    AnalysisOutboxStatus,
    AnalysisProposalOutbox,
)
from neuroagent.application.automation.composite_goal import (
    CompositeGoalAnalysisAttempt,
    CompositeGoalContinuationMessage,
    CompositeGoalIdentity,
    CompositeGoalSnapshot,
    CompositeGoalStatus,
    project_composite_goal,
)
from neuroagent.application.automation.models import (
    AuthorityReferences,
    TaskBudget,
    TaskStatus,
    TaskStep,
    TaskStepStatus,
    WaitingCondition,
    WaitingKind,
    WorkGoal,
    WorkTask,
)
from neuroagent.application.automation.result_interpretation import (
    ResultInterpretationStatus,
    StatisticalResultInterpretationOutbox,
)
from neuroagent.application.automation.service import WorkTaskApplicationService
from neuroagent.application.errors import NotFoundError


def _task(
    task_id: str,
    *,
    status: TaskStatus,
    refs: AuthorityReferences,
    proposal_hash: str | None = None,
    step_kind: str,
) -> WorkTask:
    return WorkTask(
        task_id=task_id,
        goal=WorkGoal(description="synthetic goal", completion_conditions=("verified",)),
        status=status,
        waiting_condition=(
            WaitingCondition(
                kind=WaitingKind.USER_DECISION,
                reference="synthetic-proposal",
                reason="awaiting review",
            )
            if status is TaskStatus.WAITING_USER
            else None
        ),
        authority_refs=refs,
        steps=(
            TaskStep(
                step_id=f"{task_id}-step",
                sequence=0,
                kind=step_kind,
                status=(
                    TaskStepStatus.SUCCEEDED
                    if status is TaskStatus.COMPLETED
                    else TaskStepStatus.READY
                ),
                input_version=proposal_hash,
                authority_refs=refs,
            ),
        ),
        budget=TaskBudget(
            max_steps=10,
            max_model_calls=10,
            max_tokens=1000,
            max_elapsed_seconds=600,
        ),
    )


def _completed_snapshot() -> CompositeGoalSnapshot:
    intent_id = "intent-composite"
    conversation_id = "conversation-composite"
    project_id = "project-composite"
    request_hash = "a" * 64
    first_hash = "b" * 64
    active_hash = "c" * 64
    preprocessing = _task(
        "preprocessing-task",
        status=TaskStatus.COMPLETED,
        refs=AuthorityReferences(
            conversation_id=conversation_id,
            analysis_intent_task_id=intent_id,
            analysis_request_hash=request_hash,
            plan_revision_id="preprocessing-plan",
            workflow_run_id="preprocessing-run",
            input_revision="d" * 64,
        ),
        step_kind="preprocessing_run",
    )
    first_analysis = _task(
        "analysis-task-first",
        status=TaskStatus.WAITING_USER,
        refs=AuthorityReferences(
            conversation_id=conversation_id,
            analysis_intent_task_id=intent_id,
            analysis_request_hash=request_hash,
            input_revision="e" * 64,
        ),
        proposal_hash=first_hash,
        step_kind="statistical_run",
    )
    active_analysis = _task(
        "analysis-task-active",
        status=TaskStatus.COMPLETED,
        refs=AuthorityReferences(
            conversation_id=conversation_id,
            analysis_intent_task_id=intent_id,
            analysis_request_hash=request_hash,
            plan_revision_id="analysis-plan",
            workflow_run_id="analysis-run",
            input_revision="f" * 64,
        ),
        proposal_hash=active_hash,
        step_kind="statistical_run",
    )
    goal = CompositeGoalIdentity(
        intent_task_id=intent_id,
        conversation_id=conversation_id,
        project_id=project_id,
        analysis_request_hash=request_hash,
        preprocessing_task_id=preprocessing.task_id,
        analysis_proposal_outbox_intent_task_id=intent_id,
        analysis_work_task_id=active_analysis.task_id,
        result_interpretation_outbox_task_id=active_analysis.task_id,
    )
    proposal_outbox = AnalysisProposalOutbox(
        intent_task_id=intent_id,
        conversation_id=conversation_id,
        project_id=project_id,
        preprocessing_task_id=preprocessing.task_id,
        plan_revision_id="preprocessing-plan",
        manifest_hash="d" * 64,
        run_id="preprocessing-run",
        analysis_request_hash=request_hash,
        status=AnalysisOutboxStatus.PROPOSAL_READY,
        continuation_message_id="initial-proposal-message",
        analysis_work_task_id=first_analysis.task_id,
    )
    result_outbox = StatisticalResultInterpretationOutbox(
        task_id=active_analysis.task_id,
        conversation_id=conversation_id,
        project_id=project_id,
        run_id="analysis-run",
        plan_revision_id="analysis-plan",
        result_id="stat-result-1",
        result_bundle_hash="1" * 64,
        result_version=3,
        artifact_ids=("result-artifact",),
        status=ResultInterpretationStatus.COMPLETED,
        continuation_message_id="result-message",
    )
    message = CompositeGoalContinuationMessage(
        message_id="result-message",
        conversation_id=conversation_id,
        role="assistant",
        payload={
            "statistical_result_interpretation": {
                "task_id": active_analysis.task_id,
                "status": "completed",
                "result_id": "stat-result-1",
                "result_version": 3,
            }
        },
    )
    return CompositeGoalSnapshot(
        goal=goal,
        conversation_mode="work",
        conversation_project_id=project_id,
        preprocessing_plan_revision_id="preprocessing-plan",
        preprocessing_plan_project_id=project_id,
        preprocessing_plan_manifest_hash="d" * 64,
        preprocessing_task=preprocessing,
        analysis_proposal_outbox=proposal_outbox,
        analysis_attempts=(
            CompositeGoalAnalysisAttempt(
                attempt_number=1,
                work_task_id=first_analysis.task_id,
                proposal_hash=first_hash,
            ),
            CompositeGoalAnalysisAttempt(
                attempt_number=2,
                work_task_id=active_analysis.task_id,
                proposal_hash=active_hash,
            ),
        ),
        proposal_analysis_task=first_analysis,
        analysis_task=active_analysis,
        result_interpretation_outbox=result_outbox,
        result_continuation_message=message,
    )


def test_result_projection_uses_active_attempt_after_proposal_revision() -> None:
    progress = project_composite_goal(_completed_snapshot())

    assert progress.status is CompositeGoalStatus.COMPLETED
    assert progress.reason_code == "result_interpretation_delivered"
    assert progress.continuation_message_id == "result-message"


def test_late_initial_attempt_cannot_project_goal_completion() -> None:
    snapshot = _completed_snapshot()
    late_old_task = snapshot.proposal_analysis_task
    assert late_old_task is not None
    snapshot = snapshot.model_copy(update={"analysis_task": late_old_task})

    progress = project_composite_goal(snapshot)

    assert progress.status is CompositeGoalStatus.INDETERMINATE
    assert progress.reason_code == "analysis_task_reference_mismatch"


@pytest.mark.parametrize(
    "message_update",
    [
        {"message_id": "other-message"},
        {"conversation_id": "other-conversation"},
        {"role": "user"},
        {
            "payload": {
                "statistical_result_interpretation": {
                    "task_id": "analysis-task-first",
                    "status": "completed",
                    "result_id": "stat-result-1",
                    "result_version": 3,
                }
            }
        },
        {
            "payload": {
                "statistical_result_interpretation": {
                    "task_id": "analysis-task-active",
                    "status": "completed",
                    "result_id": "other-result",
                    "result_version": 3,
                }
            }
        },
        {
            "payload": {
                "statistical_result_interpretation": {
                    "task_id": "analysis-task-active",
                    "status": "completed",
                    "result_id": "stat-result-1",
                    "result_version": 2,
                }
            }
        },
    ],
)
def test_completion_requires_exact_assistant_continuation_evidence(message_update) -> None:
    snapshot = _completed_snapshot()
    message = snapshot.result_continuation_message
    assert message is not None
    snapshot = snapshot.model_copy(
        update={"result_continuation_message": message.model_copy(update=message_update)}
    )

    progress = project_composite_goal(snapshot)

    assert progress.status is CompositeGoalStatus.INDETERMINATE
    assert progress.reason_code == "result_continuation_mismatch"


def test_scoped_query_accepts_unbound_work_conversation_but_checks_plan_project() -> None:
    snapshot = _completed_snapshot().model_copy(update={"conversation_project_id": None})

    WorkTaskApplicationService._require_composite_goal_scope(
        snapshot, "conversation-composite", "intent-composite"
    )
    with pytest.raises(NotFoundError):
        WorkTaskApplicationService._require_composite_goal_scope(
            snapshot.model_copy(update={"preprocessing_plan_project_id": "other-project"}),
            "conversation-composite",
            "intent-composite",
        )
