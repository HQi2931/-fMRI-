from __future__ import annotations

import pytest

from neuroagent.application.automation.analysis_outbox import (
    AnalysisOutboxStatus,
    AnalysisProposalOutbox,
)
from neuroagent.application.automation.composite_goal import (
    CompositeGoalAnalysisAttempt,
    CompositeGoalContinuationMessage,
    CompositeGoalHandoffAction,
    CompositeGoalIdentity,
    CompositeGoalPlanEvidence,
    CompositeGoalRunEvidence,
    CompositeGoalSnapshot,
    CompositeGoalStatus,
    project_composite_goal,
    resolve_composite_goal_handoff,
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
        preprocessing_plan=CompositeGoalPlanEvidence(
            plan_revision_id="preprocessing-plan",
            project_id=project_id,
            state="approved",
            manifest_hash="d" * 64,
            plan_hash="2" * 64,
            input_revision="d" * 64,
        ),
        preprocessing_run=CompositeGoalRunEvidence(
            run_id="preprocessing-run",
            project_id=project_id,
            plan_revision_id="preprocessing-plan",
            state="succeeded",
            version=3,
            qc_status="approved",
            latest_qc_review_state="approved",
        ),
        analysis_plan=CompositeGoalPlanEvidence(
            plan_revision_id="analysis-plan",
            project_id=project_id,
            state="approved",
            manifest_hash="d" * 64,
            plan_hash="3" * 64,
            input_revision="f" * 64,
        ),
        analysis_run=CompositeGoalRunEvidence(
            run_id="analysis-run",
            project_id=project_id,
            plan_revision_id="analysis-plan",
            state="succeeded",
            version=4,
        ),
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


def test_projected_composite_phase_distinguishes_exact_plan_review() -> None:
    snapshot = _completed_snapshot()
    prep = snapshot.preprocessing_task
    assert prep is not None
    snapshot = snapshot.model_copy(
        update={
            "goal": snapshot.goal.model_copy(
                update={
                    "analysis_proposal_outbox_intent_task_id": None,
                    "analysis_work_task_id": None,
                    "result_interpretation_outbox_task_id": None,
                }
            ),
            "preprocessing_task": prep.model_copy(
                update={
                    "status": TaskStatus.WAITING_USER,
                    "waiting_condition": WaitingCondition(
                        kind=WaitingKind.USER_DECISION,
                        reference="plan:preprocessing-plan",
                        reason="review the exact Plan",
                    ),
                    "authority_refs": prep.authority_refs.model_copy(
                        update={"workflow_run_id": None}
                    ),
                    "steps": tuple(
                        step.model_copy(
                            update={
                                "status": TaskStepStatus.READY,
                                "authority_refs": step.authority_refs.model_copy(
                                    update={"workflow_run_id": None}
                                ),
                            }
                        )
                        for step in prep.steps
                    ),
                }
            ),
            "preprocessing_run": None,
            "analysis_proposal_outbox": None,
            "analysis_attempts": (),
            "proposal_analysis_task": None,
            "analysis_task": None,
            "result_interpretation_outbox": None,
            "result_continuation_message": None,
            "preprocessing_plan": snapshot.preprocessing_plan.model_copy(
                update={"state": "awaiting_approval"}
            ),
        }
    )

    progress = project_composite_goal(snapshot)

    assert progress.status is CompositeGoalStatus.WAITING_REVIEW
    assert progress.reason_code == "preprocessing_plan_review"


def test_projected_composite_phase_waits_on_the_exact_bound_run() -> None:
    snapshot = _completed_snapshot()
    prep = snapshot.preprocessing_task
    assert prep is not None
    snapshot = snapshot.model_copy(
        update={
            "preprocessing_task": prep.model_copy(
                update={
                    "status": TaskStatus.WAITING_RUN,
                    "waiting_condition": WaitingCondition(
                        kind=WaitingKind.WORKFLOW_RUN,
                        reference="preprocessing-run",
                        reason="wait for the existing Run",
                    ),
                    "steps": tuple(
                        step.model_copy(update={"status": TaskStepStatus.WAITING})
                        for step in prep.steps
                    ),
                }
            ),
            "preprocessing_run": snapshot.preprocessing_run.model_copy(update={"state": "running"}),
        }
    )

    progress = project_composite_goal(snapshot)

    assert progress.status is CompositeGoalStatus.WAITING_RUN
    assert progress.reason_code == "preprocessing_run_pending"
    assert progress.preprocessing_run_id == "preprocessing-run"
    assert progress.analysis_run_id is None


def test_progress_exposes_only_observable_run_states() -> None:
    snapshot = _completed_snapshot()
    prep = snapshot.preprocessing_task
    assert prep is not None
    waiting_prep = prep.model_copy(
        update={
            "status": TaskStatus.WAITING_RUN,
            "waiting_condition": WaitingCondition(
                kind=WaitingKind.WORKFLOW_RUN,
                reference="preprocessing-run",
                reason="wait for the exact bound Run",
            ),
            "steps": tuple(
                step.model_copy(update={"status": TaskStepStatus.WAITING}) for step in prep.steps
            ),
        }
    )
    active = snapshot.model_copy(
        update={
            "preprocessing_task": waiting_prep,
            "preprocessing_run": snapshot.preprocessing_run.model_copy(update={"state": "queued"}),
        }
    )

    progress = project_composite_goal(active)

    assert progress.preprocessing_run_id == "preprocessing-run"
    assert progress.analysis_run_id is None

    succeeded = active.model_copy(
        update={
            "preprocessing_run": active.preprocessing_run.model_copy(update={"state": "succeeded"})
        }
    )
    succeeded_progress = project_composite_goal(succeeded)
    assert succeeded_progress.preprocessing_run_id is None


def test_progress_exposes_failed_run_after_failure_blocks_the_task() -> None:
    snapshot = _completed_snapshot()
    prep = snapshot.preprocessing_task
    assert prep is not None
    failed_prep = prep.model_copy(
        update={
            "status": TaskStatus.BLOCKED,
            "waiting_condition": None,
            "steps": tuple(
                step.model_copy(update={"status": TaskStepStatus.FAILED}) for step in prep.steps
            ),
        }
    )
    failed = snapshot.model_copy(
        update={
            "preprocessing_task": failed_prep,
            "preprocessing_run": snapshot.preprocessing_run.model_copy(
                update={"state": "failed_terminal"}
            ),
        }
    )

    progress = project_composite_goal(failed)

    assert progress.status is CompositeGoalStatus.BLOCKED
    assert progress.preprocessing_run_id == "preprocessing-run"
    assert progress.analysis_run_id is None


def test_shared_preprocessing_run_id_uses_the_exact_proposal_outbox_binding() -> None:
    snapshot = _completed_snapshot()
    prep = snapshot.preprocessing_task
    assert prep is not None
    goal = snapshot.goal.model_copy(
        update={
            "intent_task_id": "intent-composite-second",
            "analysis_request_hash": "9" * 64,
            "analysis_proposal_outbox_intent_task_id": "intent-composite-second",
        }
    )
    outbox = snapshot.analysis_proposal_outbox
    assert outbox is not None
    shared_source = snapshot.model_copy(
        update={
            "goal": goal,
            "preprocessing_task": prep.model_copy(
                update={
                    "status": TaskStatus.WAITING_RUN,
                    "waiting_condition": WaitingCondition(
                        kind=WaitingKind.WORKFLOW_RUN,
                        reference="preprocessing-run",
                        reason="wait for the shared preprocessing Run",
                    ),
                    "steps": tuple(
                        step.model_copy(update={"status": TaskStepStatus.WAITING})
                        for step in prep.steps
                    ),
                }
            ),
            "preprocessing_run": snapshot.preprocessing_run.model_copy(update={"state": "running"}),
            "analysis_proposal_outbox": outbox.model_copy(
                update={
                    "intent_task_id": goal.intent_task_id,
                    "analysis_request_hash": goal.analysis_request_hash,
                }
            ),
        }
    )

    progress = project_composite_goal(shared_source)

    assert progress.preprocessing_run_id == "preprocessing-run"
    assert progress.analysis_run_id is None


def test_analysis_run_id_requires_current_attempt_binding() -> None:
    snapshot = _completed_snapshot()
    analysis = snapshot.analysis_task
    assert analysis is not None
    waiting_analysis = analysis.model_copy(
        update={
            "status": TaskStatus.WAITING_RUN,
            "waiting_condition": WaitingCondition(
                kind=WaitingKind.WORKFLOW_RUN,
                reference="analysis-run",
                reason="wait for the exact bound Run",
            ),
            "steps": tuple(
                step.model_copy(update={"status": TaskStepStatus.WAITING})
                for step in analysis.steps
            ),
        }
    )
    active = snapshot.model_copy(
        update={
            "analysis_task": waiting_analysis,
            "analysis_run": snapshot.analysis_run.model_copy(update={"state": "running"}),
        }
    )

    progress = project_composite_goal(active)

    assert progress.status is CompositeGoalStatus.WAITING_RUN
    assert progress.analysis_run_id == "analysis-run"
    assert progress.preprocessing_run_id is None

    assert active.analysis_attempts
    stale_attempt = active.analysis_attempts[-1].model_copy(update={"proposal_hash": "e" * 64})
    mismatched = active.model_copy(
        update={"analysis_attempts": (*active.analysis_attempts[:-1], stale_attempt)}
    )
    mismatched_progress = project_composite_goal(mismatched)
    assert mismatched_progress.status is CompositeGoalStatus.INDETERMINATE
    assert mismatched_progress.analysis_run_id is None


@pytest.mark.parametrize("run_state", ["qc_review", "succeeded", "failed_terminal", "cancelled"])
def test_projected_composite_phase_does_not_call_terminal_or_qc_run_waiting(
    run_state: str,
) -> None:
    snapshot = _completed_snapshot()
    prep = snapshot.preprocessing_task
    assert prep is not None
    snapshot = snapshot.model_copy(
        update={
            "preprocessing_task": prep.model_copy(
                update={
                    "status": TaskStatus.WAITING_RUN,
                    "waiting_condition": WaitingCondition(
                        kind=WaitingKind.WORKFLOW_RUN,
                        reference="preprocessing-run",
                        reason="wait for the bound Run",
                    ),
                    "steps": tuple(
                        step.model_copy(update={"status": TaskStepStatus.WAITING})
                        for step in prep.steps
                    ),
                }
            ),
            "preprocessing_run": snapshot.preprocessing_run.model_copy(update={"state": run_state}),
        }
    )

    progress = project_composite_goal(snapshot)

    assert progress.status is CompositeGoalStatus.INDETERMINATE
    assert progress.reason_code == "preprocessing_run_state_mismatch"


@pytest.mark.parametrize(
    "plan_update",
    [
        None,
        {"project_id": "another-project"},
        {"input_revision": "e" * 64},
        {"plan_revision_id": "another-plan"},
    ],
)
def test_projected_composite_phase_fails_closed_without_exact_plan_input(
    plan_update: dict[str, str] | None,
) -> None:
    snapshot = _completed_snapshot()
    prep = snapshot.preprocessing_task
    assert prep is not None
    plan = snapshot.preprocessing_plan
    assert plan is not None
    snapshot = snapshot.model_copy(
        update={
            "preprocessing_task": prep.model_copy(
                update={
                    "status": TaskStatus.WAITING_RUN,
                    "waiting_condition": WaitingCondition(
                        kind=WaitingKind.WORKFLOW_RUN,
                        reference="preprocessing-run",
                        reason="wait for the bound Run",
                    ),
                    "steps": tuple(
                        step.model_copy(update={"status": TaskStepStatus.WAITING})
                        for step in prep.steps
                    ),
                }
            ),
            "preprocessing_run": snapshot.preprocessing_run.model_copy(update={"state": "running"}),
            "preprocessing_plan": (
                None if plan_update is None else plan.model_copy(update=plan_update)
            ),
        }
    )

    progress = project_composite_goal(snapshot)

    assert progress.status is CompositeGoalStatus.INDETERMINATE
    assert progress.reason_code == "preprocessing_run_authority_mismatch"
    assert progress.preprocessing_run_id is None


def test_projected_composite_qc_review_requires_qc_review_run_state() -> None:
    snapshot = _completed_snapshot()
    prep = snapshot.preprocessing_task
    assert prep is not None
    snapshot = snapshot.model_copy(
        update={
            "preprocessing_task": prep.model_copy(
                update={
                    "status": TaskStatus.WAITING_USER,
                    "waiting_condition": WaitingCondition(
                        kind=WaitingKind.USER_DECISION,
                        reference="qc:preprocessing-run",
                        reason="inspect the QC decision",
                    ),
                    "steps": tuple(
                        step.model_copy(update={"status": TaskStepStatus.WAITING})
                        for step in prep.steps
                    ),
                }
            ),
            "preprocessing_run": snapshot.preprocessing_run.model_copy(
                update={
                    "state": "qc_review",
                    "qc_status": "pending",
                    "latest_qc_review_state": "draft",
                }
            ),
        }
    )

    progress = project_composite_goal(snapshot)

    assert progress.status is CompositeGoalStatus.WAITING_REVIEW
    assert progress.reason_code == "preprocessing_qc_review"


def test_projected_composite_does_not_call_succeeded_run_with_pending_qc_reviewable() -> None:
    snapshot = _completed_snapshot()
    prep = snapshot.preprocessing_task
    assert prep is not None
    snapshot = snapshot.model_copy(
        update={
            "preprocessing_task": prep.model_copy(
                update={
                    "status": TaskStatus.WAITING_USER,
                    "waiting_condition": WaitingCondition(
                        kind=WaitingKind.USER_DECISION,
                        reference="qc:preprocessing-run",
                        reason="inspect the QC decision",
                    ),
                    "steps": tuple(
                        step.model_copy(update={"status": TaskStepStatus.WAITING})
                        for step in prep.steps
                    ),
                }
            ),
            "preprocessing_run": snapshot.preprocessing_run.model_copy(
                update={
                    "state": "succeeded",
                    "qc_status": "pending",
                    "latest_qc_review_state": "draft",
                }
            ),
        }
    )

    progress = project_composite_goal(snapshot)

    assert progress.status is CompositeGoalStatus.INDETERMINATE
    assert progress.reason_code == "preprocessing_qc_review_state_mismatch"


def test_projected_composite_phase_fails_closed_on_run_reference_mismatch() -> None:
    snapshot = _completed_snapshot()
    prep = snapshot.preprocessing_task
    assert prep is not None
    snapshot = snapshot.model_copy(
        update={
            "preprocessing_task": prep.model_copy(
                update={
                    "status": TaskStatus.WAITING_RUN,
                    "waiting_condition": WaitingCondition(
                        kind=WaitingKind.WORKFLOW_RUN,
                        reference="different-run",
                        reason="stale wait",
                    ),
                }
            ),
        }
    )

    progress = project_composite_goal(snapshot)

    assert progress.status is CompositeGoalStatus.INDETERMINATE
    assert progress.reason_code == "preprocessing_run_authority_mismatch"
    assert progress.preprocessing_run_id is None


@pytest.mark.parametrize("mismatch", ["plan", "run", "input_revision", "step_status"])
def test_projected_composite_phase_fails_closed_on_task_step_mismatch(
    mismatch: str,
) -> None:
    snapshot = _completed_snapshot()
    prep = snapshot.preprocessing_task
    assert prep is not None
    step = prep.steps[0]
    step_update: dict[str, object] = {"status": TaskStepStatus.WAITING}
    if mismatch == "plan":
        step_update["authority_refs"] = step.authority_refs.model_copy(
            update={"plan_revision_id": "another-plan"}
        )
    elif mismatch == "run":
        step_update["authority_refs"] = step.authority_refs.model_copy(
            update={"workflow_run_id": "another-run"}
        )
    elif mismatch == "input_revision":
        step_update["authority_refs"] = step.authority_refs.model_copy(
            update={"input_revision": "e" * 64}
        )
    else:
        step_update["status"] = TaskStepStatus.READY
    snapshot = snapshot.model_copy(
        update={
            "preprocessing_task": prep.model_copy(
                update={
                    "status": TaskStatus.WAITING_RUN,
                    "waiting_condition": WaitingCondition(
                        kind=WaitingKind.WORKFLOW_RUN,
                        reference="preprocessing-run",
                        reason="wait for the bound Run",
                    ),
                    "steps": (step.model_copy(update=step_update),),
                }
            ),
            "preprocessing_run": snapshot.preprocessing_run.model_copy(update={"state": "running"}),
        }
    )

    progress = project_composite_goal(snapshot)

    assert progress.status is CompositeGoalStatus.INDETERMINATE
    assert progress.reason_code == "preprocessing_run_authority_mismatch"


@pytest.mark.parametrize(
    ("outbox_status", "expected_action", "expected_phase"),
    [
        (
            AnalysisOutboxStatus.READY,
            CompositeGoalHandoffAction.PREPARE_ANALYSIS_PROPOSAL,
            CompositeGoalStatus.WAITING_ANALYSIS,
        ),
        (
            AnalysisOutboxStatus.PROPOSAL_READY,
            CompositeGoalHandoffAction.NONE,
            CompositeGoalStatus.COMPLETED,
        ),
    ],
)
def test_handoff_decision_only_resumes_the_registered_ready_proposal(
    outbox_status, expected_action, expected_phase
) -> None:
    snapshot = _completed_snapshot()
    proposal = snapshot.analysis_proposal_outbox
    assert proposal is not None
    snapshot = snapshot.model_copy(
        update={"analysis_proposal_outbox": proposal.model_copy(update={"status": outbox_status})}
    )

    decision = resolve_composite_goal_handoff(snapshot)

    assert decision.phase is expected_phase
    assert decision.action is expected_action
    assert decision.target_id == (
        snapshot.goal.intent_task_id
        if expected_action is not CompositeGoalHandoffAction.NONE
        else None
    )


def test_handoff_decision_only_resumes_an_exact_ready_result_interpretation() -> None:
    snapshot = _completed_snapshot()
    interpretation = snapshot.result_interpretation_outbox
    assert interpretation is not None
    snapshot = snapshot.model_copy(
        update={
            "result_interpretation_outbox": interpretation.model_copy(
                update={"status": ResultInterpretationStatus.READY}
            )
        }
    )

    decision = resolve_composite_goal_handoff(snapshot)

    assert decision.phase is CompositeGoalStatus.WAITING_RESULT_INTERPRETATION
    assert decision.action is CompositeGoalHandoffAction.PREPARE_RESULT_INTERPRETATION
    assert decision.target_id == interpretation.task_id
