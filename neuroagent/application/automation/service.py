"""Narrow application use cases for durable user-goal progress."""

from __future__ import annotations

from neuroagent.application.automation.models import (
    EventConsumptionDecision,
    TaskStatus,
    WaitingCondition,
    WorkEventIdentity,
    WorkTask,
    transition_task,
)
from neuroagent.application.automation.ports import WorkTaskRepositoryPort
from neuroagent.application.errors import ConflictError, InputValidationError


class WorkTaskApplicationService:
    """Persist task intent and event receipts without owning scientific run state."""

    def __init__(self, repository: WorkTaskRepositoryPort) -> None:
        self._repository = repository

    def create_task(self, task: WorkTask) -> WorkTask:
        return self._repository.create_task(task)

    def get_task(self, task_id: str) -> WorkTask:
        return self._repository.get_task(task_id)

    def transition_task(
        self,
        task_id: str,
        *,
        expected_version: int,
        target: TaskStatus,
        waiting_condition: WaitingCondition | None = None,
    ) -> WorkTask:
        current = self._repository.get_task(task_id)
        if current.version != expected_version:
            raise ConflictError(
                "revision_conflict",
                "自动化任务已更新。请重新读取后再继续。",
                resource="work_task",
                resource_id=task_id,
                expected=expected_version,
                actual=current.version,
            )
        updated = transition_task(
            current,
            target,
            waiting_condition=waiting_condition,
        )
        return self._repository.save_task(updated, expected_version=expected_version)

    def consume_workflow_event(
        self,
        task_id: str,
        event: WorkEventIdentity,
    ) -> tuple[EventConsumptionDecision, WorkTask]:
        """Record a workflow notification once; workflow truth is reloaded elsewhere.

        This method deliberately does not interpret event payloads or copy Run,
        QC, or Artifact status. A successful receipt only advances the task's
        orchestration version and cursor so a later coordinator can reread the
        authoritative workflow records before deciding the next action.
        """

        current = self._repository.get_task(task_id)
        expected_run_id = current.authority_refs.workflow_run_id
        if expected_run_id is None or event.workflow_run_id != expected_run_id:
            raise InputValidationError(
                "workflow_event_run_mismatch",
                "运行事件与自动化任务绑定的运行不匹配。",
                task_id=task_id,
                expected_run_id=expected_run_id,
                event_run_id=event.workflow_run_id,
            )
        candidate = WorkTask.model_validate(
            {**current.model_dump(), "version": current.version + 1}
        )
        decision, _committed = self._repository.consume_event(
            task_id,
            event,
            expected_version=current.version,
            updated_task=candidate,
        )
        # Read after both applied and no-op decisions so a concurrent duplicate
        # returns current persisted state rather than a stale pre-read snapshot.
        return decision, self._repository.get_task(task_id)
