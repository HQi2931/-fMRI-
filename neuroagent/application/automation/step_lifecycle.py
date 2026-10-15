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
_ACTIVE_STEP_STATUSES = frozenset(
    {TaskStepStatus.READY, TaskStepStatus.RUNNING, TaskStepStatus.WAITING}
)
_STOPPED_STEP_STATUSES = frozenset(
    {TaskStepStatus.FAILED, TaskStepStatus.BLOCKED, TaskStepStatus.SKIPPED}
)


def resolve_linear_step_sequence(task: WorkTask) -> tuple[TaskStep, ...]:
    """Return a validated, sequence-ordered view of an explicit linear task.

    Empty ``steps`` remains the legacy task shape and resolves to an empty
    tuple. Explicit sequences must be contiguous from zero and may represent
    only an unstarted suffix, a successful prefix followed by one active step
    and a pending suffix, a stopped step followed by a pending suffix, or a
    fully successful sequence. A successful prefix with no active successor is
    rejected because completion and successor promotion are one transition.
    A fully successful sequence may still await goal-level artifact checks.
    """

    if not task.steps:
        return ()

    ordered = tuple(sorted(task.steps, key=lambda item: item.sequence))
    if tuple(step.sequence for step in ordered) != tuple(range(len(ordered))):
        raise ValueError("linear TaskStep sequences must be contiguous starting at zero")
    if task.status is TaskStatus.COMPLETED and any(
        step.status is not TaskStepStatus.SUCCEEDED for step in ordered
    ):
        raise ValueError("a completed WorkTask must have all TaskSteps succeeded")

    active_indexes = tuple(
        index for index, step in enumerate(ordered) if step.status in _ACTIVE_STEP_STATUSES
    )
    stopped_indexes = tuple(
        index for index, step in enumerate(ordered) if step.status in _STOPPED_STEP_STATUSES
    )
    if len(active_indexes) > 1:
        raise ValueError("linear task cannot have multiple active steps")
    if len(stopped_indexes) > 1:
        raise ValueError("linear task cannot have multiple stopped steps")

    if active_indexes:
        active_index = active_indexes[0]
        if any(step.status is not TaskStepStatus.SUCCEEDED for step in ordered[:active_index]):
            raise ValueError("all steps before the active step must have succeeded")
        if any(step.status is not TaskStepStatus.PENDING for step in ordered[active_index + 1 :]):
            raise ValueError("all steps after the active step must be pending")
        return ordered

    if stopped_indexes:
        stopped_index = stopped_indexes[0]
        if any(step.status is not TaskStepStatus.SUCCEEDED for step in ordered[:stopped_index]):
            raise ValueError("all steps before a stopped step must have succeeded")
        if any(step.status is not TaskStepStatus.PENDING for step in ordered[stopped_index + 1 :]):
            raise ValueError("all steps after a stopped step must be pending")
        return ordered

    all_succeeded = all(step.status is TaskStepStatus.SUCCEEDED for step in ordered)
    if all_succeeded:
        return ordered
    if all(step.status is TaskStepStatus.PENDING for step in ordered):
        return ordered

    raise ValueError("TaskStep statuses do not form a valid linear sequence")


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


def resolve_routed_workflow_run(task: WorkTask) -> tuple[str, TaskStep | None]:
    """Resolve the one Run that may receive workflow events for this task.

    Legacy tasks without explicit steps continue to use the task-level Run
    reference. Explicit single-step tasks retain their existing strict
    task/step binding contract. A linear multi-step task may route only the
    unique waiting step, and only while its waiting condition identifies that
    step's Run. This resolves event identity; it does not authorize readiness
    or completion of a multi-step task.
    """

    if not task.steps:
        run_id = task.authority_refs.workflow_run_id
        if run_id is None:
            raise ValueError("legacy task has no bound Run")
        return run_id, None

    ordered = resolve_linear_step_sequence(task)
    if len(ordered) == 1:
        run_id = task.authority_refs.workflow_run_id
        if run_id is None:
            raise ValueError("single-step task has no bound Run")
        step = resolve_single_bound_run_step(task, run_id)
        if step is None:
            raise ValueError("single-step Run binding is missing")
        return run_id, step

    waiting_steps = tuple(step for step in ordered if step.status is TaskStepStatus.WAITING)
    if len(waiting_steps) != 1:
        raise ValueError("linear multi-step task must have one waiting Run step")
    step = waiting_steps[0]
    if step.kind not in _SUPPORTED_RUN_STEP_KINDS:
        raise ValueError(f"unsupported Run step kind: {step.kind}")
    run_id = step.authority_refs.workflow_run_id
    if run_id is None:
        raise ValueError("active Run step has no bound Run")
    if task.authority_refs.workflow_run_id not in {None, run_id}:
        raise ValueError("task root Run reference differs from the active step Run")
    bound_run_ids = tuple(
        item.authority_refs.workflow_run_id
        for item in ordered
        if item.authority_refs.workflow_run_id is not None
    )
    if len(bound_run_ids) != len(set(bound_run_ids)):
        raise ValueError("linear TaskSteps cannot reuse a Run reference")

    waiting = task.waiting_condition
    if task.status is TaskStatus.WAITING_RUN:
        is_matching_wait = (
            waiting is not None
            and waiting.kind is WaitingKind.WORKFLOW_RUN
            and waiting.reference == run_id
        )
    elif task.status is TaskStatus.WAITING_USER:
        is_matching_wait = (
            waiting is not None
            and waiting.kind is WaitingKind.USER_DECISION
            and waiting.reference == f"qc:{run_id}"
        )
    elif task.status is TaskStatus.WAITING_DEPENDENCY:
        expected_reference = (
            f"statistical-result:{run_id}"
            if step.kind == "statistical_run"
            else f"downstream:{run_id}"
        )
        is_matching_wait = (
            waiting is not None
            and waiting.kind is WaitingKind.DEPENDENCY
            and waiting.reference == expected_reference
        )
    else:
        is_matching_wait = False
    if not is_matching_wait:
        raise ValueError("task waiting condition does not identify the active step Run")
    return run_id, step


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
        if (
            task_run_id != run_id
            or step_run_id != run_id
            or not _is_idempotent_run_binding(task, step, run_id)
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
            "authority_refs": task.authority_refs.model_copy(update={"workflow_run_id": run_id}),
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
    For an explicit linear sequence, completion advances only the immediate
    successor; the WorkTask remains active until its final step succeeds.
    """

    ordered_steps = resolve_linear_step_sequence(task)
    is_linear_multi_step = len(ordered_steps) > 1
    if is_linear_multi_step:
        _require_linear_task_step(task, step, ordered_steps)
    else:
        # Keep the existing single-step and no-steps legacy behavior unchanged.
        _require_task_step(task, step)
    if not artifact_ids or any(not item for item in artifact_ids):
        raise ValueError("completion requires non-empty Artifact IDs")
    if len(artifact_ids) != len(set(artifact_ids)):
        raise ValueError("completion Artifact IDs must be unique")

    normalized_ids = tuple(sorted(artifact_ids))

    if is_linear_multi_step and step.status is TaskStepStatus.SUCCEEDED:
        if step.authority_refs.artifact_ids != normalized_ids:
            raise ValueError("completed step Artifact references differ from verified evidence")
        index = next(i for i, item in enumerate(ordered_steps) if item.step_id == step.step_id)
        if index == len(ordered_steps) - 1 and (
            task.status is not TaskStatus.COMPLETED
            or task.authority_refs.artifact_ids != normalized_ids
        ):
            raise ValueError("completed final step does not match completed task evidence")
        # The original step may be replayed after its successor has already
        # progressed. Sequence validation above ensures it remains a valid
        # successful prefix; the replay must not alter that later progress.
        return task

    if is_linear_multi_step:
        _require_active_linear_task_step(step, ordered_steps)

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

    if is_linear_multi_step:
        return _complete_linear_step(
            task,
            step,
            ordered_steps=ordered_steps,
            artifact_ids=normalized_ids,
        )

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


def _complete_linear_step(
    task: WorkTask,
    step: TaskStep,
    *,
    ordered_steps: tuple[TaskStep, ...],
    artifact_ids: tuple[str, ...],
) -> WorkTask:
    """Complete one verified step and promote only its direct successor."""

    index = next(i for i, item in enumerate(ordered_steps) if item.step_id == step.step_id)
    successor = ordered_steps[index + 1] if index + 1 < len(ordered_steps) else None
    if step.kind not in _SUPPORTED_RUN_STEP_KINDS:
        raise ValueError(f"unsupported Run step kind: {step.kind}")
    if successor is not None:
        if successor.kind not in _SUPPORTED_RUN_STEP_KINDS:
            raise ValueError(f"unsupported next Run step kind: {successor.kind}")
        if successor.authority_refs.workflow_run_id is not None:
            raise ValueError("next TaskStep must not already be bound to a Run")

    succeeded_step = transition_task_step(step, TaskStepStatus.SUCCEEDED).model_copy(
        update={
            "authority_refs": step.authority_refs.model_copy(update={"artifact_ids": artifact_ids})
        }
    )
    steps = {item.step_id: item for item in ordered_steps}
    steps[step.step_id] = succeeded_step

    if successor is not None:
        ready_successor = transition_task_step(successor, TaskStepStatus.READY)
        steps[successor.step_id] = ready_successor
        running = transition_task(task, TaskStatus.RUNNING)
        advanced = running.model_copy(
            update={
                # The previous Run remains on its completed TaskStep. The task
                # root is only the active step projection, so clear its Plan,
                # Run, and input revision while the next step is being prepared.
                "authority_refs": task.authority_refs.model_copy(
                    update={
                        "plan_revision_id": None,
                        "workflow_run_id": None,
                        "input_revision": None,
                    }
                ),
                "steps": tuple(steps[item.step_id] for item in ordered_steps),
            }
        )
        return WorkTask.model_validate(advanced.model_dump())

    if task.status is TaskStatus.WAITING_USER:
        # A resolved QC gate is the only user-wait exception that may finish a
        # step. Its type-specific readiness handler has already checked it.
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
            "authority_refs": task.authority_refs.model_copy(update={"artifact_ids": artifact_ids}),
            "steps": tuple(steps[item.step_id] for item in ordered_steps),
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


def _require_linear_task_step(
    task: WorkTask,
    step: TaskStep,
    ordered_steps: tuple[TaskStep, ...],
) -> None:
    if step.kind not in _SUPPORTED_RUN_STEP_KINDS:
        raise ValueError(f"unsupported Run step kind: {step.kind}")
    matches = tuple(item for item in ordered_steps if item.step_id == step.step_id)
    if len(matches) != 1 or matches[0] != step:
        raise ValueError("step does not identify the current persisted task step")


def _require_active_linear_task_step(
    step: TaskStep,
    ordered_steps: tuple[TaskStep, ...],
) -> None:
    active_steps = tuple(item for item in ordered_steps if item.status in _ACTIVE_STEP_STATUSES)
    if len(active_steps) != 1 or active_steps[0] != step:
        raise ValueError("only the unique active TaskStep can be completed")


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
                condition.reference == f"downstream:{run_id}" and step.kind == "preprocessing_run"
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
