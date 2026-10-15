"""Pure, shared WorkTask/TaskStep lifecycle transitions.

The helpers in this module only construct versioned orchestration state. Callers
remain responsible for validating the scientific authority records and for
persisting the returned task with their existing CAS/fencing and Unit of Work.
"""

from __future__ import annotations

from neuroagent.application.automation.models import (
    TaskStatus,
    TaskStep,
    TaskStepStatus,
    WaitingCondition,
    WaitingKind,
    WorkTask,
    transition_task,
    transition_task_step,
)

_SUPPORTED_RUN_STEP_KINDS = frozenset({"preprocessing_run", "statistical_run"})


def resolve_single_bound_run_step(task: WorkTask, run_id: str) -> TaskStep | None:
    """Resolve the current single-step Run contract or reject ambiguous state.

    Tasks without TaskSteps are returned as ``None`` for the existing legacy
    reconciliation fallback. Once a task has explicit steps, it must have one
    supported Run step whose task and step Run references agree exactly.
    """

    if not run_id:
        raise ValueError("run_id is required")
    if not task.steps:
        return None
    if len(task.steps) != 1:
        raise ValueError("current Run lifecycle requires exactly one TaskStep")
    step = task.steps[0]
    if step.kind not in _SUPPORTED_RUN_STEP_KINDS:
        raise ValueError(f"unsupported Run step kind: {step.kind}")
    if (
        task.authority_refs.workflow_run_id != run_id
        or step.authority_refs.workflow_run_id != run_id
    ):
        raise ValueError("task and step must reference the same bound Run")
    if step.status is not TaskStepStatus.WAITING:
        raise ValueError("the uniquely bound Run step must be waiting")
    return step


def bind_step_to_run(
    task: WorkTask,
    step: TaskStep,
    *,
    run_id: str,
    waiting_reason: str,
) -> WorkTask:
    """Bind one ready user-authorized step to a Run and wait for that Run.

    Replaying an already committed binding is a no-op only when the task, step,
    waiting condition, and Run references still agree. A stale or partial
    binding fails closed.
    """

    if not run_id:
        raise ValueError("run_id is required")
    _require_task_step(task, step)

    task_run_id = task.authority_refs.workflow_run_id
    step_run_id = step.authority_refs.workflow_run_id
    if task_run_id is not None or step_run_id is not None:
        if task_run_id != run_id or step_run_id != run_id or not _is_idempotent_run_binding(
            task, step, run_id
        ):
            raise ValueError("task or step is already bound to a different or invalid Run state")
        return task

    if task.status is not TaskStatus.WAITING_USER or step.status is not TaskStepStatus.READY:
        raise ValueError("only a waiting-user task with a ready step can bind a Run")

    started_step = transition_task_step(step, TaskStepStatus.RUNNING)
    waiting_step = transition_task_step(started_step, TaskStepStatus.WAITING).model_copy(
        update={
            "authority_refs": started_step.authority_refs.model_copy(
                update={"workflow_run_id": run_id}
            )
        }
    )
    waiting = transition_task(
        task,
        TaskStatus.WAITING_RUN,
        waiting_condition=WaitingCondition(
            kind=WaitingKind.WORKFLOW_RUN,
            reference=run_id,
            reason=waiting_reason,
        ),
    ).model_copy(
        update={
            "authority_refs": task.authority_refs.model_copy(
                update={"workflow_run_id": run_id}
            ),
            "steps": tuple(
                waiting_step if item.step_id == step.step_id else item for item in task.steps
            ),
        }
    )
    return WorkTask.model_validate(waiting.model_dump())


def complete_step(
    task: WorkTask,
    step: TaskStep,
    *,
    artifact_ids: tuple[str, ...],
) -> WorkTask:
    """Complete a waiting step after its caller has verified all evidence.

    This helper relies on ``transition_task`` for normal transitions. The only
    user-wait exception is a step that was waiting on QC for this exact Run;
    callers must first verify the authoritative QC decision and step evidence.
    """

    _require_task_step(task, step)
    if not artifact_ids or any(not item for item in artifact_ids):
        raise ValueError("completion requires non-empty Artifact IDs")
    if len(artifact_ids) != len(set(artifact_ids)):
        raise ValueError("completion Artifact IDs must be unique")

    normalized_ids = tuple(sorted(artifact_ids))
    run_id = task.authority_refs.workflow_run_id
    if run_id is None or step.authority_refs.workflow_run_id != run_id:
        raise ValueError("completed task and step must reference the same Run")

    if task.status is TaskStatus.COMPLETED and step.status is TaskStepStatus.SUCCEEDED:
        if (
            task.authority_refs.artifact_ids == normalized_ids
            and step.authority_refs.artifact_ids == normalized_ids
        ):
            return task
        raise ValueError("completed task Artifact references differ from verified evidence")

    if step.status is not TaskStepStatus.WAITING:
        raise ValueError("only a waiting step can be completed")

    if not _is_ready_for_verified_completion(task, step, run_id):
        raise ValueError("task is not waiting for this verified Run or QC decision")

    succeeded_step = transition_task_step(step, TaskStepStatus.SUCCEEDED).model_copy(
        update={
            "authority_refs": step.authority_refs.model_copy(
                update={"artifact_ids": normalized_ids}
            )
        }
    )
    if task.status is TaskStatus.WAITING_USER:
        # A resolved QC gate is the only user wait that may finish a step.
        # The caller reaches this point only after its type-specific checker
        # has verified the authoritative approval or explicit skip record.
        completed = WorkTask.model_validate(
            {
                **task.model_dump(),
                "status": TaskStatus.COMPLETED,
                "waiting_condition": None,
                "version": task.version + 1,
            }
        )
    else:
        completed = transition_task(task, TaskStatus.COMPLETED)
    completed = completed.model_copy(
        update={
            "authority_refs": task.authority_refs.model_copy(
                update={"artifact_ids": normalized_ids}
            ),
            "steps": tuple(
                succeeded_step if item.step_id == step.step_id else item for item in task.steps
            ),
        }
    )
    return WorkTask.model_validate(completed.model_dump())


def _is_ready_for_verified_completion(task: WorkTask, step: TaskStep, run_id: str) -> bool:
    condition = task.waiting_condition
    if condition is None:
        return False
    if task.status is TaskStatus.WAITING_RUN:
        return condition.kind is WaitingKind.WORKFLOW_RUN and condition.reference == run_id
    if task.status is TaskStatus.WAITING_DEPENDENCY:
        if condition.kind is not WaitingKind.DEPENDENCY:
            return False
        if condition.reference == f"downstream:{run_id}":
            return step.kind == "preprocessing_run"
        if condition.reference == f"statistical-result:{run_id}":
            return step.kind == "statistical_run"
        return False
    if task.status is TaskStatus.WAITING_USER:
        return (
            step.kind == "preprocessing_run"
            and condition.kind is WaitingKind.USER_DECISION
            and condition.reference == f"qc:{run_id}"
        )
    return False


def _require_task_step(task: WorkTask, step: TaskStep) -> None:
    if step.kind not in _SUPPORTED_RUN_STEP_KINDS:
        raise ValueError(f"unsupported Run step kind: {step.kind}")
    if len(task.steps) != 1:
        raise ValueError("current Run lifecycle requires exactly one TaskStep")
    matches = tuple(item for item in task.steps if item.step_id == step.step_id)
    if len(matches) != 1 or matches[0] != step:
        raise ValueError("step does not identify the current persisted task step")


def _is_idempotent_run_binding(task: WorkTask, step: TaskStep, run_id: str) -> bool:
    if step.status is TaskStepStatus.WAITING:
        if task.status is TaskStatus.WAITING_RUN:
            return (
                task.waiting_condition is not None
                and task.waiting_condition.kind is WaitingKind.WORKFLOW_RUN
                and task.waiting_condition.reference == run_id
            )
        if task.status is TaskStatus.WAITING_USER:
            return (
                step.kind == "preprocessing_run"
                and task.waiting_condition is not None
                and task.waiting_condition.kind is WaitingKind.USER_DECISION
                and task.waiting_condition.reference == f"qc:{run_id}"
            )
        if task.status is TaskStatus.WAITING_DEPENDENCY:
            condition = task.waiting_condition
            if condition is None or condition.kind is not WaitingKind.DEPENDENCY:
                return False
            return (
                condition.reference == f"downstream:{run_id}"
                and step.kind == "preprocessing_run"
            ) or (
                condition.reference == f"statistical-result:{run_id}"
                and step.kind == "statistical_run"
            )
        return False
    if task.status is TaskStatus.COMPLETED and step.status is TaskStepStatus.SUCCEEDED:
        return bool(task.authority_refs.artifact_ids) and (
            task.authority_refs.artifact_ids == step.authority_refs.artifact_ids
        )
    return False
