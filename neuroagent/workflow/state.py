"""Pure plan and workflow state transition rules."""

from __future__ import annotations

from neuroagent.application.contracts import PlanState, WorkflowState
from neuroagent.application.errors import ConflictError

PLAN_TRANSITIONS: dict[PlanState, frozenset[PlanState]] = {
    PlanState.DRAFT: frozenset({PlanState.VALIDATING, PlanState.SUPERSEDED}),
    PlanState.VALIDATING: frozenset(
        {PlanState.DRAFT, PlanState.AWAITING_APPROVAL, PlanState.SUPERSEDED}
    ),
    PlanState.AWAITING_APPROVAL: frozenset(
        {PlanState.APPROVED, PlanState.DRAFT, PlanState.SUPERSEDED}
    ),
    PlanState.APPROVED: frozenset({PlanState.SUPERSEDED}),
    PlanState.SUPERSEDED: frozenset(),
}


WORKFLOW_TRANSITIONS: dict[WorkflowState, frozenset[WorkflowState]] = {
    WorkflowState.QUEUED: frozenset(
        {
            WorkflowState.RUNNING,
            WorkflowState.FAILED_TERMINAL,
            WorkflowState.CANCELLED,
        }
    ),
    WorkflowState.RUNNING: frozenset(
        {
            WorkflowState.CANCELLING,
            WorkflowState.QC_REVIEW,
            WorkflowState.SUCCEEDED,
            WorkflowState.FAILED_RETRYABLE,
            WorkflowState.FAILED_TERMINAL,
            WorkflowState.TIMED_OUT,
        }
    ),
    WorkflowState.CANCELLING: frozenset({WorkflowState.CANCELLED, WorkflowState.FAILED_TERMINAL}),
    WorkflowState.QC_REVIEW: frozenset({WorkflowState.SUCCEEDED, WorkflowState.FAILED_TERMINAL}),
    WorkflowState.FAILED_RETRYABLE: frozenset({WorkflowState.QUEUED, WorkflowState.CANCELLED}),
    WorkflowState.TIMED_OUT: frozenset(
        {WorkflowState.QUEUED, WorkflowState.FAILED_TERMINAL, WorkflowState.CANCELLED}
    ),
    WorkflowState.SUCCEEDED: frozenset(),
    WorkflowState.FAILED_TERMINAL: frozenset(),
    WorkflowState.CANCELLED: frozenset(),
}


TERMINAL_WORKFLOW_STATES = frozenset(
    {
        WorkflowState.SUCCEEDED,
        WorkflowState.FAILED_TERMINAL,
        WorkflowState.CANCELLED,
    }
)


def validate_plan_transition(current: PlanState, target: PlanState) -> None:
    if target not in PLAN_TRANSITIONS[current]:
        raise ConflictError(
            "invalid_plan_transition",
            f"计划不能从 {current.value} 转换到 {target.value}。",
            current=current.value,
            target=target.value,
        )


def validate_workflow_transition(current: WorkflowState, target: WorkflowState) -> None:
    if target not in WORKFLOW_TRANSITIONS[current]:
        raise ConflictError(
            "invalid_workflow_transition",
            f"运行不能从 {current.value} 转换到 {target.value}。",
            current=current.value,
            target=target.value,
        )
