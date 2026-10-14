"""Transport-neutral contracts for durable progress toward a user's goal.

These records describe orchestration progress only. WorkflowInstance remains
the source of truth for execution state; references here identify authoritative
records without copying their mutable state.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator


class TaskStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    WAITING_USER = "waiting_user"
    WAITING_RUN = "waiting_run"
    WAITING_DEPENDENCY = "waiting_dependency"
    COMPLETED = "completed"
    BLOCKED = "blocked"
    CANCELLED = "cancelled"


class TaskStepStatus(StrEnum):
    PENDING = "pending"
    READY = "ready"
    RUNNING = "running"
    WAITING = "waiting"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    BLOCKED = "blocked"
    SKIPPED = "skipped"


class WaitingKind(StrEnum):
    USER_DECISION = "user_decision"
    WORKFLOW_RUN = "workflow_run"
    DEPENDENCY = "dependency"


class WorkGoal(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    description: str = Field(min_length=1, max_length=4000)
    completion_conditions: tuple[str, ...] = Field(min_length=1, max_length=32)


class WaitingCondition(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: WaitingKind
    reference: str = Field(min_length=1, max_length=300)
    reason: str = Field(min_length=1, max_length=2000)


class AuthorityReferences(BaseModel):
    """Identifiers and revisions only; never mirror Run/QC/Artifact status."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    conversation_id: str | None = Field(default=None, min_length=1, max_length=100)
    plan_revision_id: str | None = Field(default=None, min_length=1, max_length=100)
    workflow_run_id: str | None = Field(default=None, min_length=1, max_length=100)
    input_revision: str | None = Field(default=None, min_length=1, max_length=200)
    artifact_ids: tuple[str, ...] = Field(default=(), max_length=200)


class TaskBudget(BaseModel):
    """Shared task budget; counters are cumulative across resumes and roles."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    max_steps: int = Field(ge=1, le=1000)
    max_model_calls: int = Field(ge=0, le=1000)
    max_tokens: int = Field(ge=0, le=10_000_000)
    max_elapsed_seconds: int = Field(ge=1, le=31_536_000)
    used_steps: int = Field(default=0, ge=0)
    used_model_calls: int = Field(default=0, ge=0)
    used_tokens: int = Field(default=0, ge=0)
    elapsed_seconds: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def counters_do_not_exceed_limits(self) -> TaskBudget:
        if self.used_steps > self.max_steps:
            raise ValueError("used_steps exceeds max_steps")
        if self.used_model_calls > self.max_model_calls:
            raise ValueError("used_model_calls exceeds max_model_calls")
        if self.used_tokens > self.max_tokens:
            raise ValueError("used_tokens exceeds max_tokens")
        if self.elapsed_seconds > self.max_elapsed_seconds:
            raise ValueError("elapsed_seconds exceeds max_elapsed_seconds")
        return self


class TaskStep(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    step_id: str = Field(min_length=1, max_length=100)
    sequence: int = Field(ge=0)
    kind: str = Field(min_length=1, max_length=100)
    status: TaskStepStatus = TaskStepStatus.PENDING
    input_version: str | None = Field(default=None, min_length=1, max_length=200)
    authority_refs: AuthorityReferences = Field(default_factory=AuthorityReferences)
    recovery_steps: tuple[str, ...] = Field(default=(), max_length=32)
    version: int = Field(default=1, ge=1)


class EventCursor(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    stream_key: str = Field(min_length=1, max_length=300)
    event_id: int = Field(ge=0)


class WorkEventIdentity(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    stream_key: str = Field(min_length=1, max_length=300)
    workflow_run_id: str = Field(min_length=1, max_length=100)
    event_id: int = Field(ge=1)
    dedupe_key: str = Field(min_length=1, max_length=300)


class WorkTaskClaim(BaseModel):
    """Short-lived, fenced ownership of one background reconciliation pass."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    task_id: str = Field(min_length=1, max_length=100)
    worker_id: str = Field(min_length=1, max_length=200)
    claim_token: str = Field(min_length=32, max_length=64)


class WorkTask(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    task_id: str = Field(min_length=1, max_length=100)
    goal: WorkGoal
    status: TaskStatus = TaskStatus.QUEUED
    waiting_condition: WaitingCondition | None = None
    authority_refs: AuthorityReferences = Field(default_factory=AuthorityReferences)
    steps: tuple[TaskStep, ...] = Field(default=(), max_length=1000)
    recovery_steps: tuple[str, ...] = Field(default=(), max_length=64)
    budget: TaskBudget
    event_cursors: tuple[EventCursor, ...] = Field(default=(), max_length=100)
    version: int = Field(default=1, ge=1)

    @model_validator(mode="after")
    def waiting_state_has_condition(self) -> WorkTask:
        needs_condition = self.status in {
            TaskStatus.WAITING_USER,
            TaskStatus.WAITING_RUN,
            TaskStatus.WAITING_DEPENDENCY,
        }
        if needs_condition != (self.waiting_condition is not None):
            raise ValueError("waiting_condition must be present exactly for waiting task states")
        if self.waiting_condition is not None:
            expected = {
                TaskStatus.WAITING_USER: WaitingKind.USER_DECISION,
                TaskStatus.WAITING_RUN: WaitingKind.WORKFLOW_RUN,
                TaskStatus.WAITING_DEPENDENCY: WaitingKind.DEPENDENCY,
            }[self.status]
            if self.waiting_condition.kind is not expected:
                raise ValueError("waiting_condition kind does not match task status")
        if len({step.step_id for step in self.steps}) != len(self.steps):
            raise ValueError("step_id values must be unique within a task")
        if len({step.sequence for step in self.steps}) != len(self.steps):
            raise ValueError("step sequence values must be unique within a task")
        if len({cursor.stream_key for cursor in self.event_cursors}) != len(self.event_cursors):
            raise ValueError("event cursor stream keys must be unique")
        return self


class EventConsumptionDecision(StrEnum):
    CONSUME = "consume"
    DUPLICATE = "duplicate"
    STALE = "stale"


class RunReadiness(StrEnum):
    """Safe next action inferred from freshly read authoritative records."""

    COMPLETED = "completed"
    WAITING_RUN = "waiting_run"
    WAITING_USER = "waiting_user"
    WAITING_DEPENDENCY = "waiting_dependency"
    BLOCKED = "blocked"
    INDETERMINATE = "indeterminate"


class EventReconciliationReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    event_id: int = Field(ge=1)
    decision: EventConsumptionDecision


class AuthoritativeRunSnapshot(BaseModel):
    """Small read-only view of Workflow, QC and result authority records."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: str = Field(min_length=1, max_length=100)
    run_state: str = Field(min_length=1, max_length=100)
    run_version: int = Field(ge=1)
    attempt: int = Field(ge=1)
    qc_mode: str | None = Field(default=None, min_length=1, max_length=100)
    qc_status: str | None = Field(default=None, min_length=1, max_length=100)
    latest_qc_review_id: str | None = Field(default=None, max_length=100)
    latest_qc_review_state: str | None = Field(default=None, max_length=100)
    qc_skip_id: str | None = Field(default=None, max_length=100)
    registered_artifact_ids: tuple[str, ...] = Field(default=(), max_length=1000)
    registered_artifact_types: tuple[str, ...] = Field(default=(), max_length=1000)
    resolved_referenced_artifact_ids: tuple[str, ...] = Field(default=(), max_length=200)
    unresolved_referenced_artifact_ids: tuple[str, ...] = Field(default=(), max_length=200)
    statistical_result_ids: tuple[str, ...] = Field(default=(), max_length=100)
    verified_statistical_result_id: str | None = Field(default=None, max_length=100)
    result_artifact_ids: tuple[str, ...] = Field(default=(), max_length=1000)


class WorkflowRunReconciliation(BaseModel):
    """Result of event dedupe followed by authoritative state re-reads."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    task: WorkTask
    readiness: RunReadiness
    event_receipts: tuple[EventReconciliationReceipt, ...] = Field(default=(), max_length=1000)
    authoritative: AuthoritativeRunSnapshot | None = None
    reason: str = Field(min_length=1, max_length=2000)


_TASK_TRANSITIONS: dict[TaskStatus, frozenset[TaskStatus]] = {
    TaskStatus.QUEUED: frozenset(
        {
            TaskStatus.RUNNING,
            TaskStatus.WAITING_USER,
            TaskStatus.WAITING_RUN,
            TaskStatus.WAITING_DEPENDENCY,
            TaskStatus.BLOCKED,
            TaskStatus.CANCELLED,
        }
    ),
    TaskStatus.RUNNING: frozenset(
        {
            TaskStatus.WAITING_USER,
            TaskStatus.WAITING_RUN,
            TaskStatus.WAITING_DEPENDENCY,
            TaskStatus.COMPLETED,
            TaskStatus.BLOCKED,
            TaskStatus.CANCELLED,
        }
    ),
    TaskStatus.WAITING_USER: frozenset(
        {
            TaskStatus.RUNNING,
            TaskStatus.WAITING_RUN,
            TaskStatus.WAITING_DEPENDENCY,
            TaskStatus.BLOCKED,
            TaskStatus.CANCELLED,
        }
    ),
    TaskStatus.WAITING_RUN: frozenset(
        {
            TaskStatus.RUNNING,
            TaskStatus.WAITING_USER,
            TaskStatus.WAITING_DEPENDENCY,
            TaskStatus.COMPLETED,
            TaskStatus.BLOCKED,
            TaskStatus.CANCELLED,
        }
    ),
    TaskStatus.WAITING_DEPENDENCY: frozenset(
        {TaskStatus.RUNNING, TaskStatus.COMPLETED, TaskStatus.BLOCKED, TaskStatus.CANCELLED}
    ),
    TaskStatus.BLOCKED: frozenset({TaskStatus.RUNNING, TaskStatus.CANCELLED}),
    TaskStatus.COMPLETED: frozenset(),
    TaskStatus.CANCELLED: frozenset(),
}

_STEP_TRANSITIONS: dict[TaskStepStatus, frozenset[TaskStepStatus]] = {
    TaskStepStatus.PENDING: frozenset({TaskStepStatus.READY, TaskStepStatus.SKIPPED}),
    TaskStepStatus.READY: frozenset(
        {TaskStepStatus.RUNNING, TaskStepStatus.BLOCKED, TaskStepStatus.SKIPPED}
    ),
    TaskStepStatus.RUNNING: frozenset(
        {
            TaskStepStatus.WAITING,
            TaskStepStatus.SUCCEEDED,
            TaskStepStatus.FAILED,
            TaskStepStatus.BLOCKED,
        }
    ),
    TaskStepStatus.WAITING: frozenset(
        {
            TaskStepStatus.READY,
            TaskStepStatus.RUNNING,
            TaskStepStatus.SUCCEEDED,
            TaskStepStatus.BLOCKED,
        }
    ),
    TaskStepStatus.FAILED: frozenset({TaskStepStatus.READY, TaskStepStatus.BLOCKED}),
    TaskStepStatus.BLOCKED: frozenset({TaskStepStatus.READY}),
    TaskStepStatus.SUCCEEDED: frozenset(),
    TaskStepStatus.SKIPPED: frozenset(),
}


def transition_task(
    task: WorkTask,
    target: TaskStatus,
    *,
    waiting_condition: WaitingCondition | None = None,
) -> WorkTask:
    """Return a versioned task transition or reject an illegal transition."""

    if target not in _TASK_TRANSITIONS[task.status]:
        raise ValueError(f"invalid task transition: {task.status.value} -> {target.value}")
    return WorkTask.model_validate(
        {
            **task.model_dump(),
            "status": target,
            "waiting_condition": waiting_condition,
            "version": task.version + 1,
        }
    )


def transition_task_step(step: TaskStep, target: TaskStepStatus) -> TaskStep:
    if target not in _STEP_TRANSITIONS[step.status]:
        raise ValueError(f"invalid task step transition: {step.status.value} -> {target.value}")
    return step.model_copy(update={"status": target, "version": step.version + 1})


def decide_event_consumption(
    event: WorkEventIdentity,
    *,
    cursor: int,
    dedupe_key_seen: bool,
) -> EventConsumptionDecision:
    """Classify an event before an atomic apply+receipt+cursor transaction.

    The infrastructure adapter must check the unique dedupe key and persist
    the effects, receipt, and cursor in one SQLite transaction.
    """

    if dedupe_key_seen:
        return EventConsumptionDecision.DUPLICATE
    if event.event_id <= cursor:
        return EventConsumptionDecision.STALE
    return EventConsumptionDecision.CONSUME


def advance_event_cursor(task: WorkTask, stream_key: str, event_id: int) -> WorkTask:
    """Advance one stream cursor monotonically after the event is applied."""

    if not stream_key or event_id < 1:
        raise ValueError("stream_key and positive event_id are required")
    cursors = {item.stream_key: item.event_id for item in task.event_cursors}
    previous = cursors.get(stream_key, 0)
    if event_id <= previous:
        return task
    cursors[stream_key] = event_id
    updated = tuple(
        EventCursor(stream_key=key, event_id=value) for key, value in sorted(cursors.items())
    )
    return WorkTask.model_validate(
        {**task.model_dump(), "event_cursors": updated, "version": task.version + 1}
    )
