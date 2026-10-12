"""Persistent user-goal automation contracts and pure state rules."""

from neuroagent.application.automation.models import (
    AuthorityReferences,
    EventConsumptionDecision,
    EventCursor,
    TaskBudget,
    TaskStatus,
    TaskStep,
    TaskStepStatus,
    WaitingCondition,
    WaitingKind,
    WorkEventIdentity,
    WorkGoal,
    WorkTask,
    advance_event_cursor,
    decide_event_consumption,
    transition_task,
    transition_task_step,
)
from neuroagent.application.automation.ports import WorkTaskRepositoryPort
from neuroagent.application.automation.service import WorkTaskApplicationService

__all__ = [
    "AuthorityReferences",
    "EventConsumptionDecision",
    "EventCursor",
    "TaskBudget",
    "TaskStatus",
    "TaskStep",
    "TaskStepStatus",
    "WaitingCondition",
    "WaitingKind",
    "WorkEventIdentity",
    "WorkGoal",
    "WorkTask",
    "WorkTaskApplicationService",
    "WorkTaskRepositoryPort",
    "advance_event_cursor",
    "decide_event_consumption",
    "transition_task",
    "transition_task_step",
]
